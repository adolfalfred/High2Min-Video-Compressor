"""Targeted ADT accessibility adapters that never rewrite authored runtime bundles."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Final

from .adt_planning import APPROVED_HELPERS
from .errors import PublishFailedError

SCRIPT_TAG_PATTERN: Final = re.compile(
    r"(?P<tag><script\b[^>]*\bsrc\s*=\s*(['\"])(?P<src>[^'\"]+)\2[^>]*>\s*</script>)",
    re.IGNORECASE,
)
STYLE_TAG_PATTERN: Final = re.compile(
    r"(?P<tag><link\b[^>]*\bhref\s*=\s*(['\"])(?P<href>[^'\"]+)\2[^>]*>)",
    re.IGNORECASE,
)
RUNTIME_NAME_PATTERN: Final = re.compile(r"^base\.bundle(?:\.[A-Za-z0-9_-]+)*\.js$")


@dataclass(frozen=True, slots=True)
class TextDocument:
    text: str
    bom: bool
    newline: str

    @classmethod
    def read(cls, path: Path) -> TextDocument:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise PublishFailedError(f"ADT text file is unreadable: '{path}'.") from exc
        bom = data.startswith(b"\xef\xbb\xbf")
        payload = data[3:] if bom else data
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PublishFailedError(f"ADT text file is not UTF-8: '{path}'.") from exc
        newline = "\r\n" if text.count("\r\n") > text.count("\n") - text.count("\r\n") else "\n"
        return cls(text=text, bom=bom, newline=newline)

    def encode(self, text: str) -> bytes:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        payload = normalized.replace("\n", self.newline).encode("utf-8")
        return (b"\xef\xbb\xbf" + payload) if self.bom else payload


def _path_without_query(value: str) -> str:
    return value.split("?", 1)[0].split("#", 1)[0].lstrip("./")


def _asset_url(page_href: str, asset_relative: str) -> str:
    page_parent = PurePosixPath(page_href).parent
    depth = len(page_parent.parts) if page_parent != PurePosixPath(".") else 0
    prefix = "../" * depth if depth else "./"
    return prefix + asset_relative


def _runtime_static_capability(path: Path) -> tuple[bool, str]:
    try:
        source = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise PublishFailedError(f"Active ADT runtime is unreadable: '{path}'.") from exc
    if not RUNTIME_NAME_PATTERN.fullmatch(path.name):
        return False, "the active script is not an ADT base.bundle runtime"
    if "signLanguage" not in source or "readAloud" not in source:
        return False, "the active runtime does not expose both signLanguage and readAloud capabilities"
    return True, "static ADT accessibility capabilities detected"


def verify_active_runtimes(book: Path, active_runtime_files: tuple[str, ...]) -> dict[str, str]:
    """Reject unknown/incomplete runtimes without modifying them."""

    if not active_runtime_files:
        raise PublishFailedError(
            "No active ADT runtime is referenced by the pages. Run publish-plan for details."
        )
    result: dict[str, str] = {}
    for relative in active_runtime_files:
        path = book / Path(*PurePosixPath(relative).parts)
        if not path.is_file():
            raise PublishFailedError(f"Active ADT runtime is missing: '{relative}'.")
        supported, reason = _runtime_static_capability(path)
        if not supported:
            raise PublishFailedError(
                f"Runtime compatibility blocker for '{relative}': {reason}. "
                "High2Min did not rewrite the runtime or website."
            )
        result[relative] = reason
    return result


def _remove_helper_tags(source: str) -> str:
    helper_names = {PurePosixPath(value).name.casefold() for value in APPROVED_HELPERS.values()}
    source = SCRIPT_TAG_PATTERN.sub(
        lambda match: "" if PurePosixPath(_path_without_query(match.group("src"))).name.casefold() in helper_names else match.group("tag"),
        source,
    )
    return STYLE_TAG_PATTERN.sub(
        lambda match: "" if PurePosixPath(_path_without_query(match.group("href"))).name.casefold() in helper_names else match.group("tag"),
        source,
    )


def _runtime_match(source: str, active_names: set[str]) -> re.Match[str] | None:
    for match in SCRIPT_TAG_PATTERN.finditer(source):
        name = PurePosixPath(_path_without_query(match.group("src"))).name.casefold()
        if name in active_names:
            return match
    return None


def inject_adapters_into_html(
    source: str,
    *,
    page_href: str,
    active_runtime_files: tuple[str, ...],
    newline: str = "\n",
) -> str:
    """Normalize only the three tool-managed tags around the active runtime."""

    cleaned = _remove_helper_tags(source)
    active_names = {PurePosixPath(value).name.casefold() for value in active_runtime_files}
    runtime = _runtime_match(cleaned, active_names)
    if runtime is None:
        raise PublishFailedError(f"Page '{page_href}' does not reference an analyzed active runtime.")
    media_url = _asset_url(page_href, APPROVED_HELPERS["media"])
    sign_url = _asset_url(page_href, APPROVED_HELPERS["sign_script"])
    style_url = _asset_url(page_href, APPROVED_HELPERS["sign_style"])
    media_tag = f'<script src="{media_url}"></script>'
    sign_tag = f'<script src="{sign_url}"></script>'
    style_tag = f'<link rel="stylesheet" href="{style_url}">'
    cleaned = cleaned[:runtime.start()] + media_tag + newline + cleaned[runtime.start():]
    runtime = _runtime_match(cleaned, active_names)
    assert runtime is not None
    cleaned = cleaned[:runtime.end()] + newline + sign_tag + cleaned[runtime.end():]
    head = re.search(r"</head\s*>", cleaned, re.IGNORECASE)
    if head:
        cleaned = cleaned[:head.start()] + style_tag + newline + cleaned[head.start():]
    else:
        first_script = SCRIPT_TAG_PATTERN.search(cleaned)
        position = first_script.start() if first_script else 0
        cleaned = cleaned[:position] + style_tag + newline + cleaned[position:]
    return cleaned


def install_accessibility_adapters(
    book: Path,
    *,
    page_hrefs: tuple[str, ...],
    active_runtime_files: tuple[str, ...],
) -> tuple[Path, ...]:
    """Install missing approved assets and targeted HTML references."""

    verify_active_runtimes(book, active_runtime_files)
    changed: list[Path] = []
    assets = book / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    for relative in APPROVED_HELPERS.values():
        destination = book / Path(*PurePosixPath(relative).parts)
        if destination.is_file():
            continue
        resource = files("adt_video_publisher").joinpath("assets", destination.name)
        destination.write_bytes(resource.read_bytes())
        changed.append(destination.relative_to(book))
    for href in page_hrefs:
        relative = Path(*PurePosixPath(href).parts)
        path = book / relative
        document = TextDocument.read(path)
        updated = inject_adapters_into_html(
            document.text,
            page_href=href,
            active_runtime_files=active_runtime_files,
            newline=document.newline,
        )
        encoded = document.encode(updated)
        if encoded != path.read_bytes():
            path.write_bytes(encoded)
            changed.append(relative)
    return tuple(dict.fromkeys(changed))


def validate_adapter_order(
    book: Path,
    *,
    page_hrefs: tuple[str, ...],
    active_runtime_files: tuple[str, ...],
) -> None:
    """Validate helper presence and media-before-runtime/sign-after-runtime order."""

    verify_active_runtimes(book, active_runtime_files)
    for relative in APPROVED_HELPERS.values():
        if not (book / Path(*PurePosixPath(relative).parts)).is_file():
            raise PublishFailedError(f"Required accessibility adapter is missing: '{relative}'.")
    active_names = {PurePosixPath(value).name.casefold() for value in active_runtime_files}
    for href in page_hrefs:
        path = book / Path(*PurePosixPath(href).parts)
        source = TextDocument.read(path).text
        scripts = [
            (match.start(), PurePosixPath(_path_without_query(match.group("src"))).name.casefold())
            for match in SCRIPT_TAG_PATTERN.finditer(source)
        ]
        runtime_positions = [position for position, name in scripts if name in active_names]
        media_positions = [position for position, name in scripts if name == "media-playback-independence.js"]
        sign_positions = [position for position, name in scripts if name == "sign-language-video.js"]
        style_count = sum(
            1 for match in STYLE_TAG_PATTERN.finditer(source)
            if PurePosixPath(_path_without_query(match.group("href"))).name.casefold() == "sign-language-video.css"
        )
        if len(runtime_positions) != 1 or len(media_positions) != 1 or len(sign_positions) != 1 or style_count != 1:
            raise PublishFailedError(f"Page '{href}' has incomplete or duplicate accessibility adapters.")
        if not media_positions[0] < runtime_positions[0] < sign_positions[0]:
            raise PublishFailedError(f"Page '{href}' loads accessibility adapters in an unsafe order.")


def write_bytes_atomic_preserving(path: Path, data: bytes) -> None:
    """Same-directory atomic byte replacement used by metadata transforms."""

    temporary = path.with_name(f".{path.name}.high2min.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
