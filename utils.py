from typing import List
import os
import shutil
import subprocess


def _can_run(cmd: list) -> bool:
    try:
        # Use a short timeout to avoid hanging
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


def sudo_prefix() -> List[str]:
    """Choose whether to use sudo for Docker commands.

    Strategy:
    - If NO_SUDO=1 is set, never use sudo.
    - If running as root, don't use sudo.
    - If current user can talk to Docker daemon without sudo, don't use sudo.
    - If sudo is available and can run non-interactively, use ['sudo', '-E'].
    - Otherwise, don't use sudo (caller will likely see a Docker permission error).
    """
    # Explicit override to disable sudo (useful in restricted sandboxes)
    if os.environ.get("NO_SUDO", "").strip() in ("1", "true", "True"):
        return []

    try:
        if os.geteuid() == 0:
            return []
    except Exception:
        # If we can't determine EUID, continue with best-effort checks
        pass

    # If docker works without sudo, prefer that
    if shutil.which("docker") and _can_run(["docker", "info"]):
        return []

    # If sudo exists and can run non-interactively, prefer sudo -E
    if shutil.which("sudo") and _can_run(["sudo", "-n", "true"]):
        # Use non-interactive sudo and preserve env
        return ["sudo", "-n", "-E"]

    # Fall back to no sudo; callers will surface a helpful error from Docker
    return []
