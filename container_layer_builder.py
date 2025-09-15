import os
import shlex
import subprocess
import uuid
import tempfile
from typing import Dict, List, Optional

from config import (
    IMAGE_DEP_METADATA_PATH,  # legacy path (no longer written)
    Layer,
    LayerType,
    IMAGE_LABEL_VERSION,
    IMAGE_LABEL_CACHE_KEY,
    IMAGE_LABEL_CREATED,
    IMAGE_LABEL_ITEMS,
    IMAGE_LABEL_ITEMS_B64,
)
from utils import sudo_prefix


class ContainerLayerBuilder:
    """Builds layers by running commands inside a container and committing snapshots.

    - Starts from a parent image
    - Creates a short‑lived container
    - Copies files (for script installs with copies or file: entries)
    - Executes commands inside the container
    - Attaches dependency metadata as image labels if provided (no filesystem writes)
    - Commits the container to a new image tag
    """

    def __init__(self, env_vars: Dict[str, str], config_dir: Optional[str] = None, preserve_on_failure: bool = True):
        self.env_vars = env_vars or {}
        self.config_dir = config_dir or os.getcwd()
        self.preserve_on_failure = preserve_on_failure
        self.last_container_name: Optional[str] = None
        self.last_container_id: Optional[str] = None
        self.last_failed_cmd: Optional[str] = None

    def _docker(self, args: List[str], env: Optional[Dict[str, str]] = None, capture: bool = False, cwd: Optional[str] = None, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        cmd = sudo_prefix() + ['docker'] + args
        if capture:
            return subprocess.run(cmd, env=env, cwd=cwd, text=True, capture_output=True, timeout=timeout)
        return subprocess.run(cmd, env=env, cwd=cwd, timeout=timeout)

    def _container_name(self, layer_name: str) -> str:
        suffix = uuid.uuid4().hex[:8]
        return f"depimg_{layer_name}_{suffix}"

    def _env_args(self) -> List[str]:
        args: List[str] = []
        for k, v in self.env_vars.items():
            # Avoid injecting empty values
            if v is None:
                continue
            args += ['-e', f'{k}={v}']
        return args

    def _ensure_shell(self, image: str) -> str:
        """Return preferred shell path available in the image."""
        # Prefer bash, fallback to sh
        # We can't probe image filesystem without running, so choose bash for common bases
        return '/bin/bash'

    def _copy_into(self, container: str, src_abs: str, dst_in_container: str):
        """Robust docker cp handling for files and directories.

        - If dst ends with '/', always treat as directory target.
        - If src is a directory and dst does NOT end with '/', copy the directory contents into dst
          (i.e., behave like `cp -a src/. dst/`) so the dst path becomes the directory itself,
          not `dst/src_basename`.
        """
        os.makedirs(os.path.dirname(src_abs), exist_ok=True)
        src_is_dir = os.path.isdir(src_abs)
        cp_src = src_abs
        if src_is_dir and not dst_in_container.endswith('/'):
            # Copy directory contents directly into the exact dst path
            cp_src = os.path.join(src_abs, '.')
        self._docker(['cp', cp_src, f'{container}:{dst_in_container}'])

    def _exec(self, container: str, command: str) -> int:
        shell = self._ensure_shell("")
        # Allocate TTY by default to reduce buffering; keep STDIN open
        exec_args = ['exec', '-i', '-t'] + self._env_args() + [container, shell, '-lc', command]
        print(f">>> Running in {container}: {command}")
        rc = self._docker(exec_args).returncode
        if rc != 0:
            self.last_failed_cmd = command
        return rc

    def _exec_multi(self, container: str, commands: List[str]):
        for idx, cmd in enumerate(commands, 1):
            rc = self._exec(container, cmd)
            if rc != 0:
                raise RuntimeError(f"Command failed (#{idx}): {cmd}")

    def build_layer(self, layer: Layer, parent_image: str, image_tag: str, *, copies: Optional[List[str]] = None, metadata_items: Optional[List[str]] = None) -> str:
        """Build a layer by mutating a container from parent_image and committing to image_tag.

        copies: list of "src:dst" mappings (src relative to config_dir)
        metadata_items: dependency items to attach as image labels before commit
        """
        container = None
        created = False
        try:
            name = self._container_name(layer.name)
            # Create a paused/idle container
            idle_cmd = ['bash', '-lc', 'while sleep 3600; do :; done']
            env = os.environ.copy()
            # Enable buildkit for any nested docker, though we don't run nested docker here
            env.setdefault('DOCKER_BUILDKIT', '1')
            env.setdefault('BUILDKIT_PROGRESS', 'plain')
            res = self._docker(['create'] + self._env_args() + ['--name', name, parent_image] + idle_cmd, env=env, capture=True)
            if res.returncode != 0:
                raise RuntimeError(f"docker create failed: {res.stderr}")
            container = res.stdout.strip()
            self.last_container_name = name
            self.last_container_id = container
            created = True

            # Perform copies (src:dst) — enforce precise directory semantics by stripping trailing '/'
            for mapping in (copies or []):
                src, dst = mapping.split(':', 1)
                src_abs = os.path.abspath(os.path.join(self.config_dir, src))
                # Sanitize destination: always precise path (no trailing '/')
                if dst != '/':
                    dst = dst.rstrip('/')
                # Ensure destination path semantics (precise copy)
                self._docker(['start', container])
                if os.path.isdir(src_abs):
                    # Directory source: create the exact target directory
                    self._exec(container, f"mkdir -p {shlex.quote(dst)}")
                else:
                    # File source: ensure parent dir exists
                    parent = os.path.dirname(dst) or '/'
                    self._exec(container, f"mkdir -p {shlex.quote(parent)}")
                self._docker(['stop', container])
                # Now copy with robust semantics
                self._copy_into(container, src_abs, dst)

            # Start the container for execution
            self._docker(['start', container])

            # Compute and run commands based on layer type (each as separate exec for real-time output)
            cmds: List[str] = []
            if layer.type in (LayerType.APT, LayerType.YUM, LayerType.PIP):
                pkg = layer.content
                if layer.type == LayerType.APT:
                    cmds.append('export DEBIAN_FRONTEND=noninteractive')
                    cmds.append('apt-get update')
                    cmds.append(f'apt-get install -y {shlex.quote(pkg)}')
                    cmds.append('rm -rf /var/lib/apt/lists/* || true')
                elif layer.type == LayerType.YUM:
                    if layer.name in ("yum_makecache", "yum_refresh"):
                        cmds.append('yum makecache')
                    else:
                        cmds.append(f'yum install -y {shlex.quote(pkg)}')
                else:  # pip
                    cmds.append(f'python3 -m pip install --no-cache-dir {shlex.quote(pkg)}')
            elif layer.type in (LayerType.SCRIPT, LayerType.CONFIG):
                # Split lines and handle file: entries
                lines = [ln for ln in layer.content.splitlines() if ln.strip()]
                runlines: List[str] = []
                for raw in lines:
                    cmd = raw.strip()
                    if cmd.startswith('file:'):
                        rel = cmd.split(':', 1)[1].strip()
                        base = os.path.basename(rel)
                        src_abs = os.path.abspath(os.path.join(self.config_dir, rel))
                        dst = f"/dependency_img_build/{base}"
                        # Ensure dir exists then copy
                        self._exec(container, f"mkdir -p /dependency_img_build && chmod 0777 /dependency_img_build")
                        self._docker(['stop', container])
                        self._copy_into(container, src_abs, dst)
                        self._docker(['start', container])
                        self._exec(container, f"chmod +x {shlex.quote(dst)}")
                        if base.endswith('.py'):
                            runlines.append(f"python3 {shlex.quote(dst)}")
                        else:
                            runlines.append(f"/bin/bash {shlex.quote(dst)}")
                    else:
                        runlines.append(cmd)
                if runlines:
                    cmds.append(' && '.join(runlines))
            else:
                # Unknown layer type; no-op
                pass

            if cmds:
                # Prepend 'set -e' via first command to ensure early exit semantics
                first = cmds[0]
                cmds[0] = f"set -e; {first}"
                self._exec_multi(container, cmds)

            # Prepare commit label changes if dependency metadata provided
            change_args: List[str] = []
            if metadata_items:
                try:
                    import hashlib, json as _json, base64, datetime as _dt
                    payload_json = _json.dumps(list(metadata_items), separators=(',', ':'))
                    payload_b64 = base64.b64encode(payload_json.encode('utf-8')).decode('ascii')
                    cache_key = hashlib.sha256("\n".join(metadata_items).encode('utf-8')).hexdigest()
                    created = _dt.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
                    labels = {
                        IMAGE_LABEL_VERSION: '1',
                        IMAGE_LABEL_CACHE_KEY: cache_key,
                        IMAGE_LABEL_CREATED: created,
                        # Prefer b64 to avoid quoting/newline issues; include plain json for readability if wanted
                        IMAGE_LABEL_ITEMS_B64: payload_b64,
                        # Keeping ITEMS as optional small/plain can be omitted if too large
                        # IMAGE_LABEL_ITEMS: payload_json,
                    }
                    # Build a single LABEL change with space-separated k=v pairs (values are b64/ASCII-safe)
                    parts = [f"{k}={v}" for k, v in labels.items()]
                    change_args = ['--change', 'LABEL ' + ' '.join(parts)]
                except Exception as _e:
                    # If label prep fails, proceed without labels (do not write legacy file)
                    print(f"⚠️  Failed to prepare label metadata: {_e}")
                    change_args = []

            # Commit snapshot (with label changes if any)
            res_commit = self._docker(['commit'] + change_args + [container, image_tag], capture=True)
            if res_commit.returncode != 0:
                err = (res_commit.stderr or '').strip()
                print(f"⚠️  docker commit failed: {err}")
                # Fallback: flatten via export/import to avoid deep layer issues (e.g., 'max depth exceeded')
                try:
                    # If depth exceeded, proactively remove the parent image to force future rebuild
                    if 'max depth exceeded' in err.lower():
                        print(f"   Depth exceeded detected; removing parent image to force base rebuild: {parent_image}")
                        self._docker(['rmi', '-f', parent_image], capture=True)
                    print("   Trying fallback: docker export/import to flatten image layers...")
                    # Export
                    with tempfile.NamedTemporaryFile(prefix='depimg_', suffix='.tar', delete=False) as tf:
                        tar_path = tf.name
                    # Prefer 'docker export -o'
                    exp = self._docker(['export', '-o', tar_path, container], capture=True)
                    if exp.returncode != 0:
                        # Fallback to streaming export without -o via shell redirection using Python
                        with open(tar_path, 'wb') as f:
                            proc = subprocess.Popen(sudo_prefix() + ['docker', 'export', container], stdout=f)
                            proc.wait()
                            if proc.returncode != 0:
                                raise RuntimeError("docker export failed")
                    # Import
                    # Apply label changes on import as well, if available
                    import_args = ['import']
                    if change_args:
                        # change_args looks like ['--change', 'LABEL ...']; docker import accepts the same
                        import_args += change_args
                    import_args += [tar_path, image_tag]
                    imp = self._docker(import_args, capture=True)
                    if imp.returncode != 0:
                        raise RuntimeError(f"docker import failed: {imp.stderr}")
                    print("   Fallback import succeeded")
                    return image_tag
                except Exception as fe:
                    raise RuntimeError(f"docker commit failed and fallback import failed: {fe}")
            return image_tag
        finally:
            if container:
                # Stop container if running
                self._docker(['stop', container])
                if not self.preserve_on_failure:
                    self._docker(['rm', '-f', container])
