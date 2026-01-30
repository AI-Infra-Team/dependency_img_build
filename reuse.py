"""
Layer reuse logic using order-insensitive prefix matching and in-image metadata only.
No legacy JSON cache.

Parallel image inspection uses daemon threads only (no processes).
Use env var to tune workers:
  - IMGDEPS_INSPECT_WORKERS: int, default min(8, len(candidates))
"""

import os
import sys
import re
import subprocess
import logging
import threading
import queue
from typing import List, Dict, Optional, Tuple, Set
import shlex
import shutil
from config import (
    Layer,
    LayerType,
    IMAGE_DEP_METADATA_PATH,  # legacy fallback
    IMAGE_LABEL_ITEMS_B64,
    IMAGE_LABEL_ITEMS,
)
from utils import sudo_prefix

# Plain logging option to avoid wide Unicode and strange terminal alignment
# Default to plain ASCII to avoid terminal width issues; set 0 to enable emojis
PLAIN_LOG = os.getenv('IMGDEPS_LOG_PLAIN', '1').lower() in ('1', 'true', 'yes')
ICON_FIND = '🔍' if not PLAIN_LOG else '[FIND]'
ICON_CLIP = '📋' if not PLAIN_LOG else '[LIST]'
ICON_WHALE = '🐳' if not PLAIN_LOG else '[CANDS]'
ICON_THREAD = '🧵' if not PLAIN_LOG else '[THREAD]'
ICON_INSPECT = '🔎 Inspect' if not PLAIN_LOG else '[INSPECT]'
ICON_STAR = '⭐' if not PLAIN_LOG else '[BEST]'
ICON_CHECK = '✅' if not PLAIN_LOG else '[OK]'
ICON_CROSS = '❌' if not PLAIN_LOG else '[NONE]'


class LayerReuseManager:
    """Manages layer reuse using only in-image metadata."""

    def __init__(self, concurrency: int | None = None):
        # Allow control via env var
        env_workers = os.getenv('IMGDEPS_INSPECT_WORKERS')
        self.concurrency = concurrency if concurrency is not None else (int(env_workers) if (env_workers or '').isdigit() else None)

    def find_optimal_base(self, target_layers: List[Layer], preferred_repo: Optional[str] = None, required_tag_prefix: Optional[str] = None) -> Tuple[str, Set[str], List[Layer], List[Dict]]:
        """
        Find the best base image by scanning local Docker images and reading
        dependency metadata from IMAGE_DEP_METADATA_PATH inside each image.
        """
        ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
        SUMMARY_ONLY = os.getenv('IMGDEPS_INSPECT_SUMMARY_ONLY', '1').lower() in ('1','true','yes')
        LOG_FILE_PATH = os.getenv('IMGDEPS_INSPECT_LOG_FILE', '').strip()
        def _sanitize(msg: str) -> str:
            if msg is None:
                return ""
            if not isinstance(msg, str):
                msg = str(msg)
            # Remove ANSI escape codes
            msg = ANSI_RE.sub('', msg)
            # Remove carriage returns which can cause odd alignment
            msg = msg.replace('\r', '')
            # Keep only printable ASCII plus newline and tab
            msg = ''.join(ch for ch in msg if ch == '\n' or ch == '\t' or 32 <= ord(ch) <= 126)
            # Trim leading/trailing whitespace to avoid odd left-padding from upstream
            msg = msg.strip()
            return msg

        def _println(msg: str):
            # Single place for output: stdout, line-buffered with sanitization
            safe = _sanitize(msg)
            sys.stdout.write(safe + "\n")
            try:
                sys.stdout.flush()
            except Exception:
                pass
        def _print_block(lines: List[str]):
            # Print a whole logical block atomically to reduce interleaving
            if not lines:
                return
            safe = "\n".join(_sanitize(line) for line in lines)
            sys.stdout.write(safe + "\n")
            try:
                sys.stdout.flush()
            except Exception:
                pass

        def _restore_tty():
            if not sys.stdout.isatty():
                return
            if os.getenv('IMGDEPS_TTY_RESTORE', '1').lower() not in ('1','true','yes'):
                return
            try:
                if shutil.which('stty'):
                    subprocess.run(['stty', 'sane'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
            if LOG_FILE_PATH:
                try:
                    with open(LOG_FILE_PATH, 'a', encoding='utf-8') as f:
                        f.write(safe + "\n")
                except Exception:
                    # ignore logging failures
                    pass

        _restore_tty()
        _println(f"{ICON_FIND} Finding optimal reuse strategy (in-image metadata)...")

        # Build target signature for reuse selection.
        #
        # Reuse is decided by order-insensitive prefix matching, and items are content-hashed
        # (type:name:hash). This guarantees that any layer content change invalidates reuse.
        target_items_ordered: List[str] = []
        target_items: Set[str] = set()
        target_item_to_layer: Dict[str, Layer] = {}
        ordered_layers: List[Layer] = []

        # Strict item format: "<type>:<name>:<hash>" where hash is 8 hex chars (Layer.calculate_hash()).
        item_re = re.compile(r"^(apt|yum|pip|script):[^:]+:[0-9a-f]{8}$")

        for layer in target_layers:
            if layer.type == LayerType.BASE:
                continue
            if layer.type in (LayerType.APT, LayerType.YUM, LayerType.PIP, LayerType.SCRIPT):
                item = f"{layer.type.value}:{layer.name}:{layer.hash}"
                if not item_re.fullmatch(item):
                    raise ValueError(f"invalid target metadata item format: {item}")
                target_items_ordered.append(item)
                target_items.add(item)
                target_item_to_layer[item] = layer
            ordered_layers.append(layer)

        _println(f"{ICON_CLIP} Target layers: {len(target_items)}")

        # Precompute prefix sets for order-insensitive prefix matching.
        # A cached image with k items is reusable only if its item set equals
        # the set of the first k target items (same elements, any order).
        prefix_sets: List[Set[str]] = [set()]
        cur: Set[str] = set()
        for it in target_items_ordered:
            cur = set(cur)
            cur.add(it)
            prefix_sets.append(cur)

        # Gather candidate images
        candidates = self._list_local_images(preferred_repo, required_tag_prefix)
        _println(f"{ICON_WHALE} Candidates: {len(candidates)}")

        best_image: Optional[str] = None
        best_reused_n = 0
        best_score = float('-inf')
        best_cached_items: List[str] = []

        # Parallel or sequential inspection
        workers = self.concurrency or min(8, max(1, len(candidates)))
        _println(f"{ICON_THREAD} Inspect mode: thread (daemon), workers: {workers}")

        print_lock = threading.Lock()
        def handle_result(image_tag: str, pkg_list: List[str], debug: Dict):
            nonlocal best_score, best_image, best_reused_n, best_cached_items
            with print_lock:
                if not pkg_list:
                    if SUMMARY_ONLY:
                        first_cmd = (debug.get('commands') or [''])[0]
                        _print_block([f"{ICON_INSPECT} {image_tag} | cmd: {first_cmd} | found: 0; missing: {len(target_items)}; score: -inf"])
                    else:
                        out = [f"{ICON_INSPECT} {image_tag}"]
                        out.extend([f"cmd: {c}" for c in debug.get('commands', [])])
                        out.append(f"found: 0; missing: {len(target_items)}; score: -inf")
                        _print_block(out)
                    return
                cached_items = [str(x) for x in pkg_list if isinstance(x, str) and x.strip()]
                # Only accept images that already use hashed metadata items.
                # This prevents reusing images built with legacy, non-hashed metadata.
                if any(item_re.fullmatch(it) is None for it in cached_items):
                    score = float('-inf')
                    cached_set: Set[str] = set()
                    extra_n = 0
                    missing_n = len(target_items)
                else:
                    cached_set = set(cached_items)
                    extra_n = len(cached_set - target_items)
                    if extra_n != 0:
                        # Candidate has items not in target; do not reuse.
                        score = float('-inf')
                        missing_n = len(target_items)
                    else:
                        k = len(cached_set)
                        # Order-insensitive prefix match: cached items must be exactly the first k target items.
                        if k < len(prefix_sets) and cached_set == prefix_sets[k]:
                            score = float(k)
                            missing_n = len(target_items) - k
                        else:
                            score = float('-inf')
                            missing_n = len(target_items)
                if SUMMARY_ONLY:
                    first_cmd = (debug.get('commands') or [''])[0]
                    line = f"{ICON_INSPECT} {image_tag} | cmd: {first_cmd} | found: {len(cached_set)}; missing: {missing_n}; extra: {extra_n}; score: {score}"
                    if score > best_score:
                        best_score = score
                        best_image = image_tag
                        best_reused_n = len(cached_set)
                        best_cached_items = list(cached_items)
                        line += f" | {ICON_STAR}"
                    _print_block([line])
                else:
                    out = [f"{ICON_INSPECT} {image_tag}"]
                    out.extend([f"cmd: {c}" for c in debug.get('commands', [])])
                    out.append(f"found: {len(cached_set)}; missing: {missing_n}; extra: {extra_n}; score: {score}")
                    if score > best_score:
                        best_score = score
                        best_image = image_tag
                        best_reused_n = len(cached_set)
                        best_cached_items = list(cached_items)
                        out.append(f"{ICON_STAR} Best so far -> {best_image} (reused={best_reused_n}, missing={len(target_items)-best_reused_n})")
                    _print_block(out)

        if workers > 1 and candidates:
            # Daemon thread pool using queues to ensure daemon=True and main-thread printing
            task_q: queue.Queue = queue.Queue()
            result_q: queue.Queue = queue.Queue()

            def _worker_thr():
                while True:
                    tag = task_q.get()
                    if tag is None:
                        task_q.task_done()
                        break
                    try:
                        items, dbg = self._read_packages_from_image_metadata(tag)
                        result_q.put((tag, items, dbg))
                    except Exception as e:
                        result_q.put((tag, [], {"commands": [f"<error: {e}>"]}))
                    finally:
                        task_q.task_done()

            threads: List[threading.Thread] = []
            for i in range(workers):
                t = threading.Thread(target=_worker_thr, name=f"imgdeps-inspect-{i}", daemon=True)
                t.start()
                threads.append(t)

            # Enqueue tasks
            for tag in candidates:
                task_q.put(tag)

            # Consume results as they complete (no order), printing in main thread
            processed = 0
            while processed < len(candidates):
                tag, items, dbg = result_q.get()
                handle_result(tag, items, dbg)
                processed += 1

            # Stop workers
            for _ in threads:
                task_q.put(None)
            task_q.join()
        else:
            for image_tag in candidates:
                pkg_list, debug = self._read_packages_from_image_metadata(image_tag)
                handle_result(image_tag, pkg_list, debug)
        reused_layer_names: Set[str] = set()
        layers_to_build: List[Layer] = []
        cleanup_commands: List[Dict] = []

        if best_image and best_reused_n > 0 and best_cached_items:
            cached_set = set(best_cached_items)
            _println(f"{ICON_CHECK} Best base: {best_image} (reused={len(cached_set)}, missing={len(target_items - cached_set)})")
            _restore_tty()

            # Reuse any layer whose content-hash item is already present in the cached image.
            # Order of the cached metadata list is ignored by design.
            for layer in ordered_layers:
                if layer.type == LayerType.CONFIG:
                    layers_to_build.append(layer)
                    continue

                if layer.type not in (LayerType.APT, LayerType.YUM, LayerType.PIP, LayerType.SCRIPT):
                    continue

                item = f"{layer.type.value}:{layer.name}:{layer.hash}"
                if item in cached_set:
                    reused_layer_names.add(layer.name)
                else:
                    layers_to_build.append(layer)

            return best_image, reused_layer_names, layers_to_build, cleanup_commands

        # No suitable base found, build from scratch
        _println(f"{ICON_CROSS} No suitable base found, building from scratch")
        _restore_tty()
        base_image = target_layers[0].content if target_layers else "ubuntu:22.04"
        return base_image, reused_layer_names, ordered_layers, cleanup_commands

    def generate_cleanup_commands(self, extra_packages: Set[str]) -> List[Dict]:
        """Generate cleanup commands for extra items across managers"""
        cleanup_commands: List[Dict] = []
        if not extra_packages:
            return cleanup_commands
        groups: Dict[str, List[str]] = {'apt': [], 'yum': [], 'pip': []}
        script_names: List[str] = []
        for item in extra_packages:
            if item.startswith('apt:'):
                groups['apt'].append(item[4:])
            elif item.startswith('yum:'):
                groups['yum'].append(item[4:])
            elif item.startswith('pip:'):
                groups['pip'].append(item[4:])
            elif item.startswith('script:'):
                script_names.append(item[7:])
        if groups['apt']:
            cleanup_commands.append({'type': 'apt_remove', 'packages': groups['apt'], 'description': f"Safe removal of {len(groups['apt'])} extra APT packages"})
        if groups['yum']:
            cleanup_commands.append({'type': 'yum_remove', 'packages': groups['yum'], 'description': f"Safe removal of {len(groups['yum'])} extra YUM packages"})
        if groups['pip']:
            cleanup_commands.append({'type': 'pip_remove', 'packages': groups['pip'], 'description': f"Uninstall {len(groups['pip'])} extra PIP packages"})
        if script_names:
            cleanup_commands.append({'type': 'script_remove', 'scripts': script_names, 'description': f"Extra scripts present: {len(script_names)} (not auto-removed)"})
        return cleanup_commands

    def _list_local_images(self, preferred_repo: Optional[str], required_tag_prefix: Optional[str]) -> List[str]:
        """List local docker images as repo:tag, filtered by repo and tag prefix if provided.
        Uses sudo when not running as root to match build-time permissions.
        """
        try:
            cmd = sudo_prefix() + ['docker', 'images', '--format', '{{.Repository}}:{{.Tag}}']
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            if result.returncode != 0:
                return []
            items = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            # Filter out dangling tags
            items = [it for it in items if not it.endswith(':<none>')]
            if preferred_repo:
                items = [it for it in items if it.split(':', 1)[0] == preferred_repo]
            if required_tag_prefix:
                items = [it for it in items if it.split(':', 1)[1].startswith(required_tag_prefix)]
            return items
        except Exception as e:
            logging.warning(f"Failed to list docker images: {e}")
            return []

    def _read_packages_from_image_metadata(self, image_tag: str) -> Tuple[List[str], Dict[str, List[str]]]:
        """Read dependency items from image metadata labels; fallback to legacy in-image file.

        Label strategy:
          - First try LABEL io.teleinfra.imgdeps.items_b64 (base64(JSON array))
          - Then try LABEL io.teleinfra.imgdeps.items (JSON array)
          - Fallback: docker run cat IMAGE_DEP_METADATA_PATH
        """
        dbg: Dict[str, List[str]] = {"commands": []}
        def _fmt(cmd: List[str]) -> str:
            return ' '.join(shlex.quote(x) for x in cmd)
        # Try labels via image inspect (fast, no container)
        try:
            cmd = sudo_prefix() + ['docker', 'image', 'inspect', image_tag, '--format', '{{json .Config.Labels}}']
            dbg["commands"].append(_fmt(cmd))
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
            if result.returncode == 0 and result.stdout.strip():
                import json, base64
                labels = json.loads(result.stdout.strip() or 'null')
                if isinstance(labels, dict) and labels:
                    if IMAGE_LABEL_ITEMS_B64 in labels and labels[IMAGE_LABEL_ITEMS_B64]:
                        try:
                            data = base64.b64decode(labels[IMAGE_LABEL_ITEMS_B64]).decode('utf-8')
                            items = json.loads(data)
                            if isinstance(items, list):
                                return [str(x) for x in items if isinstance(x, (str, int, float)) and str(x).strip()], dbg
                        except Exception:
                            pass
                    if IMAGE_LABEL_ITEMS in labels and labels[IMAGE_LABEL_ITEMS]:
                        try:
                            items = json.loads(labels[IMAGE_LABEL_ITEMS])
                            if isinstance(items, list):
                                return [str(x) for x in items if isinstance(x, (str, int, float)) and str(x).strip()], dbg
                        except Exception:
                            pass
        except Exception:
            pass

        # Legacy fallback: read file inside container (slower)
        try:
            cmd = sudo_prefix() + ['docker', 'run', '--rm', '--entrypoint', '/bin/cat', image_tag, IMAGE_DEP_METADATA_PATH]
            dbg["commands"].append(_fmt(cmd))
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            if result.returncode != 0:
                return [], dbg
            content = result.stdout.strip()
            if not content:
                return [], dbg
            return [line.strip() for line in content.split('\n') if line.strip()], dbg
        except Exception:
            return [], dbg
