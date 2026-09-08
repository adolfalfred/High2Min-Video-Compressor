"""Safe parsing and cache-version handling for ADT video URL references."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlsplit


_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


@dataclass(frozen=True, slots=True)
class VideoReference:
    """A browser-facing mapping and the local filename it resolves to."""

    value: str
    filename: str
    query: str
    fragment: str

    @property
    def has_cache_version(self) -> bool:
        return any(
            unquote(part.partition("=")[0]).casefold() == "v"
            for part in self.query.split("&")
            if part
        )


def parse_video_reference(value: object) -> VideoReference:
    """Parse one safe local MP4 URL reference without treating its query as a filename."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("video reference must be a non-empty trimmed string")
    if "\\" in value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("video reference contains a backslash or control character")
    if _INVALID_PERCENT_ESCAPE.search(value):
        raise ValueError("video reference contains an invalid percent escape")
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise ValueError("video reference is not a valid URL reference") from exc
    if parts.scheme or parts.netloc:
        raise ValueError("video reference must be local")
    decoded_path = unquote(parts.path)
    path = PurePosixPath(decoded_path)
    if (
        not decoded_path
        or path.is_absolute()
        or path.name != decoded_path
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in decoded_path)
        or any(character in '<>:"|?*' for character in decoded_path)
        or "/" in decoded_path
        or "\\" in decoded_path
        or path.suffix.casefold() != ".mp4"
    ):
        raise ValueError("video reference must resolve to one local MP4 filename")
    return VideoReference(
        value=value,
        filename=decoded_path,
        query=parts.query,
        fragment=parts.fragment,
    )


def versioned_video_reference(
    filename: str,
    *,
    cache_version: str,
    existing_reference: str | None = None,
    inherit_cache_version: bool = False,
) -> str:
    """Build a replacement mapping while preserving its authored URL suffix semantics."""

    canonical = parse_video_reference(filename)
    existing = parse_video_reference(existing_reference) if existing_reference is not None else None
    query_parts = existing.query.split("&") if existing and existing.query else []
    should_version = inherit_cache_version or bool(existing and existing.has_cache_version)
    replaced = False
    if should_version:
        encoded_version = quote(str(cache_version), safe="-._~")
        for index, part in enumerate(query_parts):
            key, _separator, _value = part.partition("=")
            if unquote(key).casefold() == "v":
                query_parts[index] = f"{key}={encoded_version}"
                replaced = True
        if not replaced:
            query_parts.append(f"v={encoded_version}")
    query = f"?{'&'.join(query_parts)}" if query_parts else ""
    fragment = f"#{existing.fragment}" if existing and existing.fragment else ""
    return f"{canonical.filename}{query}{fragment}"
