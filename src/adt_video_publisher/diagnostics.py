"""Cross-platform locations and durable JSON-lines diagnostics."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO


def application_log_directory() -> Path:
    """Return the user's normal High2Min log directory for this platform."""

    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "High2Min Video Compressor" / "Logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "High2Min Video Compressor"
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "high2min" / "logs"


def new_publish_log_path(job_id: str) -> Path:
    """Allocate a unique, user-visible path without creating the file yet."""

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return application_log_directory() / f"publish-{timestamp}-{job_id[:8]}.jsonl"


class DiagnosticLog:
    """Line-buffered diagnostic stream that never redirects process output."""

    def __init__(self, path: str | os.PathLike[str] | None) -> None:
        self.path = Path(path).expanduser().resolve(strict=False) if path else None
        self._stream: TextIO | None = None

    def __enter__(self) -> "DiagnosticLog":
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = self.path.open("a", encoding="utf-8", buffering=1)
        return self

    def write(self, event: str, payload: dict[str, object]) -> None:
        if self.path is None:
            return
        document = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "event": event,
            "payload": payload,
        }
        line = json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            if self._stream is not None:
                self._stream.write(line)
                self._stream.flush()
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line)
        except OSError:
            # Diagnostic storage must never prevent the actual user operation.
            self.path = None

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
