from typing import List
import os


def sudo_prefix() -> List[str]:
    """Return the unified sudo prefix policy for this project.

    Rules (keep it simple, no fallbacks):
    - If running as root (euid == 0): return []
    - Otherwise: always return ["sudo", "-E"]

    Rationale:
    - Avoids environment-driven switches and probing logic that cause divergent paths.
    - Callers surface any permission issues directly to the user instead of silently
      attempting alternate modes.
    """
    try:
        return [] if os.geteuid() == 0 else ["sudo", "-E"]
    except Exception:
        # If EUID cannot be determined, default to requiring sudo with env preserved.
        return ["sudo", "-E"]
