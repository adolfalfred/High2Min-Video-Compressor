"""Transactional ADT website publishing and deterministic deployment packaging."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from .adt_adapters import (
    TextDocument,
    install_accessibility_adapters,
    validate_adapter_order,
)
from .adt_planning import APPROVED_HELPERS, analyze_adt_publish, plan_videos
from .adt_validation import (
    validate_generated_site,
    validate_staged_diff_contract,
    validate_staged_generated_site,
)
from .compression import validate_candidate
from .contracts import CONTRACT_SCHEMA_VERSION, ExitCode
from .diagnostics import DiagnosticLog
from .errors import (
    InvalidInputError,
    PublishFailedError,
    PublishingInterruptedError,
    ResourceLimitError,
    UnsafePathError,
    ValidationFailedError,
)
from .media import probe_media
from .planning import DEFAULT_MAXIMUM_BYTES
from .resources import format_megabytes

ProgressCallback = Callable[[str, str, dict[str, object]], None]
IMS_NAMESPACE: Final = "http://www.imsproject.org/xsd/imscp_rootv1p1p2"
ADLCP_NAMESPACE: Final = "http://www.adlnet.org/xsd/adlcp_rootv1p2"
XSI_NAMESPACE: Final = "http://www.w3.org/2001/XMLSchema-instance"
FIXED_ZIP_TIMESTAMP: Final = (1980, 1, 1, 0, 0, 0)
STORED_SUFFIXES: Final = {
    ".aac", ".avi", ".gif", ".gz", ".jpeg", ".jpg", ".m4a", ".m4v", ".mov",
    ".mp3", ".mp4", ".ogg", ".pdf", ".png", ".webm", ".webp", ".woff", ".woff2", ".zip",
}
RUNTIME_BUNDLE_PATTERN: Final = "base.bundle*.js"
OFFLINE_PRELOADER_RELATIVE: Final = Path("assets") / "offline-preloader.js"
ESSENTIAL_SITE_RELATIVES: Final = (
    Path("index.html"),
    Path("assets") / "config.json",
    Path("content") / "pages.json",
)
TRANSACTION_SCHEMA_VERSION: Final = 1
RUNTIME_SCRIPT_PATTERN: Final = re.compile(
    r'(?P<path>(?:\./)?assets/base\.bundle(?:\.[A-Za-z0-9_-]+)*\.js)'
    r'(?P<query>\?[^#"\'<>\s]*)?(?P<fragment>#[^"\'<>\s]*)?'
)
OFFLINE_SCRIPT_PATTERN: Final = re.compile(
    r'(?P<path>(?:\.\./|\./)*assets/offline-preloader(?:[-._][A-Za-z0-9_-]+)*\.js)'
    r'(?P<query>\?[^#"\'<>\s]*)?(?P<fragment>#[^"\'<>\s]*)?'
)


@dataclass(frozen=True, slots=True)
class PageVideo:
    source: Path
    page_index: int
    key: str
    filename: str
    size_bytes: int
    source_filename: str = ""
    page_href: str = ""


@dataclass(frozen=True, slots=True)
class PublishedVideo:
    source: Path
    output: Path
    page_index: int
    key: str
    size_bytes: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "source": str(self.source),
            "output": str(self.output),
            "status": "completed",
            "page_index": self.page_index,
            "mapping_key": self.key,
            "original_bytes": self.size_bytes,
            "output_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class PackageResult:
    path: Path
    checksum_path: Path
    size_bytes: int
    sha256: str
    entry_count: int

    def to_dict(self) -> dict[str, object]:
        document = asdict(self)
        document["path"] = str(self.path)
        document["checksum_path"] = str(self.checksum_path)
        return document


@dataclass(frozen=True, slots=True)
class PublishResult:
    job_id: str
    source_book: Path
    output_book: Path
    language: str
    bundle_version: str
    videos: tuple[PublishedVideo, ...]
    package: PackageResult | None
    diagnostic_log: Path | None = None

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.videos)

    def to_result_document(self) -> dict[str, object]:
        return {
            "schema_version": CONTRACT_SCHEMA_VERSION,
            "job_id": self.job_id,
            "ok": True,
            "exit_code": int(ExitCode.SUCCESS),
            "summary": {
                "total": len(self.videos),
                "completed": len(self.videos),
                "failed": 0,
                "video_bytes": self.total_bytes,
            },
            "book": {
                "source": str(self.source_book),
                "output": str(self.output_book),
                "language": self.language,
                "bundle_version": self.bundle_version,
            },
            "package": self.package.to_dict() if self.package else None,
            "diagnostic_log": str(self.diagnostic_log) if self.diagnostic_log else None,
            "items": [item.to_dict() for item in self.videos],
        }


class _PublishReporter:
    """Emit monotonic phase progress to the UI/CLI and a durable diagnostic log."""

    def __init__(
        self,
        job_id: str,
        callback: ProgressCallback | None,
        diagnostic_log: str | os.PathLike[str] | None,
        cancel_event: threading.Event | None,
    ) -> None:
        self.job_id = job_id
        self.callback = callback
        self.log = DiagnosticLog(diagnostic_log)
        self.cancel_event = cancel_event
        self.started = time.monotonic()
        self.percent = 0.0

    @property
    def log_path(self) -> Path | None:
        return self.log.path

    def record(self, event: str, payload: dict[str, object]) -> None:
        details = {"job_id": self.job_id, "elapsed_seconds": round(time.monotonic() - self.started, 3)}
        details.update(payload)
        self.log.write(event, details)

    def phase(
        self,
        name: str,
        percent: float,
        message: str,
        *,
        current: str | None = None,
        cancellable: bool = True,
    ) -> None:
        self.percent = max(self.percent, min(100.0, float(percent)))
        payload: dict[str, object] = {
            "kind": "publishing",
            "phase": name,
            "percent": self.percent,
            "message": message,
            "elapsed_seconds": round(time.monotonic() - self.started, 1),
            "cancellable": cancellable,
        }
        if current:
            payload["current"] = current
        self.record("phase", payload)
        if self.callback:
            self.callback(self.job_id, "item_progress", payload)

    def check_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            self.record("cancelled", {"phase_percent": self.percent})
            if self.callback:
                self.callback(
                    self.job_id,
                    "job_interrupted",
                    {"ok": False, "phase_percent": self.percent},
                )
            raise PublishingInterruptedError(
                "ADT publishing was stopped safely before repository changes began."
            )


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _resolve_directory(value: str | os.PathLike[str], label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise InvalidInputError(f"{label} directory does not exist: '{path}'.")
    return path


def _safe_archive_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise PublishFailedError(f"Manifest contains an invalid file path: {value!r}.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PublishFailedError(f"Manifest contains an unsafe file path: '{value}'.")
    return path


def _manifest_tree(path: Path) -> tuple[ET.ElementTree, ET.Element, ET.Element]:
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError) as exc:
        raise PublishFailedError(f"ADT manifest is unreadable: '{path}'.") from exc
    root = tree.getroot()
    resources = [element for element in root.iter() if element.tag == f"{{{IMS_NAMESPACE}}}resource"]
    if len(resources) != 1:
        raise PublishFailedError("ADT publishing requires exactly one SCORM resource in imsmanifest.xml.")
    if resources[0].get("href") != "index.html":
        raise PublishFailedError("The SCORM resource must launch index.html.")
    return tree, root, resources[0]


def declared_manifest_files(manifest_path: str | os.PathLike[str]) -> tuple[str, ...]:
    """Return a validated, duplicate-free manifest file list."""

    _tree, _root, resource = _manifest_tree(Path(manifest_path))
    values: list[str] = []
    seen: set[str] = set()
    for element in resource:
        if element.tag != f"{{{IMS_NAMESPACE}}}file":
            continue
        value = element.get("href")
        normalized = _safe_archive_path(value or "").as_posix()
        key = normalized.casefold()
        if key in seen:
            raise PublishFailedError(f"Manifest declares a duplicate file: '{normalized}'.")
        if normalized == "imsmanifest.xml":
            raise PublishFailedError("imsmanifest.xml must not declare itself as a resource file.")
        seen.add(key)
        values.append(normalized)
    if "index.html" not in values:
        raise PublishFailedError("The manifest does not declare index.html.")
    return tuple(values)


def _read_json(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishFailedError(f"{label} is unreadable or invalid JSON: '{path}'.") from exc


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_json_preserving_style(path: Path, value: object) -> None:
    document = TextDocument.read(path)
    multiline = "\n" in document.text or "\r" in document.text
    trailing_newline = document.text.endswith(("\n", "\r"))
    if multiline:
        match = re.search(r"\n(?P<indent>[ \t]+)[\"}]", document.text)
        indent: int | str = match.group("indent") if match else 2
        updated = json.dumps(value, ensure_ascii=False, indent=indent)
    else:
        updated = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if trailing_newline:
        updated += "\n"
    path.write_bytes(document.encode(updated))


def _page_count(book: Path) -> int:
    pages_path = book / "content" / "pages.json"
    pages = _read_json(pages_path, "content/pages.json")
    if not isinstance(pages, list) or not pages:
        raise PublishFailedError("content/pages.json must be a non-empty array.")
    for position, page in enumerate(pages, start=1):
        if not isinstance(page, dict) or not isinstance(page.get("href"), str):
            raise PublishFailedError(f"Page entry {position} has no valid href.")
        href_value = page["href"].split("?", 1)[0].split("#", 1)[0]
        href = _safe_archive_path(href_value).as_posix()
        if not (book / Path(*PurePosixPath(href).parts)).is_file():
            raise PublishFailedError(f"Page entry {position} points to a missing file: '{href}'.")
    return len(pages)


def _book_configuration(book: Path, language: str | None) -> tuple[dict[str, object], str]:
    config_path = book / "assets" / "config.json"
    config = _read_json(config_path, "assets/config.json")
    if not isinstance(config, dict):
        raise PublishFailedError("assets/config.json must contain an object.")
    languages = config.get("languages")
    if not isinstance(languages, dict):
        raise PublishFailedError("assets/config.json has no languages object.")
    selected = language or languages.get("default")
    available = languages.get("available")
    if not isinstance(selected, str) or not selected:
        raise PublishFailedError("No publication language was supplied or configured.")
    if not isinstance(available, list) or selected not in available:
        raise PublishFailedError(f"Language '{selected}' is not listed in config.json.")
    language_root = book / "content" / "i18n" / selected
    if not language_root.is_dir():
        raise PublishFailedError(f"Language content directory is missing: '{language_root}'.")
    return config, selected


def discover_page_videos(
    videos: str | os.PathLike[str],
    *,
    page_count: int,
    recursive: bool = False,
    page_hrefs: tuple[str, ...] | None = None,
    mapping_file: str | os.PathLike[str] | None = None,
) -> tuple[PageVideo, ...]:
    """Discover MP4 inputs using their single page-number group or an explicit mapping."""

    hrefs = page_hrefs or tuple(f"page-{index}" for index in range(1, page_count + 1))
    if len(hrefs) != page_count:
        raise InvalidInputError("The supplied ADT page href list does not match page_count.")
    planned = plan_videos(
        videos,
        page_hrefs=hrefs,
        recursive=recursive,
        mapping_file=mapping_file,
    )
    return tuple(
        PageVideo(
            source=item.source,
            page_index=item.page_index,
            key=item.mapping_key,
            filename=item.destination_filename,
            size_bytes=item.size_bytes,
            source_filename=item.source_filename,
            page_href=item.page_href,
        )
        for item in planned
    )


def _increment_bundle_version(value: object) -> tuple[object, str]:
    if isinstance(value, bool):
        raise PublishFailedError("bundleVersion must be numeric or dotted numeric text.")
    if isinstance(value, int) and value >= 0:
        next_value = value + 1
        return next_value, str(next_value)
    if isinstance(value, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value):
        parts = value.split(".")
        parts[-1] = str(int(parts[-1]) + 1)
        next_text = ".".join(parts)
        return next_text, next_text
    raise PublishFailedError("bundleVersion must be numeric or dotted numeric text.")


def _advance_cache_version(config: dict[str, object]) -> str:
    """Advance a legacy bundleVersion or a dedicated High2Min cache version."""

    value = config.get("bundleVersion")
    try:
        incremented, text = _increment_bundle_version(value)
    except PublishFailedError:
        cache_value = config.get("high2minCacheVersion", 0)
        if isinstance(cache_value, bool):
            cache_value = 0
        try:
            number = int(cache_value)
        except (TypeError, ValueError):
            number = 0
        number = max(0, number) + 1
        config["high2minCacheVersion"] = number
        return f"h2m-{number}"
    config["bundleVersion"] = incremented
    return text


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_and_hash_video(
    source: Path,
    destination: Path,
    *,
    reporter: _PublishReporter,
    completed_bytes: int,
    total_bytes: int,
) -> str:
    """Copy and hash once, reporting byte progress and honoring safe cancellation."""

    digest = hashlib.sha256()
    destination.parent.mkdir(parents=True, exist_ok=True)
    copied = 0
    try:
        with source.open("rb") as input_stream, destination.open("xb") as output_stream:
            while True:
                reporter.check_cancelled()
                block = input_stream.read(1024 * 1024)
                if not block:
                    break
                output_stream.write(block)
                digest.update(block)
                copied += len(block)
                overall = completed_bytes + copied
                reporter.phase(
                    "staging",
                    25 + overall / max(1, total_bytes) * 35,
                    "Copying and verifying compressed videos",
                    current=source.name,
                )
            output_stream.flush()
            # Closing the temporary staged file is sufficient here. A forced sync after
            # every video can block for minutes on Linux FUSE, NTFS, exFAT, SMB, and NFS
            # mounts. The repository is still untouched at this phase, and the durable
            # transaction journal below retains fsync before any atomic replacements.
    except Exception:
        if destination.exists():
            destination.unlink()
        raise
    return digest.hexdigest()


def _remove_generated_directory(path: Path, expected_parent: Path) -> None:
    resolved = path.resolve(strict=False)
    if resolved.parent != expected_parent.resolve() or ".adt-publish-" not in resolved.name:
        raise UnsafePathError(f"Refusing to remove an unexpected temporary directory: '{resolved}'.")
    if resolved.exists():
        shutil.rmtree(resolved)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _write_json_atomic(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_write_probe(directory: Path) -> None:
    """Verify create, flush, rename, and delete permissions before expensive work."""

    first = directory / f".high2min-write-test-{uuid.uuid4().hex}.tmp"
    second = directory / f".high2min-write-test-{uuid.uuid4().hex}.tmp"
    try:
        with first.open("xb") as stream:
            stream.write(b"high2min-write-test")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(first, second)
        second.unlink()
    except OSError as exc:
        raise PublishFailedError(
            f"High2Min cannot create, rename, and delete files in '{directory}': {exc}. "
            "Move the ADT repository to a writable local folder or correct its permissions."
        ) from exc
    finally:
        for path in (first, second):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass


def _preflight_in_place_filesystem(book: Path, language: str) -> None:
    targets = (
        book.parent,
        book,
        book / "assets",
        book / "content" / "i18n" / language,
    )
    for directory in targets:
        if not directory.is_dir():
            raise PublishFailedError(f"Required writable ADT directory is missing: '{directory}'.")
        _atomic_write_probe(directory)


def _copy_file(source: Path, destination: Path) -> None:
    """Copy file contents without propagating platform-specific xattrs or ACL metadata."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _copy_in_place_stage_sources(
    source: Path,
    stage: Path,
    declared: tuple[str, ...],
    page_hrefs: tuple[str, ...],
    offline_resource_files: tuple[str, ...],
    active_offline_preloaders: tuple[str, ...],
    language: str,
    active_runtime_files: tuple[str, ...],
    reporter: _PublishReporter,
) -> None:
    """Stage only files that publishing can modify, never the complete ADT website."""

    required = {
        Path("assets") / "config.json",
        Path("imsmanifest.xml"),
        Path("content") / "i18n" / language / "videos.json",
    }
    for relative_text in active_offline_preloaders:
        required.add(Path(*PurePosixPath(relative_text).parts))
    for relative_text in declared:
        relative = Path(*PurePosixPath(relative_text).parts)
        if relative.suffix.lower() == ".html":
            required.add(relative)
    for href in page_hrefs:
        required.add(Path(*PurePosixPath(href).parts))
    for relative_text in offline_resource_files:
        required.add(Path(*PurePosixPath(relative_text).parts))
    for relative_text in active_runtime_files:
        required.add(Path(*PurePosixPath(relative_text).parts))
    for relative_text in APPROVED_HELPERS.values():
        relative = Path(*PurePosixPath(relative_text).parts)
        if (source / relative).is_file():
            required.add(relative)
    ordered = sorted(required, key=lambda value: value.as_posix().casefold())
    total = max(1, len(ordered))
    for position, relative in enumerate(ordered, start=1):
        reporter.check_cancelled()
        source_file = source / relative
        if source_file.is_file():
            reporter.phase(
                "staging",
                22 + position / total * 3,
                f"Preparing generated website files ({position}/{len(ordered)})",
                current=relative.as_posix(),
            )
            _copy_file(source_file, stage / relative)


def _write_manifest_file_list(source_manifest: Path, destination: Path, files: set[str]) -> tuple[str, ...]:
    """Patch resource file declarations while preserving XML comments and unrelated formatting."""

    _manifest_tree(source_manifest)
    normalized_files: dict[str, str] = {}
    for relative in files:
        normalized = _safe_archive_path(relative).as_posix()
        key = normalized.casefold()
        if key in normalized_files:
            raise PublishFailedError(f"Publication would create a duplicate manifest path: '{normalized}'.")
        normalized_files[key] = normalized
    document = TextDocument.read(source_manifest)
    resource_pattern = re.compile(
        r"<(?P<prefix>(?:[A-Za-z_][\w.-]*:)?)resource\b(?P<attrs>[^>]*)\bhref\s*=\s*"
        r"(?P<quote>['\"])index\.html(?P=quote)[^>]*>(?P<body>.*?)"
        r"</(?P=prefix)resource\s*>",
        re.IGNORECASE | re.DOTALL,
    )
    resources = list(resource_pattern.finditer(document.text))
    if len(resources) != 1:
        raise PublishFailedError("ADT publishing requires one textual SCORM resource for index.html.")
    resource = resources[0]
    body = resource.group("body")
    file_pattern = re.compile(
        r"(?P<leading>[ \t]*)<(?P<prefix>(?:[A-Za-z_][\w.-]*:)?)file\b(?P<attrs>[^>]*)"
        r"\bhref\s*=\s*(?P<quote>['\"])(?P<href>[^'\"]+)(?P=quote)[^>]*/\s*>[ \t]*(?P<newline>\r?\n)?",
        re.IGNORECASE,
    )
    kept_keys: set[str] = set()

    def keep_or_remove(match: re.Match[str]) -> str:
        relative = _safe_archive_path(match.group("href")).as_posix()
        key = relative.casefold()
        if key not in normalized_files:
            return ""
        if key in kept_keys:
            raise PublishFailedError(f"Manifest declares a duplicate file: '{relative}'.")
        kept_keys.add(key)
        return match.group(0)

    patched_body = file_pattern.sub(keep_or_remove, body)
    missing = [
        value for key, value in normalized_files.items()
        if key not in kept_keys
    ]
    if missing:
        matches = list(file_pattern.finditer(body))
        indent = matches[-1].group("leading") if matches else "    "
        prefix = (matches[-1].group("prefix") or "") if matches else (resource.group("prefix") or "")
        newline = document.newline
        insertion = "".join(
            f'{indent}<{prefix}file href="{relative}" />{newline}'
            for relative in sorted(missing, key=lambda value: (value != "index.html", value.casefold()))
        )
        if patched_body and not patched_body.endswith(("\n", "\r")):
            patched_body += newline
        patched_body += insertion
    updated = document.text[:resource.start("body")] + patched_body + document.text[resource.end("body"):]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(document.encode(updated))
    declared = declared_manifest_files(destination)
    if {value.casefold() for value in declared} != set(normalized_files):
        raise PublishFailedError("Targeted manifest update did not produce the requested file set.")
    return declared


def _update_in_place_manifest(
    source: Path,
    stage: Path,
    *,
    language: str,
    declared: tuple[str, ...],
    page_hrefs: tuple[str, ...],
    offline_resource_files: tuple[str, ...],
    active_offline_preloaders: tuple[str, ...],
    videos: tuple[PageVideo, ...],
    mode: str,
) -> tuple[str, ...]:
    prefix = f"content/i18n/{language}/video/".casefold()
    files = {
        relative
        for relative in declared
        if _overlay_file(source, stage, relative).is_file()
    }
    files.update(page_hrefs)
    files.update(offline_resource_files)
    files.update(active_offline_preloaders)
    if mode == "replace":
        files = {relative for relative in files if not relative.casefold().startswith(prefix)}
    files.update(f"content/i18n/{language}/video/{item.filename}" for item in videos)
    files.add(f"content/i18n/{language}/videos.json")
    for relative in ESSENTIAL_SITE_RELATIVES:
        if (source / relative).is_file() or (stage / relative).is_file():
            files.add(relative.as_posix())
    for runtime in (stage / "assets").glob(RUNTIME_BUNDLE_PATTERN):
        if runtime.is_file():
            files.add(runtime.relative_to(stage).as_posix())
    for relative_text in APPROVED_HELPERS.values():
        relative = Path(*PurePosixPath(relative_text).parts)
        if (stage / relative).is_file() or (source / relative).is_file():
            files.add(relative.as_posix())
    return _write_manifest_file_list(
        source / "imsmanifest.xml",
        stage / "imsmanifest.xml",
        files,
    )


def _overlay_file(source: Path, stage: Path, relative: str) -> Path:
    path = Path(*PurePosixPath(relative).parts)
    staged = stage / path
    return staged if staged.is_file() else source / path


def _validate_staged_in_place(
    source: Path,
    stage: Path,
    *,
    language: str,
    mode: str = "replace",
) -> dict[str, object]:
    """Validate a minimal staged overlay without materializing another website copy."""

    page_count = _page_count(source)
    config = _read_json(stage / "assets" / "config.json", "staged assets/config.json")
    if not isinstance(config, dict):
        raise PublishFailedError("Staged assets/config.json must contain an object.")
    features = config.get("features")
    if (
        not isinstance(features, dict)
        or features.get("signLanguage") is not True
        or features.get("readAloud") is not True
    ):
        raise PublishFailedError("The staged config does not enable sign language and voice-over.")
    videos_path = stage / "content" / "i18n" / language / "videos.json"
    mappings = _read_json(videos_path, f"staged {language}/videos.json")
    if not isinstance(mappings, dict):
        raise PublishFailedError("Staged videos.json must contain an object.")
    video_root = videos_path.parent / "video"
    expected_files: set[str] = set()
    for key, filename in mappings.items():
        match = re.fullmatch(r"video-([1-9][0-9]*)", str(key))
        if not match or int(match.group(1)) > page_count:
            raise PublishFailedError(f"Staged videos.json contains an invalid key: '{key}'.")
        expected = f"page_{int(match.group(1))}.mp4"
        staged_video = video_root / expected
        source_video = source / "content" / "i18n" / language / "video" / expected
        exists = staged_video.is_file() or (mode == "merge" and source_video.is_file())
        if filename != expected or not exists:
            raise PublishFailedError(f"Staged mapping '{key}' must point to an existing '{expected}'.")
        expected_files.add(expected.casefold())
    actual_files = {path.name.casefold() for path in video_root.glob("*.mp4") if path.is_file()}
    if mode == "merge":
        source_video_root = source / "content" / "i18n" / language / "video"
        actual_files.update(
            path.name.casefold() for path in source_video_root.glob("*.mp4") if path.is_file()
        )
    if actual_files != expected_files:
        raise PublishFailedError("The staged video directory and videos.json do not match exactly.")
    declared = declared_manifest_files(stage / "imsmanifest.xml")
    missing = [relative for relative in declared if not _overlay_file(source, stage, relative).is_file()]
    if missing:
        raise PublishFailedError(
            f"Staged manifest references {len(missing)} missing file(s); first: '{missing[0]}'."
        )
    return {
        "page_count": page_count,
        "video_count": len(mappings),
        "language": language,
        "manifest_file_count": len(declared),
        "bundle_version": str(config.get("bundleVersion")),
    }


def _validate_media(item: PageVideo, maximum_bytes: int, probe_path: str | None) -> None:
    media = probe_media(item.source, probe_path=probe_path)
    report = validate_candidate(
        media=media,
        source_duration_seconds=media.duration_seconds,
        maximum_bytes=maximum_bytes,
        strict_size=True,
    )
    if not report.valid:
        raise ValidationFailedError(f"'{item.source.name}' is not publishable: {' '.join(report.errors)}")


def _copy_manifest_site(
    source: Path,
    stage: Path,
    *,
    skip_prefix: str | None,
    page_hrefs: tuple[str, ...],
    offline_resource_files: tuple[str, ...],
    active_offline_preloaders: tuple[str, ...],
    active_runtime_files: tuple[str, ...],
) -> None:
    declared = declared_manifest_files(source / "imsmanifest.xml")
    required = list(declared)
    seen = {relative.casefold() for relative in required}
    for relative in (*page_hrefs, *offline_resource_files, *active_offline_preloaders):
        if relative.casefold() not in seen:
            required.append(relative)
            seen.add(relative.casefold())
    prefix = skip_prefix.casefold().rstrip("/") + "/" if skip_prefix else None
    for relative in required:
        if prefix and relative.casefold().startswith(prefix):
            continue
        source_file = source / Path(*PurePosixPath(relative).parts)
        if not source_file.is_file():
            continue
        destination = stage / Path(*PurePosixPath(relative).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, destination)
    for relative in ESSENTIAL_SITE_RELATIVES:
        source_file = source / relative
        if not source_file.is_file():
            continue
        destination = stage / relative
        if destination.is_file():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, destination)
    for relative_text in active_runtime_files:
        source_file = source / Path(*PurePosixPath(relative_text).parts)
        if not source_file.is_file():
            continue
        destination = stage / Path(*PurePosixPath(relative_text).parts)
        if destination.is_file():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, destination)
    shutil.copy2(source / "imsmanifest.xml", stage / "imsmanifest.xml")


def _update_manifest(book: Path) -> tuple[str, ...]:
    manifest_path = book / "imsmanifest.xml"
    tree, _root, resource = _manifest_tree(manifest_path)
    files = sorted(
        (
            path.relative_to(book).as_posix()
            for path in book.rglob("*")
            if path.is_file() and path != manifest_path
        ),
        key=lambda value: (value != "index.html", value.casefold()),
    )
    for child in list(resource):
        if child.tag == f"{{{IMS_NAMESPACE}}}file":
            resource.remove(child)
    for relative in files:
        ET.SubElement(resource, f"{{{IMS_NAMESPACE}}}file", {"href": relative})
    ET.register_namespace("", IMS_NAMESPACE)
    ET.register_namespace("adlcp", ADLCP_NAMESPACE)
    ET.register_namespace("xsi", XSI_NAMESPACE)
    ET.indent(tree, space="  ")
    tree.write(manifest_path, encoding="utf-8", xml_declaration=True)
    return tuple(files)


def _inline_json_span(source: str) -> tuple[int, int]:
    assignment = re.search(r"\b(?:var|let|const)\s+INLINE\s*=\s*", source)
    if assignment is None:
        raise PublishFailedError("Offline preloader has no INLINE resource map.")
    start = assignment.end()
    while start < len(source) and source[start].isspace():
        start += 1
    if start >= len(source) or source[start] != "{":
        raise PublishFailedError("Offline preloader INLINE resource map must start with an object.")
    depth = 0
    in_string = False
    escaped = False
    for position in range(start, len(source)):
        character = source[position]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return start, position + 1
            if depth < 0:
                break
    raise PublishFailedError("Offline preloader INLINE resource map is incomplete.")


def _versioned_reference(match: re.Match[str], version: str) -> str:
    query = (match.groupdict().get("query") or "").lstrip("?")
    parts = query.split("&") if query else []
    replaced = False
    for index, part in enumerate(parts):
        if part.split("=", 1)[0] == "v":
            parts[index] = f"v={version}"
            replaced = True
    if not replaced:
        parts.append(f"v={version}")
    return f"{match.group('path')}?{'&'.join(parts)}{match.groupdict().get('fragment') or ''}"


def _synchronize_offline_preloader(
    book: Path,
    *,
    language: str,
    bundle_version: str,
    page_hrefs: tuple[str, ...],
    active_offline_preloaders: tuple[str, ...] | None = None,
    offline_preloader_recoveries: dict[str, str] | None = None,
) -> tuple[Path, ...]:
    """Refresh every active offline preloader without changing its authored wrapper."""

    changed: list[Path] = []
    active_pages = sorted(
        {Path(*PurePosixPath(href).parts) for href in page_hrefs},
        key=lambda path: path.as_posix().casefold(),
    )
    for relative in active_pages:
        html_path = book / relative
        document = TextDocument.read(html_path)
        source = document.text
        updated = OFFLINE_SCRIPT_PATTERN.sub(
            lambda match: _versioned_reference(match, bundle_version),
            source,
        )
        updated = RUNTIME_SCRIPT_PATTERN.sub(
            lambda match: _versioned_reference(match, bundle_version),
            updated,
        )
        if updated != source:
            html_path.write_bytes(document.encode(updated))
            changed.append(relative)

    selected = active_offline_preloaders
    if selected is None:
        selected = (
            (OFFLINE_PRELOADER_RELATIVE.as_posix(),)
            if (book / OFFLINE_PRELOADER_RELATIVE).is_file()
            else ()
        )
    recoveries = offline_preloader_recoveries or {}
    documents: dict[str, tuple[TextDocument, str, int, int]] = {}
    valid_payloads: dict[str, dict[str, object]] = {}
    for relative_text in selected:
        relative = _safe_archive_path(relative_text)
        preloader = book / Path(*relative.parts)
        if not preloader.is_file():
            raise PublishFailedError(f"Active offline preloader is missing: '{relative_text}'.")
        document = TextDocument.read(preloader)
        source = document.text
        payload_start, payload_end = _inline_json_span(source)
        documents[relative_text] = (document, source, payload_start, payload_end)
        try:
            payload = json.loads(source[payload_start:payload_end])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            valid_payloads[relative_text] = payload

    for relative_text in selected:
        document, source, payload_start, payload_end = documents[relative_text]
        inline = valid_payloads.get(relative_text)
        if inline is None:
            recovery_source = recoveries.get(relative_text)
            recovery = valid_payloads.get(recovery_source or "")
            if recovery is None:
                raise PublishFailedError(
                    f"Active offline preloader '{relative_text}' has invalid JSON and no valid recovery source."
                )
            inline = copy.deepcopy(recovery)
        else:
            inline = copy.deepcopy(inline)

        inline["./assets/config.json"] = _read_json(
            book / "assets" / "config.json", "assets/config.json"
        )
        inline[f"./content/i18n/{language}/videos.json"] = _read_json(
            book / "content" / "i18n" / language / "videos.json",
            f"{language}/videos.json",
        )
        for key in tuple(inline):
            if not isinstance(key, str):
                continue
            relative_value = key.removeprefix("./")
            try:
                resource = _safe_archive_path(relative_value)
            except PublishFailedError:
                continue
            source_path = book / Path(*resource.parts)
            if not source_path.is_file():
                continue
            if source_path.suffix.lower() == ".html":
                # TextDocument preserves BOM and newline semantics in embedded pages.
                inline[key] = TextDocument.read(source_path).text
            elif source_path.suffix.lower() == ".json":
                inline[key] = _read_json(source_path, resource.as_posix())

        serialized = json.dumps(inline, ensure_ascii=True, separators=(",", ":"))
        updated = source[:payload_start] + serialized + source[payload_end:]
        if updated != source:
            preloader = book / Path(*_safe_archive_path(relative_text).parts)
            preloader.write_bytes(document.encode(updated))
            changed.append(Path(*_safe_archive_path(relative_text).parts))
    return tuple(dict.fromkeys(changed))


def validate_adt_website(
    book: str | os.PathLike[str],
    *,
    language: str | None = None,
    allow_unmanifested: bool = False,
) -> dict[str, object]:
    """Validate manifest completeness, page files, config, and video mappings."""

    root = _resolve_directory(book, "ADT website")
    required = (root / "index.html", root / "imsmanifest.xml", root / "assets" / "config.json")
    for path in required:
        if not path.is_file():
            raise PublishFailedError(f"Required ADT file is missing: '{path}'.")
    page_count = _page_count(root)
    config, selected_language = _book_configuration(root, language)
    features = config.get("features")
    if (
        not isinstance(features, dict)
        or features.get("signLanguage") is not True
        or features.get("readAloud") is not True
    ):
        raise PublishFailedError("The published config does not enable sign language and voice-over.")
    videos_path = root / "content" / "i18n" / selected_language / "videos.json"
    mappings = _read_json(videos_path, f"{selected_language}/videos.json")
    if not isinstance(mappings, dict):
        raise PublishFailedError("videos.json must contain an object.")
    video_root = videos_path.parent / "video"
    mapped_files: set[str] = set()
    for key, filename in mappings.items():
        match = re.fullmatch(r"video-([1-9][0-9]*)", str(key))
        if not match or int(match.group(1)) > page_count:
            raise PublishFailedError(f"videos.json contains an invalid key: '{key}'.")
        if not isinstance(filename, str) or PurePosixPath(filename).name != filename:
            raise PublishFailedError(f"videos.json contains an unsafe filename for '{key}'.")
        expected = f"page_{int(match.group(1))}.mp4"
        if filename != expected:
            raise PublishFailedError(f"Mapping '{key}' must point to '{expected}'.")
        if filename.casefold() in mapped_files:
            raise PublishFailedError(f"videos.json maps a file more than once: '{filename}'.")
        mapped_files.add(filename.casefold())
        if not (video_root / filename).is_file():
            raise PublishFailedError(f"Mapped video is missing: '{filename}'.")
    actual_videos = {
        path.name.casefold() for path in video_root.glob("*.mp4") if path.is_file()
    } if video_root.is_dir() else set()
    if actual_videos != mapped_files:
        raise PublishFailedError("The published video directory and videos.json do not match exactly.")
    declared = declared_manifest_files(root / "imsmanifest.xml")
    missing = sorted(
        relative
        for relative in declared
        if not (root / Path(*PurePosixPath(relative).parts)).is_file()
    )
    extra: list[str] = []
    if not allow_unmanifested:
        actual = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file() and path != root / "imsmanifest.xml"
        }
        extra = sorted(actual - set(declared))
    if missing or (extra and not allow_unmanifested):
        raise PublishFailedError(
            f"Manifest/site mismatch: {len(missing)} missing and {len(extra)} undeclared file(s)."
        )
    return {
        "page_count": page_count,
        "video_count": len(mappings),
        "language": selected_language,
        "manifest_file_count": len(declared),
        "bundle_version": str(config.get("bundleVersion")),
    }


def _zip_info(name: str, compression: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, FIXED_ZIP_TIMESTAMP)
    info.compress_type = compression
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.flag_bits |= 0x800
    return info


def build_deployment_package(book: Path, package_path: Path) -> tuple[int, str]:
    """Create a deterministic ZIP containing exactly the manifest declaration."""

    declared = declared_manifest_files(book / "imsmanifest.xml")
    entries = ("imsmanifest.xml", *sorted(declared, key=str.casefold))
    package_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(package_path, "w", allowZip64=True, compresslevel=9) as archive:
        for relative in entries:
            source = book / Path(*PurePosixPath(relative).parts)
            if not source.is_file():
                raise PublishFailedError(f"Cannot package missing declared file: '{relative}'.")
            compression = zipfile.ZIP_STORED if source.suffix.lower() in STORED_SUFFIXES else zipfile.ZIP_DEFLATED
            info = _zip_info(relative, compression)
            with source.open("rb") as input_file, archive.open(info, "w", force_zip64=True) as output_file:
                shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
    validate_deployment_package(package_path)
    return len(entries), _hash_file(package_path)


def validate_deployment_package(package: str | os.PathLike[str]) -> dict[str, object]:
    path = Path(package).expanduser().resolve()
    if not path.is_file():
        raise PublishFailedError(f"Deployment package does not exist: '{path}'.")
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise PublishFailedError("Deployment ZIP contains duplicate entries.")
            for name in names:
                _safe_archive_path(name)
            if names.count("imsmanifest.xml") != 1:
                raise PublishFailedError("Deployment ZIP must contain one root imsmanifest.xml.")
            damaged = archive.testzip()
            if damaged is not None:
                raise PublishFailedError(f"Deployment ZIP has a damaged entry: '{damaged}'.")
            try:
                manifest_root = ET.fromstring(archive.read("imsmanifest.xml"))
            except ET.ParseError as exc:
                raise PublishFailedError("Packaged imsmanifest.xml is invalid.") from exc
            resources = [
                element for element in manifest_root.iter()
                if element.tag == f"{{{IMS_NAMESPACE}}}resource"
            ]
            if len(resources) != 1:
                raise PublishFailedError("Packaged manifest must contain exactly one resource.")
            declared = []
            for element in resources[0]:
                if element.tag == f"{{{IMS_NAMESPACE}}}file":
                    declared.append(_safe_archive_path(element.get("href") or "").as_posix())
            expected = {"imsmanifest.xml", *declared}
            if set(names) != expected:
                raise PublishFailedError("Deployment ZIP entries do not exactly match its manifest.")
    except zipfile.BadZipFile as exc:
        raise PublishFailedError(f"Deployment package is not a valid ZIP: '{path}'.") from exc
    return {"path": str(path), "entry_count": len(names), "size_bytes": path.stat().st_size}


def _commit_file_no_clobber(temporary: Path, final: Path) -> None:
    """Atomically expose a generated file without replacing an existing path."""

    try:
        os.link(temporary, final)
    except FileExistsError as exc:
        raise UnsafePathError(f"Refusing to replace an existing output: '{final}'.") from exc
    except OSError as exc:
        raise PublishFailedError(f"Could not commit generated output '{final}': {exc}") from exc
    temporary.unlink()


def _target_fingerprint(path: Path) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    if not path.is_file() or path.is_symlink():
        raise PublishFailedError(f"Publish target is not a regular file: '{path}'.")
    return _hash_file(path)


def _capture_baselines(book: Path, relatives: tuple[Path, ...]) -> dict[str, str | None]:
    return {
        relative.as_posix(): _target_fingerprint(book / relative)
        for relative in dict.fromkeys(relatives)
    }


def _verify_baselines(book: Path, baselines: dict[str, str | None]) -> None:
    for relative_text, expected in baselines.items():
        relative = Path(*PurePosixPath(relative_text).parts)
        actual = _target_fingerprint(book / relative)
        if actual != expected:
            raise PublishFailedError(
                f"Concurrent edit detected for '{relative_text}'; publishing was aborted before commit."
            )


def _verify_zip_sentinels(book: Path, sentinels: dict[str, str]) -> None:
    current = {
        path.relative_to(book).as_posix(): _hash_file(path)
        for path in book.rglob("*.zip")
        if path.is_file()
    }
    if current != sentinels:
        raise PublishFailedError("A ZIP file changed during ADT publishing; the transaction was stopped.")


def _commit_in_place(
    stage: Path,
    book: Path,
    language: str,
    change_relatives: tuple[Path, ...],
    removal_relatives: tuple[Path, ...],
    baselines: dict[str, str | None],
    zip_sentinels: dict[str, str],
    page_hrefs: tuple[str, ...],
    active_runtime_files: tuple[str, ...],
    active_offline_preloaders: tuple[str, ...],
    reporter: _PublishReporter,
) -> None:
    """Commit only allowlisted files and restore them from a durable journal on failure."""

    transaction_id = uuid.uuid4().hex
    backup = book.parent / f".{book.name}.adt-publish-{transaction_id}.backup"
    journal = backup / "transaction.json"
    changes = tuple(dict.fromkeys(change_relatives))
    removals = tuple(relative for relative in dict.fromkeys(removal_relatives) if relative not in changes)
    relatives = (*changes, *removals)
    if not relatives:
        raise PublishFailedError("The ADT transaction contains no approved file changes.")
    allowed = set(baselines)
    unexpected = [relative.as_posix() for relative in relatives if relative.as_posix() not in allowed]
    if unexpected:
        raise PublishFailedError(f"Transaction target is outside the publish allowlist: '{unexpected[0]}'.")
    targets = [
        {
            "relative": relative.as_posix(),
            "had_original": (book / relative).exists(),
            "action": "replace" if relative in changes else "delete",
        }
        for relative in relatives
    ]
    document: dict[str, object] = {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "status": "committing",
        "book": str(book),
        "stage": str(stage),
        "language": language,
        "targets": targets,
    }
    try:
        backup.mkdir()
        _write_json_atomic(journal, document)
        _verify_zip_sentinels(book, zip_sentinels)
        _verify_baselines(book, {relative.as_posix(): baselines[relative.as_posix()] for relative in relatives})
        total = max(1, len(relatives))
        for position, relative in enumerate(relatives, start=1):
            staged = stage / relative
            action = "replace" if relative in changes else "delete"
            if action == "replace" and not staged.is_file():
                raise PublishFailedError(f"Staged publication target is missing: '{relative.as_posix()}'.")
            target = book / relative
            saved = backup / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                saved.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, saved)
            if action == "replace":
                os.replace(staged, target)
            reporter.phase(
                "commit",
                85 + position / total * 10,
                f"Committing repository changes ({position}/{total})",
                current=relative.as_posix(),
                cancellable=False,
            )
        reporter.phase(
            "final_validation",
            97,
            "Validating the updated ADT repository",
            cancellable=False,
        )
        validate_adt_website(book, language=language, allow_unmanifested=True)
        validate_generated_site(
            book,
            language=language,
            page_hrefs=page_hrefs,
            active_runtime_files=active_runtime_files,
            active_offline_preloaders=active_offline_preloaders,
        )
        _verify_zip_sentinels(book, zip_sentinels)
        document["status"] = "committed"
        _write_json_atomic(journal, document)
    except Exception as original_error:
        reporter.record("rollback_started", {"error": str(original_error), "backup": str(backup)})
        try:
            _rollback_transaction(book, stage, backup, targets)
        except Exception as recovery_error:
            reporter.record("rollback_failed", {"error": str(recovery_error), "backup": str(backup)})
            raise PublishFailedError(
                "Publishing failed and automatic recovery was incomplete. "
                f"Original files remain in '{backup}'."
            ) from recovery_error
        reporter.record("rollback_completed", {"backup": str(backup)})
        raise

    reporter.phase("cleanup", 99, "Removing transaction files", cancellable=False)
    cleanup_errors: list[str] = []
    for generated in (stage, backup):
        if not generated.exists():
            continue
        try:
            _remove_generated_directory(generated, book.parent)
        except OSError as exc:
            cleanup_errors.append(f"{generated}: {exc}")
    if cleanup_errors:
        reporter.record("cleanup_deferred", {"errors": cleanup_errors})


def _transaction_targets(document: object) -> list[dict[str, object]]:
    if not isinstance(document, dict) or document.get("schema_version") != TRANSACTION_SCHEMA_VERSION:
        raise PublishFailedError("ADT recovery journal has an unsupported format.")
    values = document.get("targets")
    if not isinstance(values, list) or not values:
        raise PublishFailedError("ADT recovery journal contains no transaction targets.")
    targets: list[dict[str, object]] = []
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get("relative"), str):
            raise PublishFailedError("ADT recovery journal contains an invalid target.")
        relative = _safe_archive_path(str(value["relative"])).as_posix()
        action = value.get("action", "replace")
        if action not in {"replace", "delete"}:
            raise PublishFailedError("ADT recovery journal contains an invalid action.")
        targets.append({
            "relative": relative,
            "had_original": value.get("had_original") is True,
            "action": action,
        })
    return targets


def _rollback_transaction(
    book: Path,
    stage: Path,
    backup: Path,
    targets: list[dict[str, object]],
) -> None:
    for value in reversed(targets):
        relative = Path(*PurePosixPath(str(value["relative"])).parts)
        target = book / relative
        saved = backup / relative
        if saved.exists() or saved.is_symlink():
            _remove_path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(saved, target)
        elif value.get("had_original") is not True:
            _remove_path(target)
    if stage.exists():
        _remove_generated_directory(stage, book.parent)
    if backup.exists():
        _remove_generated_directory(backup, book.parent)


def _recover_pending_transactions(book: Path, reporter: _PublishReporter) -> None:
    pattern = f".{book.name}.adt-publish-*.backup"
    for backup in sorted(book.parent.glob(pattern), key=lambda path: path.name):
        journal = backup / "transaction.json"
        if not journal.is_file():
            reporter.record("legacy_backup_preserved", {"backup": str(backup)})
            continue
        document = _read_json(journal, "ADT recovery journal")
        if not isinstance(document, dict) or Path(str(document.get("book", ""))).resolve() != book:
            raise PublishFailedError(f"ADT recovery journal does not belong to '{book}': '{journal}'.")
        targets = _transaction_targets(document)
        stage = Path(str(document.get("stage", ""))).resolve(strict=False)
        if stage.parent != book.parent or ".adt-publish-" not in stage.name:
            raise UnsafePathError(f"ADT recovery journal contains an unsafe stage path: '{stage}'.")
        status = document.get("status")
        reporter.record("recovery_started", {"backup": str(backup)})
        if status == "committed":
            for generated in (stage, backup):
                if generated.exists():
                    _remove_generated_directory(generated, book.parent)
        elif status == "committing":
            _rollback_transaction(book, stage, backup, targets)
        else:
            raise PublishFailedError(f"ADT recovery journal has an invalid status: {status!r}.")
        reporter.record("recovery_completed", {"backup": str(backup), "status": status})


def publish_adt(
    videos: str | os.PathLike[str],
    *,
    book: str | os.PathLike[str],
    output: str | os.PathLike[str] | None = None,
    package: str | os.PathLike[str] | None = None,
    in_place: bool = False,
    language: str | None = None,
    recursive: bool = False,
    mapping_file: str | os.PathLike[str] | None = None,
    mode: str = "merge",
    confirm_removals: bool = False,
    maximum_bytes: int = DEFAULT_MAXIMUM_BYTES,
    probe_path: str | None = None,
    validate_media: bool = True,
    progress_callback: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
    diagnostic_log: str | os.PathLike[str] | None = None,
) -> PublishResult:
    """Publish videos into a new copy, or transactionally update an ADT website in place."""

    job_id = uuid.uuid4().hex
    reporter = _PublishReporter(
        job_id,
        progress_callback,
        diagnostic_log,
        cancel_event,
    )
    reporter.record(
        "job_created",
        {
            "book": str(book),
            "videos": str(videos),
            "in_place": in_place,
            "os_name": os.name,
        },
    )
    source_book = _resolve_directory(book, "Source ADT website")
    video_root = _resolve_directory(videos, "Compressed video")
    if in_place:
        if output is not None:
            raise InvalidInputError("In-place publishing does not accept a separate output directory.")
        if package is not None:
            raise InvalidInputError("In-place publishing does not create or modify deployment ZIP packages.")
        output_book = source_book
    else:
        if output is None:
            raise InvalidInputError("A published website output is required unless in-place mode is enabled.")
        output_book = Path(output).expanduser().resolve(strict=False)
    package_path = Path(package).expanduser().resolve(strict=False) if package else None
    checksum_path = Path(str(package_path) + ".sha256") if package_path else None
    if not in_place:
        if output_book.exists():
            raise UnsafePathError(f"Published website output already exists: '{output_book}'.")
        if _same_path(output_book, source_book) or _is_within(output_book, source_book) or _is_within(source_book, output_book):
            raise UnsafePathError("Published website output must be separate from the source ADT website.")
        if _same_path(output_book, video_root) or _is_within(output_book, video_root) or _is_within(video_root, output_book):
            raise UnsafePathError("Published website output must be separate from the compressed videos.")
    if package_path:
        if package_path.suffix.lower() != ".zip":
            raise InvalidInputError("Deployment package must use the .zip extension.")
        if package_path.exists() or (checksum_path and checksum_path.exists()):
            raise UnsafePathError("Deployment package or checksum output already exists.")
        if _is_within(package_path, source_book) or _is_within(package_path, video_root) or _is_within(package_path, output_book):
            raise UnsafePathError("Deployment package must be outside source, video, and published website folders.")
    if not in_place:
        output_book.parent.mkdir(parents=True, exist_ok=True)
    if package_path:
        package_path.parent.mkdir(parents=True, exist_ok=True)

    _recover_pending_transactions(source_book, reporter)
    publication_plan = analyze_adt_publish(
        video_root,
        book=source_book,
        language=language,
        recursive=recursive,
        mapping_file=mapping_file,
        mode=mode,
    )
    if publication_plan.blockers:
        raise PublishFailedError(" ".join(publication_plan.blockers))
    if publication_plan.removals and not confirm_removals:
        raise InvalidInputError(
            f"Replace mode would remove {len(publication_plan.removals)} existing video file(s). "
            "Review publish-plan, then pass confirm_removals=True or --confirm-removals."
        )
    page_count = len(publication_plan.page_hrefs)
    _config, selected_language = _book_configuration(source_book, publication_plan.language)
    if in_place:
        _preflight_in_place_filesystem(source_book, selected_language)
        target_video_root = source_book / "content" / "i18n" / selected_language / "video"
        if (
            _same_path(video_root, target_video_root)
            or _is_within(video_root, target_video_root)
            or _is_within(target_video_root, video_root)
        ):
            raise UnsafePathError(
                "The compressed-video input must be separate from the ADT video directory being replaced."
            )
    items = discover_page_videos(
        video_root,
        page_count=page_count,
        recursive=recursive,
        page_hrefs=publication_plan.page_hrefs,
        mapping_file=mapping_file,
    )
    planned_relatives = tuple(
        Path(*PurePosixPath(relative).parts)
        for relative in (*publication_plan.mutations, *publication_plan.removals)
    )
    baselines = _capture_baselines(source_book, planned_relatives)
    total_video_bytes = sum(item.size_bytes for item in items)
    source_declared = declared_manifest_files(source_book / "imsmanifest.xml")
    if in_place:
        stage_source_paths: set[Path] = {
            source_book / "assets" / "config.json",
            source_book / "imsmanifest.xml",
        }
        stage_source_paths.update(
            source_book / Path(*PurePosixPath(relative).parts)
            for relative in publication_plan.active_offline_preloaders
        )
        stage_source_paths.update(
            source_book / Path(*PurePosixPath(relative).parts)
            for relative in source_declared
            if PurePosixPath(relative).suffix.lower() == ".html"
        )
        stage_source_paths.update(
            source_book / Path(*PurePosixPath(relative).parts)
            for relative in publication_plan.page_hrefs
        )
        stage_source_paths.update(
            source_book / Path(*PurePosixPath(relative).parts)
            for relative in publication_plan.offline_resource_files
        )
        stage_source_paths.update(
            source_book / Path(*PurePosixPath(relative).parts)
            for relative in publication_plan.active_runtime_files
        )
        staged_source_bytes = sum(path.stat().st_size for path in stage_source_paths if path.is_file())
        estimated_stage_bytes = total_video_bytes + staged_source_bytes
        required_bytes = estimated_stage_bytes + max(64 * 1024 * 1024, estimated_stage_bytes // 10)
    else:
        source_relatives = set(source_declared)
        source_relatives.update(publication_plan.manifest_recoveries)
        source_bytes = sum(
            (source_book / Path(*PurePosixPath(relative).parts)).stat().st_size
            for relative in source_relatives
            if (source_book / Path(*PurePosixPath(relative).parts)).is_file()
        )
        required_bytes = source_bytes + total_video_bytes + 32 * 1024 * 1024
        if package_path:
            required_bytes += source_bytes + total_video_bytes
    available = shutil.disk_usage(output_book.parent).free
    if available < required_bytes:
        raise ResourceLimitError(
            f"Publishing needs about {format_megabytes(required_bytes)}, but only "
            f"{format_megabytes(available)} is free."
        )
    reporter.record(
        "preflight_completed",
        {
            "page_count": page_count,
            "video_count": len(items),
            "required_bytes": required_bytes,
            "available_bytes": available,
            "declared_files": len(source_declared),
            "manifest_recoveries": len(publication_plan.manifest_recoveries),
            "manifest_prunings": len(publication_plan.manifest_prunings),
        },
    )

    if progress_callback:
        progress_callback(job_id, "job_started", {"command": "publish", "total": len(items)})
    reporter.phase("preflight", 3, "ADT paths, permissions, and storage checks passed")
    reporter.phase("media_validation", 5, "Validating compressed videos")
    for position, item in enumerate(items, start=1):
        reporter.check_cancelled()
        if progress_callback:
            progress_callback(job_id, "item_started", {"source": str(item.source)})
        if validate_media:
            _validate_media(item, maximum_bytes, probe_path)
        reporter.phase(
            "media_validation",
            5 + position / len(items) * 15,
            f"Validated video {position} of {len(items)}",
            current=item.source.name,
        )

    stage = output_book.parent / f".{output_book.name}.adt-publish-{uuid.uuid4().hex}.tmp"
    package_temporary = package_path.with_name(f".{package_path.name}.{uuid.uuid4().hex}.tmp") if package_path else None
    checksum_temporary = checksum_path.with_name(f".{checksum_path.name}.{uuid.uuid4().hex}.tmp") if checksum_path else None
    published: list[PublishedVideo] = []
    committed_output = False
    committed_package = False
    try:
        stage.mkdir()
        video_relative_root = f"content/i18n/{selected_language}/video"
        reporter.check_cancelled()
        reporter.phase("staging", 22, "Preparing publication files")
        if in_place:
            _copy_in_place_stage_sources(
                source_book,
                stage,
                source_declared,
                publication_plan.page_hrefs,
                publication_plan.offline_resource_files,
                publication_plan.active_offline_preloaders,
                selected_language,
                publication_plan.active_runtime_files,
                reporter,
            )
        else:
            _copy_manifest_site(
                source_book,
                stage,
                skip_prefix=video_relative_root if mode == "replace" else None,
                page_hrefs=publication_plan.page_hrefs,
                offline_resource_files=publication_plan.offline_resource_files,
                active_offline_preloaders=publication_plan.active_offline_preloaders,
                active_runtime_files=publication_plan.active_runtime_files,
            )
        stage_video_root = stage / "content" / "i18n" / selected_language / "video"
        stage_video_root.mkdir(parents=True, exist_ok=True)
        mappings: dict[str, str] = (
            dict(publication_plan.existing_mappings) if mode == "merge" else {}
        )
        copied_video_bytes = 0
        for position, item in enumerate(items, start=1):
            reporter.check_cancelled()
            destination = stage_video_root / item.filename
            digest = _copy_and_hash_video(
                item.source,
                destination,
                reporter=reporter,
                completed_bytes=copied_video_bytes,
                total_bytes=total_video_bytes,
            )
            copied_video_bytes += item.size_bytes
            published.append(
                PublishedVideo(
                    source=item.source,
                    output=output_book / destination.relative_to(stage),
                    page_index=item.page_index,
                    key=item.key,
                    size_bytes=item.size_bytes,
                    sha256=digest,
                )
            )
            mappings[item.key] = item.filename
            if progress_callback:
                progress_callback(
                    job_id,
                    "item_completed",
                    {"source": str(item.source), "status": "staged", "size_bytes": item.size_bytes},
                )
            reporter.phase(
                "staging",
                25 + position / len(items) * 35,
                f"Staged video {position} of {len(items)}",
                current=item.filename,
            )

        reporter.check_cancelled()
        reporter.phase("metadata", 64, "Updating ADT video mappings and configuration")
        staged_mappings_path = stage / "content" / "i18n" / selected_language / "videos.json"
        if staged_mappings_path.is_file():
            _write_json_preserving_style(staged_mappings_path, mappings)
        else:
            _write_json(staged_mappings_path, mappings)
        staged_config_path = stage / "assets" / "config.json"
        staged_config = _read_json(staged_config_path, "assets/config.json")
        if not isinstance(staged_config, dict):
            raise PublishFailedError("Staged assets/config.json must contain an object.")
        features = staged_config.setdefault("features", {})
        if not isinstance(features, dict):
            raise PublishFailedError("config.json features must contain an object.")
        features["signLanguage"] = True
        features["readAloud"] = True
        bundle_version = _advance_cache_version(staged_config)
        _write_json_preserving_style(staged_config_path, staged_config)
        reporter.phase("runtime", 69, "Installing accessibility compatibility adapters")
        adapter_relatives = install_accessibility_adapters(
            stage,
            page_hrefs=publication_plan.page_hrefs,
            active_runtime_files=publication_plan.active_runtime_files,
        )
        reporter.check_cancelled()
        reporter.phase("offline", 74, "Refreshing offline pages and cache data")
        offline_relatives = _synchronize_offline_preloader(
            stage,
            language=selected_language,
            bundle_version=bundle_version,
            page_hrefs=publication_plan.page_hrefs,
            active_offline_preloaders=publication_plan.active_offline_preloaders,
            offline_preloader_recoveries=publication_plan.offline_preloader_recoveries,
        )
        reporter.check_cancelled()
        reporter.phase("staged_validation", 80, "Validating staged publication files")
        if in_place:
            _update_in_place_manifest(
                source_book,
                stage,
                language=selected_language,
                declared=source_declared,
                page_hrefs=publication_plan.page_hrefs,
                offline_resource_files=publication_plan.offline_resource_files,
                active_offline_preloaders=publication_plan.active_offline_preloaders,
                videos=items,
                mode=mode,
            )
            validation = _validate_staged_in_place(
                source_book,
                stage,
                language=selected_language,
                mode=mode,
            )
        else:
            _update_manifest(stage)
            validation = validate_adt_website(stage, language=selected_language)
        validate_adapter_order(
            stage,
            page_hrefs=publication_plan.page_hrefs,
            active_runtime_files=publication_plan.active_runtime_files,
        )
        _verify_baselines(source_book, baselines)
        validate_staged_diff_contract(
            source_book,
            stage,
            plan=publication_plan,
            cache_version=bundle_version,
        )
        validate_staged_generated_site(
            source_book,
            stage,
            language=selected_language,
            page_hrefs=publication_plan.page_hrefs,
            active_runtime_files=publication_plan.active_runtime_files,
            active_offline_preloaders=publication_plan.active_offline_preloaders,
        )
        expected_video_count = len(mappings)
        if validation["video_count"] != expected_video_count:
            raise PublishFailedError("Published website video count changed during validation.")

        package_result: PackageResult | None = None
        if package_path and package_temporary and checksum_path and checksum_temporary:
            entry_count, package_hash = build_deployment_package(stage, package_temporary)
            checksum_temporary.write_text(f"{package_hash}  {package_path.name}\n", encoding="ascii")
            package_result = PackageResult(
                path=package_path,
                checksum_path=checksum_path,
                size_bytes=package_temporary.stat().st_size,
                sha256=package_hash,
                entry_count=entry_count,
            )

        if in_place:
            reporter.check_cancelled()
            reporter.phase(
                "commit",
                85,
                "Beginning the short repository transaction",
                cancellable=False,
            )
            _commit_in_place(
                stage,
                source_book,
                selected_language,
                tuple(dict.fromkeys((
                    Path("assets") / "config.json",
                    Path("content") / "i18n" / selected_language / "videos.json",
                    *(Path("content") / "i18n" / selected_language / "video" / item.filename for item in items),
                    *adapter_relatives,
                    *offline_relatives,
                    Path("imsmanifest.xml"),
                ))),
                tuple(
                    Path(*PurePosixPath(relative).parts)
                    for relative in publication_plan.removals
                ),
                baselines,
                publication_plan.zip_sentinels,
                publication_plan.page_hrefs,
                publication_plan.active_runtime_files,
                publication_plan.active_offline_preloaders,
                reporter,
            )
        else:
            reporter.phase("commit", 90, "Publishing the new ADT website copy", cancellable=False)
            stage.replace(output_book)
            committed_output = True
        if package_path and package_temporary and checksum_path and checksum_temporary:
            _commit_file_no_clobber(package_temporary, package_path)
            committed_package = True
            _commit_file_no_clobber(checksum_temporary, checksum_path)
        result = PublishResult(
            job_id=job_id,
            source_book=source_book,
            output_book=output_book,
            language=selected_language,
            bundle_version=bundle_version,
            videos=tuple(published),
            package=package_result,
            diagnostic_log=reporter.log_path,
        )
        reporter.phase("completed", 100, "ADT publishing completed", cancellable=False)
        reporter.record("job_completed", {"output": str(output_book), "videos": len(published)})
        if progress_callback:
            progress_callback(
                job_id,
                "job_completed",
                {"ok": True, "exit_code": int(ExitCode.SUCCESS), "output": str(output_book)},
            )
        return result
    except Exception as exc:
        reporter.record("job_failed", {"error_type": type(exc).__name__, "error": str(exc)})
        if stage.exists():
            _remove_generated_directory(stage, output_book.parent)
        if package_temporary and package_temporary.exists():
            package_temporary.unlink()
        if checksum_temporary and checksum_temporary.exists():
            checksum_temporary.unlink()
        if committed_package and package_path and package_path.exists():
            package_path.unlink()
        if committed_output and output_book.exists():
            generated = output_book.parent / f".{output_book.name}.adt-publish-{uuid.uuid4().hex}.tmp"
            output_book.replace(generated)
            _remove_generated_directory(generated, output_book.parent)
        raise
