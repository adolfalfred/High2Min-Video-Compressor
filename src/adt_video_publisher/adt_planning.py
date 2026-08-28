"""Read-only ADT compatibility analysis and page-video mapping plans."""

from __future__ import annotations

import csv
import hashlib
import json
import posixpath
import re
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from .errors import InvalidInputError, PublishFailedError
from .processes import hidden_process_options

IMS_NAMESPACE: Final = "http://www.imsproject.org/xsd/imscp_rootv1p1p2"
NUMBER_GROUP_PATTERN: Final = re.compile(r"[0-9]+")
SCRIPT_SOURCE_PATTERN: Final = re.compile(
    r"<script\b[^>]*\bsrc\s*=\s*(['\"])(?P<src>[^'\"]+)\1[^>]*>", re.IGNORECASE
)
STYLESHEET_PATTERN: Final = re.compile(
    r"<link\b[^>]*\bhref\s*=\s*(['\"])(?P<href>[^'\"]+)\1[^>]*>", re.IGNORECASE
)
APPROVED_HELPERS: Final = {
    "media": "assets/media-playback-independence.js",
    "sign_script": "assets/sign-language-video.js",
    "sign_style": "assets/sign-language-video.css",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublishFailedError(f"{label} is unreadable or invalid JSON: '{path}'.") from exc


def _safe_relative(value: str) -> str:
    if not value or "\\" in value:
        raise PublishFailedError(f"ADT contains an invalid relative path: {value!r}.")
    path = PurePosixPath(value.split("?", 1)[0].split("#", 1)[0])
    while path.parts and path.parts[0] == ".":
        path = PurePosixPath(*path.parts[1:])
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PublishFailedError(f"ADT contains an unsafe relative path: '{value}'.")
    return path.as_posix()


def _page_hrefs(book: Path) -> tuple[str, ...]:
    document = _load_json(book / "content" / "pages.json", "content/pages.json")
    if not isinstance(document, list) or not document:
        raise PublishFailedError("content/pages.json must be a non-empty array.")
    hrefs: list[str] = []
    for position, item in enumerate(document, start=1):
        if not isinstance(item, dict) or not isinstance(item.get("href"), str):
            raise PublishFailedError(f"Page entry {position} has no valid href.")
        href = _safe_relative(item["href"])
        if not (book / Path(*PurePosixPath(href).parts)).is_file():
            raise PublishFailedError(f"Page entry {position} points to a missing file: '{href}'.")
        hrefs.append(href)
    return tuple(hrefs)


def _select_language(book: Path, language: str | None) -> tuple[dict[str, object], str]:
    document = _load_json(book / "assets" / "config.json", "assets/config.json")
    if not isinstance(document, dict):
        raise PublishFailedError("assets/config.json must contain an object.")
    languages = document.get("languages")
    if not isinstance(languages, dict):
        raise PublishFailedError("assets/config.json has no languages object.")
    selected = language or languages.get("default")
    available = languages.get("available")
    if not isinstance(selected, str) or not selected:
        raise PublishFailedError("No publication language was supplied or configured.")
    if not isinstance(available, list) or selected not in available:
        raise PublishFailedError(f"Language '{selected}' is not listed in config.json.")
    if not (book / "content" / "i18n" / selected).is_dir():
        raise PublishFailedError(f"Language content directory is missing for '{selected}'.")
    return document, selected


def _manifest_files(book: Path) -> tuple[str, ...]:
    path = book / "imsmanifest.xml"
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise PublishFailedError(f"ADT manifest is unreadable: '{path}'.") from exc
    resources = [node for node in root.iter() if node.tag == f"{{{IMS_NAMESPACE}}}resource"]
    if len(resources) != 1 or resources[0].get("href") != "index.html":
        raise PublishFailedError("ADT requires one SCORM resource that launches index.html.")
    files: list[str] = []
    seen: set[str] = set()
    for node in resources[0]:
        if node.tag != f"{{{IMS_NAMESPACE}}}file":
            continue
        relative = _safe_relative(node.get("href") or "")
        if relative.casefold() in seen:
            raise PublishFailedError(f"Manifest declares a duplicate file: '{relative}'.")
        seen.add(relative.casefold())
        files.append(relative)
    return tuple(files)


def _mapping_rows(mapping_file: Path) -> dict[str, int]:
    if not mapping_file.is_file():
        raise InvalidInputError(f"Page mapping file does not exist: '{mapping_file}'.")
    values: dict[str, object]
    if mapping_file.suffix.lower() == ".json":
        loaded = _load_json(mapping_file, "Page mapping file")
        if isinstance(loaded, dict):
            values = loaded
        elif isinstance(loaded, list):
            values = {}
            for position, row in enumerate(loaded, start=1):
                if not isinstance(row, dict) or not isinstance(row.get("source"), str):
                    raise InvalidInputError(f"Mapping row {position} needs source and page fields.")
                values[row["source"]] = row.get("page")
        else:
            raise InvalidInputError("JSON page mapping must be an object or an array of rows.")
    elif mapping_file.suffix.lower() == ".csv":
        values = {}
        try:
            with mapping_file.open("r", encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                if not reader.fieldnames or not {"source", "page"}.issubset(reader.fieldnames):
                    raise InvalidInputError("CSV page mapping needs source and page columns.")
                for row in reader:
                    values[str(row.get("source", ""))] = row.get("page")
        except OSError as exc:
            raise InvalidInputError(f"Page mapping file is unreadable: '{mapping_file}'.") from exc
    else:
        raise InvalidInputError("Page mapping file must use .json or .csv.")

    result: dict[str, int] = {}
    for source, raw_page in values.items():
        if not isinstance(source, str) or not source.strip():
            raise InvalidInputError("Every page mapping source must be a non-empty filename.")
        try:
            page = int(raw_page)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise InvalidInputError(f"Mapping for '{source}' must use a positive page number.") from exc
        if page < 1:
            raise InvalidInputError(f"Mapping for '{source}' must use a positive page number.")
        key = source.casefold()
        if key in result:
            raise InvalidInputError(f"Page mapping repeats source '{source}'.")
        result[key] = page
    return result


@dataclass(frozen=True, slots=True)
class PlannedVideo:
    source: Path
    source_filename: str
    page_index: int
    page_href: str
    mapping_key: str
    destination_filename: str
    size_bytes: int

    def to_dict(self) -> dict[str, object]:
        document = asdict(self)
        document["source"] = str(self.source)
        return document


def plan_videos(
    videos: str | Path,
    *,
    page_hrefs: tuple[str, ...],
    recursive: bool = False,
    mapping_file: str | Path | None = None,
) -> tuple[PlannedVideo, ...]:
    """Map MP4 files to ADT spine positions without assuming a page_ prefix."""

    root = Path(videos).expanduser().resolve()
    if not root.is_dir():
        raise InvalidInputError(f"Compressed video directory does not exist: '{root}'.")
    explicit = _mapping_rows(Path(mapping_file).expanduser().resolve()) if mapping_file else None
    candidates = root.rglob("*.mp4") if recursive else root.glob("*.mp4")
    items: list[PlannedVideo] = []
    pages: dict[int, str] = {}
    discovered_names: set[str] = set()
    for path in sorted(candidates, key=lambda item: str(item).casefold()):
        if not path.is_file():
            continue
        discovered_names.add(path.name.casefold())
        if explicit is not None:
            try:
                page = explicit[path.name.casefold()]
            except KeyError as exc:
                raise InvalidInputError(f"Page mapping has no entry for video '{path.name}'.") from exc
        else:
            groups = NUMBER_GROUP_PATTERN.findall(path.stem)
            if len(groups) != 1:
                raise InvalidInputError(
                    f"Video '{path.name}' must contain exactly one positive page number, or use --mapping."
                )
            page = int(groups[0])
            if page < 1:
                raise InvalidInputError(f"Video '{path.name}' maps to invalid page zero.")
        if page > len(page_hrefs):
            raise InvalidInputError(
                f"Video '{path.name}' maps to page {page}, but the ADT spine has {len(page_hrefs)} pages."
            )
        if page in pages:
            raise InvalidInputError(
                f"Videos '{pages[page]}' and '{path.name}' both map to ADT page {page}."
            )
        size = path.stat().st_size
        if size <= 0:
            raise InvalidInputError(f"Video is empty: '{path}'.")
        pages[page] = path.name
        items.append(
            PlannedVideo(
                source=path.resolve(),
                source_filename=path.name,
                page_index=page,
                page_href=page_hrefs[page - 1],
                mapping_key=f"video-{page}",
                destination_filename=f"page_{page}.mp4",
                size_bytes=size,
            )
        )
    if not items:
        raise InvalidInputError(f"No MP4 videos were found in '{root}'.")
    if explicit is not None:
        unused = sorted(set(explicit) - discovered_names)
        if unused:
            raise InvalidInputError(f"Page mapping references missing video '{unused[0]}'.")
    return tuple(sorted(items, key=lambda item: item.page_index))


def _git_state(book: Path) -> dict[str, object]:
    try:
        root = subprocess.run(
            ["git", "-C", str(book), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            **hidden_process_options(),
        )
    except (OSError, subprocess.SubprocessError):
        return {"repository": False, "dirty": False, "root": None, "changed_count": 0}
    if root.returncode != 0:
        return {"repository": False, "dirty": False, "root": None, "changed_count": 0}
    status = subprocess.run(
        ["git", "-C", str(book), "status", "--porcelain=v1", "--untracked-files=normal"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        **hidden_process_options(),
    )
    lines = tuple(line for line in status.stdout.splitlines() if line.strip())
    return {
        "repository": True,
        "dirty": bool(lines),
        "root": root.stdout.strip(),
        "changed_count": len(lines),
    }


def _inline_format(path: Path) -> str:
    if not path.is_file():
        return "absent"
    try:
        source = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return "unreadable"
    match = re.search(r"\b(?:var|let|const)\s+INLINE\s*=", source)
    return "javascript-object" if match else "unsupported"


def _offline_resource_files(path: Path) -> tuple[str, ...]:
    """Return existing local HTML/JSON sources represented by the INLINE payload."""

    if not path.is_file():
        return ()
    try:
        source = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return ()
    assignment = re.search(r"\b(?:var|let|const)\s+INLINE\s*=\s*", source)
    if assignment is None:
        return ()
    try:
        payload, _end = json.JSONDecoder().raw_decode(source, assignment.end())
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(payload, dict):
        return ()
    root = path.parent.parent
    resources: set[str] = set()
    for key in payload:
        if not isinstance(key, str):
            continue
        try:
            relative = _safe_relative(key.removeprefix("./"))
        except PublishFailedError:
            continue
        if PurePosixPath(relative).suffix.casefold() not in {".html", ".json"}:
            continue
        if (root / Path(*PurePosixPath(relative).parts)).is_file():
            resources.add(relative)
    return tuple(sorted(resources, key=str.casefold))


def _active_assets(book: Path, hrefs: Iterable[str]) -> tuple[tuple[str, ...], dict[str, list[str]]]:
    runtime: set[str] = set()
    locations: dict[str, list[str]] = {value: [] for value in APPROVED_HELPERS.values()}
    for href in hrefs:
        path = book / Path(*PurePosixPath(href).parts)
        try:
            source = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue
        for match in SCRIPT_SOURCE_PATTERN.finditer(source):
            raw = match.group("src").split("?", 1)[0].split("#", 1)[0]
            relative = posixpath.normpath(
                raw.lstrip("/") if raw.startswith("/")
                else posixpath.join(PurePosixPath(href).parent.as_posix(), raw)
            )
            relative = _safe_relative(relative)
            if PurePosixPath(relative).name.startswith("base.bundle") and relative.endswith(".js"):
                runtime.add(relative)
            if relative in locations:
                locations[relative].append(href)
        for match in STYLESHEET_PATTERN.finditer(source):
            raw = match.group("href").split("?", 1)[0].split("#", 1)[0]
            relative = posixpath.normpath(
                raw.lstrip("/") if raw.startswith("/")
                else posixpath.join(PurePosixPath(href).parent.as_posix(), raw)
            )
            relative = _safe_relative(relative)
            if relative in locations:
                locations[relative].append(href)
    return tuple(sorted(runtime, key=str.casefold)), locations


@dataclass(frozen=True, slots=True)
class AdtPublishPlan:
    book: Path
    video_root: Path
    language: str
    mode: str
    page_hrefs: tuple[str, ...]
    videos: tuple[PlannedVideo, ...]
    existing_mappings: dict[str, str]
    active_runtime_files: tuple[str, ...]
    helper_files: dict[str, dict[str, object]]
    offline_preloader_format: str
    offline_resource_files: tuple[str, ...]
    manifest_file_count: int
    manifest_recoveries: tuple[str, ...]
    manifest_prunings: tuple[str, ...]
    git: dict[str, object]
    mutations: tuple[str, ...]
    removals: tuple[str, ...]
    warnings: tuple[str, ...]
    blockers: tuple[str, ...]
    zip_sentinels: dict[str, str]

    @property
    def ready(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "ready": self.ready,
            "book": str(self.book),
            "video_root": str(self.video_root),
            "language": self.language,
            "mode": self.mode,
            "page_count": len(self.page_hrefs),
            "page_hrefs": list(self.page_hrefs),
            "videos": [item.to_dict() for item in self.videos],
            "existing_mappings": dict(self.existing_mappings),
            "active_runtime_files": list(self.active_runtime_files),
            "helper_files": self.helper_files,
            "offline_preloader_format": self.offline_preloader_format,
            "offline_resource_files": list(self.offline_resource_files),
            "manifest_file_count": self.manifest_file_count,
            "manifest_recoveries": list(self.manifest_recoveries),
            "manifest_prunings": list(self.manifest_prunings),
            "git": self.git,
            "mutations": list(self.mutations),
            "removals": list(self.removals),
            "warnings": list(self.warnings),
            "blockers": list(self.blockers),
            "zip_sentinels": dict(self.zip_sentinels),
        }


def analyze_adt_publish(
    videos: str | Path,
    *,
    book: str | Path,
    language: str | None = None,
    recursive: bool = False,
    mapping_file: str | Path | None = None,
    mode: str = "merge",
) -> AdtPublishPlan:
    """Build an exact, non-mutating ADT publication preview."""

    if mode not in {"merge", "replace"}:
        raise InvalidInputError("Publish mode must be 'merge' or 'replace'.")
    root = Path(book).expanduser().resolve()
    video_root = Path(videos).expanduser().resolve()
    required = (root / "index.html", root / "imsmanifest.xml", root / "assets" / "config.json")
    if not root.is_dir() or any(not path.is_file() for path in required):
        raise PublishFailedError(f"The selected folder is not a complete ADT website: '{root}'.")
    hrefs = _page_hrefs(root)
    _config, selected = _select_language(root, language)
    planned = plan_videos(
        video_root,
        page_hrefs=hrefs,
        recursive=recursive,
        mapping_file=mapping_file,
    )
    mapping_path = root / "content" / "i18n" / selected / "videos.json"
    existing_document = _load_json(mapping_path, f"{selected}/videos.json") if mapping_path.is_file() else {}
    if not isinstance(existing_document, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in existing_document.items()
    ):
        raise PublishFailedError(f"{selected}/videos.json must contain string mappings.")
    existing = dict(existing_document)
    manifest = _manifest_files(root)
    runtime, helper_locations = _active_assets(root, hrefs)
    blockers: list[str] = []
    warnings: list[str] = []
    if not runtime:
        blockers.append("No active assets/base.bundle*.js runtime is referenced by the ADT pages.")
    for relative in runtime:
        if not (root / Path(*PurePosixPath(relative).parts)).is_file():
            blockers.append(f"Active runtime is missing: '{relative}'.")
    offline_format = _inline_format(root / "assets" / "offline-preloader.js")
    offline_resources = _offline_resource_files(root / "assets" / "offline-preloader.js")
    manifest_keys = {relative.casefold() for relative in manifest}
    required_sources = {
        *hrefs,
        *runtime,
        *offline_resources,
        "assets/config.json",
        "content/pages.json",
        f"content/i18n/{selected}/videos.json",
    }
    if (root / "assets" / "offline-preloader.js").is_file():
        required_sources.add("assets/offline-preloader.js")
    manifest_recoveries = tuple(
        relative
        for relative in sorted(required_sources, key=str.casefold)
        if relative.casefold() not in manifest_keys
        and (root / Path(*PurePosixPath(relative).parts)).is_file()
    )
    manifest_prunings = tuple(
        relative
        for relative in manifest
        if not (root / Path(*PurePosixPath(relative).parts)).is_file()
    )
    if offline_format in {"unsupported", "unreadable"}:
        blockers.append("The offline preloader INLINE map cannot be updated safely.")
    git = _git_state(root)
    if git.get("dirty"):
        warnings.append(
            f"Git has {git.get('changed_count')} existing change(s); publishing will preserve unrelated files."
        )
    if len(runtime) > 1:
        warnings.append("Different pages reference more than one active runtime bundle.")
    if manifest_recoveries:
        warnings.append(
            f"The manifest omits {len(manifest_recoveries)} required active/offline resource(s); "
            "High2Min will recover their declarations during publishing."
        )
    if manifest_prunings:
        warnings.append(
            f"The manifest contains {len(manifest_prunings)} stale declaration(s) for missing files; "
            "High2Min will remove those declarations during publishing."
        )

    helper_files: dict[str, dict[str, object]] = {}
    mutations: set[str] = {
        "assets/config.json",
        f"content/i18n/{selected}/videos.json",
        "imsmanifest.xml",
    }
    if offline_format != "absent":
        mutations.add("assets/offline-preloader.js")
    for kind, relative in APPROVED_HELPERS.items():
        path = root / Path(*PurePosixPath(relative).parts)
        present = path.is_file()
        helper_files[kind] = {
            "path": relative,
            "present": present,
            "sha256": _sha256(path) if present else None,
            "referenced_pages": list(helper_locations[relative]),
        }
        if not present:
            mutations.add(relative)
    for href in hrefs:
        path = root / Path(*PurePosixPath(href).parts)
        source = path.read_text(encoding="utf-8-sig")
        needs_query_update = "base.bundle" in source or "offline-preloader.js" in source
        needs_helpers = any(href not in helper_locations[value] for value in APPROVED_HELPERS.values())
        if needs_query_update or needs_helpers:
            mutations.add(href)

    desired = {item.mapping_key: item.destination_filename for item in planned}
    removals: list[str] = []
    if mode == "replace":
        for key, filename in existing.items():
            if key not in desired:
                removals.append(f"content/i18n/{selected}/video/{filename}")
    for item in planned:
        mutations.add(f"content/i18n/{selected}/video/{item.destination_filename}")
    zip_sentinels = {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in root.rglob("*.zip")
        if path.is_file()
    }
    return AdtPublishPlan(
        book=root,
        video_root=video_root,
        language=selected,
        mode=mode,
        page_hrefs=hrefs,
        videos=planned,
        existing_mappings=existing,
        active_runtime_files=runtime,
        helper_files=helper_files,
        offline_preloader_format=offline_format,
        offline_resource_files=offline_resources,
        manifest_file_count=len(manifest),
        manifest_recoveries=manifest_recoveries,
        manifest_prunings=manifest_prunings,
        git=git,
        mutations=tuple(sorted(mutations, key=str.casefold)),
        removals=tuple(sorted(set(removals), key=str.casefold)),
        warnings=tuple(warnings),
        blockers=tuple(blockers),
        zip_sentinels=zip_sentinels,
    )
