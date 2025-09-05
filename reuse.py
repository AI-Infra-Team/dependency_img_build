"""
Layer reuse logic using set intersection and in-image metadata only.
No legacy JSON cache.
"""

import subprocess
import logging
from typing import List, Dict, Optional, Tuple, Set
from config import Layer, LayerType, IMAGE_DEP_METADATA_PATH
from utils import sudo_prefix


class LayerReuseManager:
    """Manages layer reuse using only in-image metadata."""

    def __init__(self):
        pass

    def find_optimal_base(self, target_layers: List[Layer], preferred_repo: Optional[str] = None, required_tag_prefix: Optional[str] = None) -> Tuple[str, Set[str], List[Layer], List[Dict]]:
        """
        Find the best base image by scanning local Docker images and reading
        dependency metadata from IMAGE_DEP_METADATA_PATH inside each image.
        """
        logging.info("🔍 Finding optimal reuse strategy (in-image metadata)...")

        # Build target set
        target_set: Set[str] = set()
        target_layer_map: Dict[str, Layer] = {}
        ordered_layers: List[Layer] = []

        for layer in target_layers:
            if layer.type == LayerType.BASE:
                continue
            if layer.type in (LayerType.APT, LayerType.YUM, LayerType.PIP):
                item = f"{layer.type.value}:{layer.content}"
                target_set.add(item)
                target_layer_map[item] = layer
            elif layer.type == LayerType.SCRIPT:
                item = f"script:{layer.name}"
                target_set.add(item)
                target_layer_map[item] = layer
            ordered_layers.append(layer)

        logging.info(f"📋 Target items: {len(target_set)}")

        # Gather candidate images
        candidates = self._list_local_images(preferred_repo, required_tag_prefix)
        logging.info(f"🐳 Candidates: {len(candidates)}")

        best_image: Optional[str] = None
        best_intersection: Set[str] = set()
        best_missing: Set[str] = set()
        best_score = float('-inf')
        best_extra: Set[str] = set()

        for image_tag in candidates:
            pkg_list = self._read_packages_from_image_metadata(image_tag)
            if not pkg_list:
                continue
            cached_set = set(pkg_list)
            inter = target_set & cached_set
            missing = target_set - cached_set
            extra = cached_set - target_set

            score = len(inter) * 100 - len(missing) * 50 - len(extra) * 0.01
            if len(missing) == 0:
                score += 10000

            if score > best_score:
                best_score = score
                best_image = image_tag
                best_intersection = inter
                best_missing = missing
                best_extra = extra

        reused_layer_names: Set[str] = set()
        layers_to_build: List[Layer] = []
        cleanup_commands: List[Dict] = []

        if best_image and best_intersection:
            logging.info(f"✅ Best base: {best_image} (reuse {len(best_intersection)})")
            if best_extra:
                cleanup_commands = self.generate_cleanup_commands(best_extra)

            if len(best_missing) == 0:
                # Everything present; only configs need rebuild
                for layer in ordered_layers:
                    if layer.type == LayerType.CONFIG:
                        layers_to_build.append(layer)
                    elif layer.type in (LayerType.APT, LayerType.YUM, LayerType.PIP, LayerType.SCRIPT):
                        reused_layer_names.add(layer.name)
            else:
                for item in best_intersection:
                    if item in target_layer_map:
                        reused_layer_names.add(target_layer_map[item].name)
                for layer in ordered_layers:
                    if layer.type == LayerType.CONFIG:
                        layers_to_build.append(layer)
                    elif layer.type in (LayerType.APT, LayerType.YUM, LayerType.PIP):
                        if f"{layer.type.value}:{layer.content}" not in best_intersection:
                            layers_to_build.append(layer)
                    elif layer.type == LayerType.SCRIPT:
                        if f"script:{layer.name}" not in best_intersection:
                            layers_to_build.append(layer)
            return best_image, reused_layer_names, layers_to_build, cleanup_commands

        # Fallback: build from base image (first layer content) when no candidate works
        logging.info("❌ No suitable base found, building from scratch")
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

    def _read_packages_from_image_metadata(self, image_tag: str) -> List[str]:
        """Read dependency items from fixed metadata file inside the image via docker run."""
        try:
            cmd = sudo_prefix() + ['docker', 'run', '--rm', '--entrypoint', '/bin/cat', image_tag, IMAGE_DEP_METADATA_PATH]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            if result.returncode != 0:
                return []
            content = result.stdout.strip()
            if not content:
                return []
            return [line.strip() for line in content.split('\n') if line.strip()]
        except Exception:
            return []
