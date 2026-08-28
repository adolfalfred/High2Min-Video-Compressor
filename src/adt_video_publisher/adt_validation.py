"""Semantic and browser-resource validation for generated ADT publications."""

from __future__ import annotations

import copy
import json
import posixpath
import re
from pathlib import Path, PurePosixPath
from typing import Final
from urllib.parse import unquote, urlsplit

from .adt_adapters import (
    TextDocument,
    inject_adapters_into_html,
    validate_adapter_order,
)
from .adt_planning import APPROVED_HELPERS, AdtPublishPlan
from .errors import PublishFailedError

LOCAL_REFERENCE_PATTERN: Final = re.compile(
    r"\b(?:src|href)\s*=\s*(['\"])(?P<value>[^'\"]+)\1", re.IGNORECASE
)
RUNTIME_REFERENCE_PATTERN: Final = re.compile(
    r'(?P<path>(?:\.\./|\./)*assets/base\.bundle(?:\.[A-Za-z0-9_-]+)*\.js)'
    r'(?P<query>\?[^#"\'<>\s]*)?(?P<fragment>#[^"\'<>\s]*)?'
)
OFFLINE_REFERENCE_PATTERN: Final = re.compile(
    r'(?P<path>(?:\.\./|\./)*assets/offline-preloader\.js)'
    r'(?P<query>\?[^#"\'<>\s]*)?(?P<fragment>#[^"\'<>\s]*)?'
)


def _version_reference(match: re.Match[str], version: str) -> str:
    query = (match.group("query") or "").lstrip("?")
    parts = query.split("&") if query else []
    found = False
    for index, part in enumerate(parts):
        if part.split("=", 1)[0] == "v":
            parts[index] = f"v={version}"
            found = True
    if not found:
        parts.append(f"v={version}")
    return f"{match.group('path')}?{'&'.join(parts)}{match.group('fragment') or ''}"


def expected_published_html(
    source: str,
    *,
    page_href: str,
    active_runtime_files: tuple[str, ...],
    cache_version: str,
    newline: str,
) -> str:
    updated = inject_adapters_into_html(
        source,
        page_href=page_href,
        active_runtime_files=active_runtime_files,
        newline=newline,
    )
    updated = OFFLINE_REFERENCE_PATTERN.sub(
        lambda match: _version_reference(match, cache_version), updated
    )
    return RUNTIME_REFERENCE_PATTERN.sub(
        lambda match: _version_reference(match, cache_version), updated
    )


def _json_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublishFailedError(f"{label} is invalid JSON: '{path}'.") from exc
    if not isinstance(value, dict):
        raise PublishFailedError(f"{label} must contain an object.")
    return value


def _inline_json_span(source: str) -> tuple[int, int]:
    assignment = re.search(r"\b(?:var|let|const)\s+INLINE\s*=\s*", source)
    if assignment is None:
        raise PublishFailedError("Offline preloader has no INLINE resource map.")
    start = assignment.end()
    while start < len(source) and source[start].isspace():
        start += 1
    if start >= len(source) or source[start] != "{":
        raise PublishFailedError("Offline preloader INLINE map does not start with an object.")
    depth = 0
    quoted = False
    escaped = False
    for position in range(start, len(source)):
        character = source[position]
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            continue
        if character == '"':
            quoted = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return start, position + 1
    raise PublishFailedError("Offline preloader INLINE map is incomplete.")


def _overlay_path(source: Path, generated: Path, relative: str) -> Path:
    path = Path(*PurePosixPath(relative).parts)
    staged = generated / path
    return staged if staged.is_file() else source / path


def _validate_local_references(
    source: Path,
    generated: Path,
    page_hrefs: tuple[str, ...],
) -> None:
    for href in page_hrefs:
        page = _overlay_path(source, generated, href)
        html = TextDocument.read(page).text
        for match in LOCAL_REFERENCE_PATTERN.finditer(html):
            value = match.group("value").strip()
            lowered = value.casefold()
            if not value or lowered.startswith(("http:", "https:", "data:", "mailto:", "tel:", "javascript:", "#")):
                continue
            parts = urlsplit(value)
            if not parts.path:
                continue
            raw = unquote(parts.path)
            if raw.startswith("/"):
                relative = posixpath.normpath(raw.lstrip("/"))
            else:
                parent = PurePosixPath(href).parent.as_posix()
                relative = posixpath.normpath(posixpath.join(parent, raw))
            if relative == ".." or relative.startswith("../"):
                raise PublishFailedError(f"Page '{href}' contains an escaping local reference: '{value}'.")
            if not _overlay_path(source, generated, relative).is_file():
                raise PublishFailedError(f"Page '{href}' references missing local file '{relative}'.")


def _validate_javascript_structure(path: Path) -> None:
    source = TextDocument.read(path).text
    if "\x00" in source or "function" not in source:
        raise PublishFailedError(f"Generated JavaScript is structurally incomplete: '{path}'.")
    for opening, closing in (("{", "}"), ("(", ")"), ("[", "]")):
        if source.count(opening) != source.count(closing):
            raise PublishFailedError(f"Generated JavaScript has unbalanced delimiters: '{path}'.")


def validate_staged_diff_contract(
    source: Path,
    generated: Path,
    *,
    plan: AdtPublishPlan,
    cache_version: str,
) -> None:
    """Prove that staging changed only the planned video-integration surface."""

    allowed = set(plan.mutations)
    for path in generated.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(generated).as_posix()
        source_path = source / Path(*PurePosixPath(relative).parts)
        changed = not source_path.is_file() or path.read_bytes() != source_path.read_bytes()
        if changed and relative not in allowed:
            raise PublishFailedError(f"Staging changed a file outside the approved publish plan: '{relative}'.")

    source_config = _json_object(source / "assets" / "config.json", "Source config")
    generated_config = _json_object(generated / "assets" / "config.json", "Generated config")
    source_allowed = copy.deepcopy(source_config)
    generated_allowed = copy.deepcopy(generated_config)
    for value in (source_allowed, generated_allowed):
        value.pop("bundleVersion", None)
        value.pop("high2minCacheVersion", None)
        features = value.get("features")
        if isinstance(features, dict):
            features.pop("signLanguage", None)
            features.pop("readAloud", None)
    if source_allowed != generated_allowed:
        raise PublishFailedError("Publishing changed unrelated assets/config.json settings.")

    for runtime in plan.active_runtime_files:
        if _overlay_path(source, generated, runtime).read_bytes() != (
            source / Path(*PurePosixPath(runtime).parts)
        ).read_bytes():
            raise PublishFailedError(f"Publishing modified the authored runtime bundle '{runtime}'.")
    for relative in APPROVED_HELPERS.values():
        source_helper = source / Path(*PurePosixPath(relative).parts)
        generated_helper = generated / Path(*PurePosixPath(relative).parts)
        if source_helper.is_file() and generated_helper.read_bytes() != source_helper.read_bytes():
            raise PublishFailedError(f"Publishing modified an existing compatibility helper '{relative}'.")

    for href in plan.page_hrefs:
        relative = Path(*PurePosixPath(href).parts)
        source_document = TextDocument.read(source / relative)
        generated_path = generated / relative
        if not generated_path.is_file():
            raise PublishFailedError(f"Generated page is missing from staging: '{href}'.")
        expected = expected_published_html(
            source_document.text,
            page_href=href,
            active_runtime_files=plan.active_runtime_files,
            cache_version=cache_version,
            newline=source_document.newline,
        )
        if generated_path.read_bytes() != source_document.encode(expected):
            raise PublishFailedError(f"Page '{href}' changed outside approved helper and cache references.")

    source_preloader = source / "assets" / "offline-preloader.js"
    generated_preloader = generated / "assets" / "offline-preloader.js"
    if source_preloader.is_file() and generated_preloader.is_file():
        source_text = TextDocument.read(source_preloader).text
        generated_text = TextDocument.read(generated_preloader).text
        source_start, source_end = _inline_json_span(source_text)
        generated_start, generated_end = _inline_json_span(generated_text)
        if source_text[:source_start] != generated_text[:generated_start] or source_text[source_end:] != generated_text[generated_end:]:
            raise PublishFailedError("Offline preloader changed outside its generated INLINE map.")

    _validate_local_references(source, generated, plan.page_hrefs)
    validate_adapter_order(
        generated,
        page_hrefs=plan.page_hrefs,
        active_runtime_files=plan.active_runtime_files,
    )
    for relative in APPROVED_HELPERS.values():
        _validate_javascript_structure(generated / relative) if relative.endswith(".js") else None


def _validate_generated_site_overlay(
    source_book: Path,
    generated_book: Path,
    *,
    language: str,
    page_hrefs: tuple[str, ...],
    active_runtime_files: tuple[str, ...],
) -> None:
    """Validate a generated site, resolving unchanged files from an optional source overlay."""

    _validate_local_references(source_book, generated_book, page_hrefs)
    validate_adapter_order(
        generated_book,
        page_hrefs=page_hrefs,
        active_runtime_files=active_runtime_files,
    )
    for relative in APPROVED_HELPERS.values():
        if relative.endswith(".js"):
            _validate_javascript_structure(
                _overlay_path(source_book, generated_book, relative)
            )
    preloader = _overlay_path(
        source_book,
        generated_book,
        "assets/offline-preloader.js",
    )
    if preloader.is_file():
        source = TextDocument.read(preloader).text
        start, end = _inline_json_span(source)
        try:
            inline = json.loads(source[start:end])
        except json.JSONDecodeError as exc:
            raise PublishFailedError("Offline preloader INLINE map is invalid JSON.") from exc
        if not isinstance(inline, dict):
            raise PublishFailedError("Offline preloader INLINE map must contain an object.")
        expected = {
            "./assets/config.json": _json_object(
                _overlay_path(source_book, generated_book, "assets/config.json"),
                "Config",
            ),
            f"./content/i18n/{language}/videos.json": _json_object(
                _overlay_path(
                    source_book,
                    generated_book,
                    f"content/i18n/{language}/videos.json",
                ),
                "Video mappings",
            ),
        }
        for key, value in expected.items():
            if inline.get(key) != value:
                raise PublishFailedError(f"Offline preloader has stale embedded value for '{key}'.")
        for key, value in inline.items():
            if not isinstance(key, str) or not key.endswith(".html") or not isinstance(value, str):
                continue
            relative = key.removeprefix("./")
            path = _overlay_path(source_book, generated_book, relative)
            if path.is_file() and value != TextDocument.read(path).text:
                raise PublishFailedError(f"Offline preloader has stale embedded HTML for '{key}'.")


def validate_staged_generated_site(
    source_book: Path,
    generated_book: Path,
    *,
    language: str,
    page_hrefs: tuple[str, ...],
    active_runtime_files: tuple[str, ...],
) -> None:
    """Validate a minimal in-place staging overlay before repository files are replaced."""

    _validate_generated_site_overlay(
        source_book,
        generated_book,
        language=language,
        page_hrefs=page_hrefs,
        active_runtime_files=active_runtime_files,
    )


def validate_generated_site(
    book: Path,
    *,
    language: str,
    page_hrefs: tuple[str, ...],
    active_runtime_files: tuple[str, ...],
) -> None:
    """Validate local references, helper order, JS structure, and offline embedded data."""

    _validate_generated_site_overlay(
        book,
        book,
        language=language,
        page_hrefs=page_hrefs,
        active_runtime_files=active_runtime_files,
    )
