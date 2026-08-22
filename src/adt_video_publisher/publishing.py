"""Transactional ADT website publishing and deterministic deployment packaging."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Final
import xml.etree.ElementTree as ET

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
PAGE_VIDEO_PATTERN: Final = re.compile(r"^page_([1-9][0-9]*)\.mp4$", re.IGNORECASE)
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
    OFFLINE_PRELOADER_RELATIVE,
)
OFFLINE_INLINE_MARKER: Final = "var INLINE = "
OFFLINE_INLINE_END_MARKER: Final = ";\n  var BASE_DIR"
TRANSACTION_SCHEMA_VERSION: Final = 1
RUNTIME_SCRIPT_PATTERN: Final = re.compile(
    r'(?P<path>(?:\./)?assets/base\.bundle(?:\.[A-Za-z0-9_-]+)*\.js)'
    r'(?:\?[^"\'<>\s]*)?'
)
OFFLINE_SCRIPT_PATTERN: Final = re.compile(
    r'(?P<path>(?:\./)?assets/offline-preloader\.js)(?:\?[^"\'<>\s]*)?'
)
VIDEO_PAUSES_FOR_AUDIO_PATTERN: Final = re.compile(
    r'[$A-Za-z_][$\w]*\s*===\s*"tts"\s*&&\s*[$A-Za-z_][$\w]*\.current\?\.pause\(\)'
)
VIDEO_CLAIMS_EXCLUSIVE_AUDIO_PATTERN: Final = re.compile(
    r'onPlay\s*:\s*\(\)\s*=>\s*[$A-Za-z_][$\w]*\("sign-language"\)'
)
AUDIO_PAUSES_FOR_VIDEO_PATTERN: Final = re.compile(
    r'[$A-Za-z_][$\w]*\s*===\s*"sign-language"\s*&&\s*\('
    r'[$A-Za-z_][$\w]*\(\)\s*,\s*[$A-Za-z_][$\w]*\(!1\)\s*,\s*'
    r'[$A-Za-z_][$\w]*\(0\)\s*,\s*[$A-Za-z_][$\w]*\(!1\)\s*\)'
)
AUDIO_START_SUCCESS_PATTERN: Final = re.compile(
    r'(?P<state>[$A-Za-z_][$\w]*\("tts"\)\s*,\s*[$A-Za-z_][$\w]*\(!0\))'
    r'(?!\s*,\s*document\.querySelectorAll\("video\[autoplay\]"\))'
)
LEGACY_RESUME_SIGN_VIDEO_SNIPPET: Final = (
    'document.querySelectorAll("video").forEach(e=>{'
    'e.paused&&e.play().catch(()=>{})})'
)
RESUME_SIGN_VIDEO_SNIPPET: Final = (
    'document.querySelectorAll("video[autoplay]").forEach(e=>{'
    'e.paused&&e.play().catch(()=>{})})'
)


@dataclass(frozen=True, slots=True)
class PageVideo:
    source: Path
    page_index: int
    key: str
    filename: str
    size_bytes: int


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


def _page_count(book: Path) -> int:
    pages_path = book / "content" / "pages.json"
    pages = _read_json(pages_path, "content/pages.json")
    if not isinstance(pages, list) or not pages:
        raise PublishFailedError("content/pages.json must be a non-empty array.")
    for position, page in enumerate(pages, start=1):
        if not isinstance(page, dict) or not isinstance(page.get("href"), str):
            raise PublishFailedError(f"Page entry {position} has no valid href.")
        href = _safe_archive_path(page["href"]).as_posix()
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
) -> tuple[PageVideo, ...]:
    """Discover strict page_N.mp4 inputs and convert them to ADT mapping keys."""

    root = _resolve_directory(videos, "Compressed video")
    candidates = root.rglob("*.mp4") if recursive else root.glob("*.mp4")
    items: list[PageVideo] = []
    page_numbers: set[int] = set()
    for path in sorted(candidates, key=lambda item: str(item).casefold()):
        if not path.is_file():
            continue
        match = PAGE_VIDEO_PATTERN.fullmatch(path.name)
        if not match:
            raise InvalidInputError(
                f"Published videos must use page_N.mp4 names; found '{path.name}'."
            )
        page_index = int(match.group(1))
        if page_index > page_count:
            raise InvalidInputError(
                f"Video '{path.name}' maps to page {page_index}, but the website has {page_count} pages."
            )
        if page_index in page_numbers:
            raise InvalidInputError(f"More than one video maps to website page {page_index}.")
        size = path.stat().st_size
        if size <= 0:
            raise InvalidInputError(f"Video is empty: '{path}'.")
        page_numbers.add(page_index)
        items.append(
            PageVideo(
                source=path.resolve(),
                page_index=page_index,
                key=f"video-{page_index}",
                filename=f"page_{page_index}.mp4",
                size_bytes=size,
            )
        )
    if not items:
        raise InvalidInputError(f"No page_N.mp4 videos were found in '{root}'.")
    return tuple(sorted(items, key=lambda item: item.page_index))


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
    reporter: _PublishReporter,
) -> None:
    """Stage only files that publishing can modify, never the complete ADT website."""

    required = {
        Path("assets") / "config.json",
        OFFLINE_PRELOADER_RELATIVE,
        Path("imsmanifest.xml"),
    }
    for relative_text in declared:
        relative = Path(*PurePosixPath(relative_text).parts)
        if relative.suffix.lower() == ".html":
            required.add(relative)
    for runtime in (source / "assets").glob(RUNTIME_BUNDLE_PATTERN):
        if runtime.is_file():
            required.add(runtime.relative_to(source))
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
    tree, _root, resource = _manifest_tree(source_manifest)
    ordered = sorted(files, key=lambda value: (value != "index.html", value.casefold()))
    seen: set[str] = set()
    for child in list(resource):
        if child.tag == f"{{{IMS_NAMESPACE}}}file":
            resource.remove(child)
    for relative in ordered:
        normalized = _safe_archive_path(relative).as_posix()
        key = normalized.casefold()
        if key in seen:
            raise PublishFailedError(f"Publication would create a duplicate manifest path: '{normalized}'.")
        seen.add(key)
        ET.SubElement(resource, f"{{{IMS_NAMESPACE}}}file", {"href": normalized})
    ET.register_namespace("", IMS_NAMESPACE)
    ET.register_namespace("adlcp", ADLCP_NAMESPACE)
    ET.register_namespace("xsi", XSI_NAMESPACE)
    ET.indent(tree, space="  ")
    destination.parent.mkdir(parents=True, exist_ok=True)
    tree.write(destination, encoding="utf-8", xml_declaration=True)
    return tuple(ordered)


def _update_in_place_manifest(
    source: Path,
    stage: Path,
    *,
    language: str,
    declared: tuple[str, ...],
    videos: tuple[PageVideo, ...],
) -> tuple[str, ...]:
    prefix = f"content/i18n/{language}/video/".casefold()
    files = {relative for relative in declared if not relative.casefold().startswith(prefix)}
    files.update(f"content/i18n/{language}/video/{item.filename}" for item in videos)
    files.add(f"content/i18n/{language}/videos.json")
    for relative in ESSENTIAL_SITE_RELATIVES:
        if (source / relative).is_file() or (stage / relative).is_file():
            files.add(relative.as_posix())
    for runtime in (stage / "assets").glob(RUNTIME_BUNDLE_PATTERN):
        if runtime.is_file():
            files.add(runtime.relative_to(stage).as_posix())
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
        if filename != expected or not (video_root / expected).is_file():
            raise PublishFailedError(f"Staged mapping '{key}' must point to an existing '{expected}'.")
        expected_files.add(expected.casefold())
    actual_files = {
        path.name.casefold() for path in video_root.glob("*.mp4") if path.is_file()
    }
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


def _copy_manifest_site(source: Path, stage: Path, *, skip_prefix: str) -> None:
    declared = declared_manifest_files(source / "imsmanifest.xml")
    prefix = skip_prefix.casefold().rstrip("/") + "/"
    for relative in declared:
        if relative.casefold().startswith(prefix):
            continue
        source_file = source / Path(*PurePosixPath(relative).parts)
        if not source_file.is_file():
            raise PublishFailedError(f"Manifest declares a missing source file: '{relative}'.")
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
    for source_file in sorted(
        (source / "assets").glob(RUNTIME_BUNDLE_PATTERN),
        key=lambda path: path.name.casefold(),
    ):
        if not source_file.is_file():
            continue
        destination = stage / "assets" / source_file.name
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


def _enable_parallel_accessibility_media(book: Path) -> tuple[Path, ...]:
    """Keep the hand control available while allowing narration and video together."""

    assets = book / "assets"
    candidates = tuple(sorted(assets.glob(RUNTIME_BUNDLE_PATTERN), key=lambda path: path.name.casefold()))
    supported: list[Path] = []
    for path in candidates:
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise PublishFailedError(f"ADT runtime bundle is unreadable: '{path}'.") from exc
        if (
            "sign-language-label" not in source
            or "signLanguage" not in source
            or "activate-tts-label" not in source
            or "readAloud" not in source
        ):
            raise PublishFailedError(
                f"ADT runtime bundle '{path.name}' has no supported hand-sign and voice-over controls."
            )
        updated = VIDEO_PAUSES_FOR_AUDIO_PATTERN.sub("!1", source)
        updated = VIDEO_CLAIMS_EXCLUSIVE_AUDIO_PATTERN.sub("onPlay:()=>{}", updated)
        updated = AUDIO_PAUSES_FOR_VIDEO_PATTERN.sub("!1", updated)
        updated = updated.replace(
            LEGACY_RESUME_SIGN_VIDEO_SNIPPET,
            RESUME_SIGN_VIDEO_SNIPPET,
        )
        updated = AUDIO_START_SUCCESS_PATTERN.sub(
            lambda match: f"{match.group('state')},{RESUME_SIGN_VIDEO_SNIPPET}",
            updated,
        )
        if (
            VIDEO_PAUSES_FOR_AUDIO_PATTERN.search(updated)
            or VIDEO_CLAIMS_EXCLUSIVE_AUDIO_PATTERN.search(updated)
            or AUDIO_PAUSES_FOR_VIDEO_PATTERN.search(updated)
            or AUDIO_START_SUCCESS_PATTERN.search(updated)
        ):
            raise PublishFailedError(
                f"Could not make narration and sign-language video independent in '{path.name}'."
            )
        if updated != source:
            path.write_text(updated, encoding="utf-8")
        supported.append(path.relative_to(book))
    if not supported:
        raise PublishFailedError(
            "The ADT runtime has no supported sign-language hand control. "
            "Publishing stopped before changing the website."
        )
    return tuple(supported)


def _synchronize_offline_preloader(
    book: Path,
    *,
    language: str,
    bundle_version: str,
) -> tuple[Path, ...]:
    """Refresh embedded HTML/JSON so the offline layer cannot serve stale ADT settings."""

    preloader = book / OFFLINE_PRELOADER_RELATIVE
    if not preloader.is_file():
        return ()

    changed: list[Path] = []
    for html_path in sorted(book.rglob("*.html"), key=lambda path: str(path).casefold()):
        try:
            source = html_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise PublishFailedError(f"ADT HTML is unreadable: '{html_path}'.") from exc
        updated = OFFLINE_SCRIPT_PATTERN.sub(
            lambda match: f"{match.group('path')}?v={bundle_version}",
            source,
        )
        updated = RUNTIME_SCRIPT_PATTERN.sub(
            lambda match: f"{match.group('path')}?v={bundle_version}",
            updated,
        )
        if updated != source:
            html_path.write_text(updated, encoding="utf-8")
            changed.append(html_path.relative_to(book))

    try:
        preloader_source = preloader.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PublishFailedError(f"Offline preloader is unreadable: '{preloader}'.") from exc
    payload_start = preloader_source.find(OFFLINE_INLINE_MARKER)
    if payload_start < 0:
        raise PublishFailedError("Offline preloader has no INLINE resource map.")
    payload_start += len(OFFLINE_INLINE_MARKER)
    payload_end = preloader_source.find(OFFLINE_INLINE_END_MARKER, payload_start)
    if payload_end < 0:
        raise PublishFailedError("Offline preloader INLINE resource map is incomplete.")
    try:
        inline = json.loads(preloader_source[payload_start:payload_end])
    except json.JSONDecodeError as exc:
        raise PublishFailedError("Offline preloader INLINE resource map is invalid JSON.") from exc
    if not isinstance(inline, dict):
        raise PublishFailedError("Offline preloader INLINE resource map must contain an object.")

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
        relative_text = key[2:] if key.startswith("./") else key
        try:
            relative = _safe_archive_path(relative_text)
        except PublishFailedError:
            continue
        source_path = book / Path(*relative.parts)
        if not source_path.is_file():
            continue
        if source_path.suffix.lower() == ".html":
            inline[key] = source_path.read_text(encoding="utf-8")
        elif source_path.suffix.lower() == ".json":
            inline[key] = _read_json(source_path, relative.as_posix())

    serialized = json.dumps(inline, ensure_ascii=True, separators=(",", ":"))
    updated_preloader = (
        preloader_source[:payload_start]
        + serialized
        + preloader_source[payload_end:]
    )
    if updated_preloader != preloader_source:
        preloader.write_text(updated_preloader, encoding="utf-8")
        changed.append(OFFLINE_PRELOADER_RELATIVE)
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


def _commit_in_place(
    stage: Path,
    book: Path,
    language: str,
    runtime_relatives: tuple[Path, ...],
    reporter: _PublishReporter,
) -> None:
    """Atomically swap generated assets and restore them from a durable journal on failure."""

    transaction_id = uuid.uuid4().hex
    backup = book.parent / f".{book.name}.adt-publish-{transaction_id}.backup"
    journal = backup / "transaction.json"
    video_relative = Path("content") / "i18n" / language / "video"
    file_relatives = (
        Path("assets") / "config.json",
        Path("content") / "i18n" / language / "videos.json",
        *runtime_relatives,
        Path("imsmanifest.xml"),
    )
    relatives = tuple(dict.fromkeys((video_relative, *file_relatives)))
    targets = [
        {
            "relative": relative.as_posix(),
            "had_original": (book / relative).exists(),
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
        total = max(1, len(relatives))
        for position, relative in enumerate(relatives, start=1):
            staged = stage / relative
            if not staged.exists():
                raise PublishFailedError(f"Staged publication target is missing: '{relative.as_posix()}'.")
            target = book / relative
            saved = backup / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                saved.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, saved)
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
        raise original_error

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
        targets.append({"relative": relative, "had_original": value.get("had_original") is True})
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
    page_count = _page_count(source_book)
    config, selected_language = _book_configuration(source_book, language)
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
    items = discover_page_videos(video_root, page_count=page_count, recursive=recursive)
    total_video_bytes = sum(item.size_bytes for item in items)
    source_declared = declared_manifest_files(source_book / "imsmanifest.xml")
    if in_place:
        stage_source_paths: set[Path] = {
            source_book / "assets" / "config.json",
            source_book / "assets" / "offline-preloader.js",
            source_book / "imsmanifest.xml",
        }
        stage_source_paths.update(
            source_book / Path(*PurePosixPath(relative).parts)
            for relative in source_declared
            if PurePosixPath(relative).suffix.lower() == ".html"
        )
        stage_source_paths.update(
            path for path in (source_book / "assets").glob(RUNTIME_BUNDLE_PATTERN) if path.is_file()
        )
        staged_source_bytes = sum(path.stat().st_size for path in stage_source_paths if path.is_file())
        estimated_stage_bytes = total_video_bytes + staged_source_bytes
        required_bytes = estimated_stage_bytes + max(64 * 1024 * 1024, estimated_stage_bytes // 10)
    else:
        source_bytes = sum(
            (source_book / Path(*PurePosixPath(relative).parts)).stat().st_size
            for relative in source_declared
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
            _copy_in_place_stage_sources(source_book, stage, source_declared, reporter)
        else:
            _copy_manifest_site(source_book, stage, skip_prefix=video_relative_root)
        stage_video_root = stage / "content" / "i18n" / selected_language / "video"
        stage_video_root.mkdir(parents=True, exist_ok=True)
        mappings: dict[str, str] = {}
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
                    {"source": str(item.source), "status": "published", "size_bytes": item.size_bytes},
                )
            reporter.phase(
                "staging",
                25 + position / len(items) * 35,
                f"Staged video {position} of {len(items)}",
                current=item.filename,
            )

        reporter.check_cancelled()
        reporter.phase("metadata", 64, "Updating ADT video mappings and configuration")
        _write_json(stage / "content" / "i18n" / selected_language / "videos.json", mappings)
        staged_config_path = stage / "assets" / "config.json"
        staged_config = _read_json(staged_config_path, "assets/config.json")
        if not isinstance(staged_config, dict):
            raise PublishFailedError("Staged assets/config.json must contain an object.")
        features = staged_config.setdefault("features", {})
        if not isinstance(features, dict):
            raise PublishFailedError("config.json features must contain an object.")
        features["signLanguage"] = True
        features["readAloud"] = True
        incremented, bundle_version = _increment_bundle_version(config.get("bundleVersion"))
        staged_config["bundleVersion"] = incremented
        _write_json(staged_config_path, staged_config)
        reporter.phase("runtime", 69, "Updating accessibility controls")
        runtime_relatives = _enable_parallel_accessibility_media(stage)
        reporter.check_cancelled()
        reporter.phase("offline", 74, "Refreshing offline pages and cache data")
        offline_relatives = _synchronize_offline_preloader(
            stage,
            language=selected_language,
            bundle_version=bundle_version,
        )
        reporter.check_cancelled()
        reporter.phase("staged_validation", 80, "Validating staged publication files")
        if in_place:
            _update_in_place_manifest(
                source_book,
                stage,
                language=selected_language,
                declared=source_declared,
                videos=items,
            )
            validation = _validate_staged_in_place(
                source_book,
                stage,
                language=selected_language,
            )
        else:
            _update_manifest(stage)
            validation = validate_adt_website(stage, language=selected_language)
        if validation["video_count"] != len(items):
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
                tuple(dict.fromkeys((*runtime_relatives, *offline_relatives))),
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
