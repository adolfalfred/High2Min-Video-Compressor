"""Safe parsing and cache-version handling for ADT video URL references."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
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


@dataclass(frozen=True, slots=True)
class BookVideoReference:
    """An existing browser mapping resolved to a file inside an ADT book."""

    value: str
    filename: str
    root_relative: str
    query: str
    fragment: str

    @property
    def has_cache_version(self) -> bool:
        return any(
            unquote(part.partition("=")[0]).casefold() == "v"
            for part in self.query.split("&")
            if part
        )


def _video_reference_parts(value: object) -> tuple[str, str, str, str]:
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
    if (
        not decoded_path
        or PurePosixPath(decoded_path).is_absolute()
        or any(ord(character) < 32 or ord(character) == 127 for character in decoded_path)
        or any(character in '<>:"|?*' for character in decoded_path)
        or "\\" in decoded_path
        or PurePosixPath(decoded_path).suffix.casefold() != ".mp4"
    ):
        raise ValueError("video reference must resolve to a local MP4 file")
    return value, decoded_path, parts.query, parts.fragment


def parse_video_reference(value: object) -> VideoReference:
    """Parse one safe local MP4 filename without treating its query as a filename."""

    original, decoded_path, query, fragment = _video_reference_parts(value)
    path = PurePosixPath(decoded_path)
    if (
        path.name != decoded_path
        or any(part in {"", ".", ".."} for part in path.parts)
        or "/" in decoded_path
    ):
        raise ValueError("video reference must resolve to one local MP4 filename")
    return VideoReference(
        value=original,
        filename=decoded_path,
        query=query,
        fragment=fragment,
    )


def resolve_book_video_reference(
    value: object,
    *,
    book: Path,
    language: str,
) -> BookVideoReference:
    """Resolve an existing ADT mapping while confining it to the selected book."""

    original, decoded_path, query, fragment = _video_reference_parts(value)
    language_path = PurePosixPath(language)
    if (
        language_path.is_absolute()
        or len(language_path.parts) != 1
        or language_path.name in {"", ".", ".."}
    ):
        raise ValueError("language must be one safe directory name")
    base = PurePosixPath("content", "i18n", language, "video")
    normalized_text = posixpath.normpath(f"{base.as_posix()}/{decoded_path}")
    normalized = PurePosixPath(normalized_text)
    if normalized.is_absolute() or not normalized.parts or normalized.parts[0] == "..":
        raise ValueError("video reference escapes the selected ADT book")
    root = book.resolve()
    candidate = (root / Path(*normalized.parts)).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("video reference escapes the selected ADT book") from exc
    return BookVideoReference(
        value=original,
        filename=PurePosixPath(decoded_path).name,
        root_relative=normalized.as_posix(),
        query=query,
        fragment=fragment,
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
    if existing_reference is None:
        existing = None
    else:
        original, decoded_path, query, fragment = _video_reference_parts(existing_reference)
        existing = VideoReference(
            value=original,
            filename=PurePosixPath(decoded_path).name,
            query=query,
            fragment=fragment,
        )
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
