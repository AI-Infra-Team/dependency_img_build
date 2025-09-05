from typing import List
import os


def sudo_prefix() -> List[str]:
    """Return sudo prefix if not running as root (preserve env)."""
    try:
        if os.geteuid() != 0:
            return ['sudo', '-E']
    except Exception:
        pass
    return []

