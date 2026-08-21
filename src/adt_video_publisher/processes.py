"""Cross-platform subprocess options for background media tools."""

from __future__ import annotations

import subprocess
import sys


def hidden_process_options() -> dict[str, int]:
    """Prevent console-subsystem child processes from flashing a window on Windows."""

    if sys.platform != "win32":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
