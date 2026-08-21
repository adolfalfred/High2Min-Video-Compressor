"""Windowed frozen entry point with durable startup diagnostics."""

from __future__ import annotations

import faulthandler
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path


def _log_directory() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "High2Min Video Compressor" / "Logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "High2Min Video Compressor"
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "high2min" / "logs"


def _show_error(message: str) -> None:
    try:
        from tkinter import messagebox

        messagebox.showerror("High2Min Video Compressor", message)
    except Exception:
        # The log remains available even when Tk cannot construct a dialog.
        return


def main() -> int:
    log_directory = _log_directory()
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / "startup.log"
    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        if sys.stdout is None:
            sys.stdout = log
        if sys.stderr is None:
            sys.stderr = log
        try:
            faulthandler.enable(log)
        except (OSError, RuntimeError):
            pass
        log.write(
            f"\n[{datetime.now(timezone.utc).isoformat()}] "
            f"High2Min desktop start; platform={sys.platform}\n"
        )
        try:
            from adt_video_publisher.desktop import run, smoke_test

            result = smoke_test() if sys.argv[1:] == ["--smoke-test"] else run()
            if result != 0:
                _show_error(
                    "The desktop interface could not start. "
                    f"Diagnostic details were saved to:\n{log_path}"
                )
            return result
        except BaseException:
            traceback.print_exc(file=log)
            _show_error(
                "High2Min Video Compressor encountered a startup error. "
                f"Diagnostic details were saved to:\n{log_path}"
            )
            return 70


if __name__ == "__main__":
    raise SystemExit(main())
