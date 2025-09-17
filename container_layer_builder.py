import os
import shlex
import string
import subprocess
import uuid
import tempfile
import shutil
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

        def sanitize(value: str) -> str:
            allowed = string.ascii_letters + string.digits + '._-'
            sanitized = ''.join(ch if ch in allowed else '_' for ch in value)
            sanitized = sanitized.strip('._-')
            if not sanitized:
                sanitized = 'layer'
            if sanitized[0] not in string.ascii_letters + string.digits:
                sanitized = f'layer_{sanitized}'
            return sanitized

        safe = sanitize(layer_name)
        return f"depimg_{safe}_{suffix}"

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
            cid_dir = tempfile.mkdtemp(prefix='depimg_cid_')
            cid_path = os.path.join(cid_dir, 'cid')
            try:
                res = self._docker(['create'] + self._env_args() + ['--cidfile', cid_path, '--name', name, parent_image] + idle_cmd, env=env)
                if res.returncode != 0:
                    raise RuntimeError(f"docker create failed with exit code {res.returncode}; check docker output above")
                with open(cid_path, 'r', encoding='utf-8') as f:
                    container = f.read().strip()
                if not container:
                    raise RuntimeError("docker create did not write a container id; check docker output above")
            finally:
                shutil.rmtree(cid_dir, ignore_errors=True)
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

            # Always flatten snapshot via export/import (no docker build, no multi-layer commit)
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
                        IMAGE_LABEL_ITEMS_B64: payload_b64,
                    }
                    parts = [f"{k}={v}" for k, v in labels.items()]
                    change_args = ['--change', 'LABEL ' + ' '.join(parts)]
                except Exception as _e:
                    print(f"⚠️  Failed to prepare label metadata: {_e}")
                    change_args = []

            print("   Flattening snapshot via docker export/import...")
            with tempfile.NamedTemporaryFile(prefix='depimg_', suffix='.tar', delete=False) as tf:
                tar_path = tf.name
            exp = self._docker(['export', '-o', tar_path, container])
            if exp.returncode != 0:
                # Fallback to streaming export without -o
                with open(tar_path, 'wb') as f:
                    proc = subprocess.Popen(sudo_prefix() + ['docker', 'export', container], stdout=f)
                    proc.wait()
                    if proc.returncode != 0:
                        raise RuntimeError("docker export failed")
            # Import with labels
            import_args = ['import']
            if change_args:
                import_args += change_args
            import_args += [tar_path, image_tag]
            imp = self._docker(import_args)
            if imp.returncode != 0:
                raise RuntimeError(f"docker import failed with exit code {imp.returncode}; check docker output above")
            print("   Snapshot flatten succeeded")
            return image_tag
        finally:
            if container:
                # Stop container if running
                self._docker(['stop', container])
                if not self.preserve_on_failure:
                    self._docker(['rm', '-f', container])
