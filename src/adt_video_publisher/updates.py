"""Safe, throttled checks for newer public GitHub releases."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import __version__


LATEST_RELEASE_API = (
    "https://api.github.com/repos/adolfalfred/High2Min-Video-Compressor/releases/latest"
)
LATEST_RELEASE_PAGE = (
    "https://github.com/adolfalfred/High2Min-Video-Compressor/releases/latest"
)
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 1024 * 1024
_VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


class UpdateCheckError(RuntimeError):
    """Raised when a requested release check cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    """A newer stable release available to the current installation."""

    current_version: str
    latest_version: str
    release_url: str


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = _VERSION_PATTERN.fullmatch(value.strip())
    if not match:
        raise ValueError(f"Unsupported release version: {value!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _cache_file() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "High2Min Video Compressor" / "update-check.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "High2Min Video Compressor" / "update-check.json"
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "high2min-video-compressor" / "update-check.json"


def _was_checked_recently(cache_file: Path, now: float, interval: float) -> bool:
    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        checked_at = float(payload["checked_at"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False
    elapsed = now - checked_at
    return 0 <= elapsed < interval


def _record_successful_check(cache_file: Path, checked_at: float) -> None:
    temporary = cache_file.with_name(f".{cache_file.name}.{uuid.uuid4().hex}.tmp")
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps({"checked_at": checked_at}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, cache_file)
    except OSError:
        # A read-only profile must not turn an optional update check into an app failure.
        pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _safe_release_url(value: object) -> str:
    if not isinstance(value, str):
        return LATEST_RELEASE_PAGE
    parsed = urlparse(value)
    expected_path = "/adolfalfred/high2min-video-compressor/releases/"
    if (
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").casefold() == "github.com"
        and parsed.path.casefold().startswith(expected_path)
    ):
        return value
    return LATEST_RELEASE_PAGE


def check_for_update(
    *,
    current_version: str = __version__,
    force: bool = False,
    cache_file: Path | None = None,
    now: float | None = None,
    interval: float = CHECK_INTERVAL_SECONDS,
    opener: Callable[..., Any] = urlopen,
) -> UpdateInfo | None:
    """Return a newer release, or ``None`` when current/throttled.

    Network and payload failures raise :class:`UpdateCheckError`. The desktop UI
    deliberately suppresses those failures for automatic startup checks and shows
    them only when the user explicitly requests a check.
    """

    effective_now = time.time() if now is None else now
    effective_cache = _cache_file() if cache_file is None else cache_file
    if not force and _was_checked_recently(effective_cache, effective_now, interval):
        return None

    request = Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"High2Min-Video-Compressor/{current_version}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw_payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw_payload) > MAX_RESPONSE_BYTES:
            raise ValueError("GitHub response was unexpectedly large.")
        payload = json.loads(raw_payload.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("GitHub response was not an object.")
        tag_name = payload["tag_name"]
        if not isinstance(tag_name, str):
            raise ValueError("GitHub release tag was not text.")
        latest_tuple = _version_tuple(tag_name)
        current_tuple = _version_tuple(current_version)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise UpdateCheckError(
            "The latest release could not be checked. Confirm the internet connection and try again."
        ) from exc

    _record_successful_check(effective_cache, effective_now)
    if latest_tuple <= current_tuple:
        return None
    return UpdateInfo(
        current_version=current_version.lstrip("v"),
        latest_version=tag_name.lstrip("v"),
        release_url=_safe_release_url(payload.get("html_url")),
    )
