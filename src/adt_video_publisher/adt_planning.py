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
from .page_identity import page_section_index
from .processes import hidden_process_options
from .video_references import parse_video_reference

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
OFFLINE_PRELOADER_NAME_PATTERN: Final = re.compile(
    r"offline-preloader(?:[-._][A-Za-z0-9_-]+)*\.js$", re.IGNORECASE
)
PAGE_SECTION_FAMILY_PATTERN: Final = re.compile(
    r"^(?P<page>pg[0-9]+)_sec[0-9]+\.html$", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class PageTarget:
    spine_index: int
    href: str
    section_id: str
    declared_video_index: int

    @property
    def video_index(self) -> int:
        # A zero-valued first cover is a valid runtime key. Later zero-valued
        # covers need a unique key; their current spine position is stable and
        # remains within the runtime's normal page bounds.
        if self.declared_video_index == 0 and self.spine_index > 1:
            return self.spine_index
        return self.declared_video_index


@dataclass(frozen=True, slots=True)
class MappingTarget:
    page: int | None = None
    href: str | None = None
    section_id: str | None = None


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


def _page_targets(book: Path) -> tuple[PageTarget, ...]:
    document = _load_json(book / "content" / "pages.json", "content/pages.json")
    if not isinstance(document, list) or not document:
        raise PublishFailedError("content/pages.json must be a non-empty array.")
    targets: list[PageTarget] = []
    for position, item in enumerate(document, start=1):
        if not isinstance(item, dict) or not isinstance(item.get("href"), str):
            raise PublishFailedError(f"Page entry {position} has no valid href.")
        href = _safe_relative(item["href"])
        page_path = book / Path(*PurePosixPath(href).parts)
        if not page_path.is_file():
            raise PublishFailedError(f"Page entry {position} points to a missing file: '{href}'.")
        try:
            source = page_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise PublishFailedError(f"ADT page is unreadable: '{page_path}'.") from exc
        declared_index = page_section_index(source)
        targets.append(
            PageTarget(
                spine_index=position,
                href=href,
                section_id=str(item.get("section_id") or ""),
                declared_video_index=position if declared_index is None else declared_index,
            )
        )
    return tuple(targets)


def _page_hrefs(book: Path) -> tuple[str, ...]:
    return tuple(target.href for target in _page_targets(book))


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


def _parse_mapping_target(source: str, raw: object) -> MappingTarget:
    if isinstance(raw, dict):
        raw_page = raw.get("page")
        raw_href = raw.get("target_href", raw.get("href"))
        raw_section = raw.get("target_section_id", raw.get("section_id"))
        supplied = sum(
            value is not None and value != ""
            for value in (raw_page, raw_href, raw_section)
        )
        if supplied != 1:
            raise InvalidInputError(
                f"Mapping for '{source}' must provide exactly one of page, target_href, "
                "or target_section_id."
            )
        if raw_href is not None and raw_href != "":
            if not isinstance(raw_href, str):
                raise InvalidInputError(f"Mapping target_href for '{source}' must be text.")
            return MappingTarget(href=_safe_relative(raw_href))
        if raw_section is not None and raw_section != "":
            if not isinstance(raw_section, str):
                raise InvalidInputError(f"Mapping target_section_id for '{source}' must be text.")
            return MappingTarget(section_id=raw_section.strip())
    else:
        raw_page = raw
    try:
        page = int(raw_page)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise InvalidInputError(
            f"Mapping for '{source}' must use a non-negative page number."
        ) from exc
    if page < 0:
        raise InvalidInputError(f"Mapping for '{source}' must use a non-negative page number.")
    return MappingTarget(page=page)


def _mapping_rows(mapping_file: Path) -> dict[str, MappingTarget]:
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
                    raise InvalidInputError(f"Mapping row {position} needs a source field.")
                values[row["source"]] = {
                    key: value for key, value in row.items() if key != "source"
                }
        else:
            raise InvalidInputError("JSON page mapping must be an object or an array of rows.")
    elif mapping_file.suffix.lower() == ".csv":
        values = {}
        try:
            with mapping_file.open("r", encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                target_fields = {
                    "page", "href", "target_href", "section_id", "target_section_id"
                }
                if (
                    not reader.fieldnames
                    or "source" not in reader.fieldnames
                    or not target_fields.intersection(reader.fieldnames)
                ):
                    raise InvalidInputError(
                        "CSV page mapping needs a source column and one target column."
                    )
                for row in reader:
                    values[str(row.get("source", ""))] = {
                        key: value for key, value in row.items() if key != "source"
                    }
        except OSError as exc:
            raise InvalidInputError(f"Page mapping file is unreadable: '{mapping_file}'.") from exc
    else:
        raise InvalidInputError("Page mapping file must use .json or .csv.")

    result: dict[str, MappingTarget] = {}
    for source, raw_target in values.items():
        if not isinstance(source, str) or not source.strip():
            raise InvalidInputError("Every page mapping source must be a non-empty filename.")
        key = source.casefold()
        if key in result:
            raise InvalidInputError(f"Page mapping repeats source '{source}'.")
        result[key] = _parse_mapping_target(source, raw_target)
    return result


def _video_number(path: Path) -> int:
    groups = NUMBER_GROUP_PATTERN.findall(path.stem)
    if len(groups) != 1:
        raise InvalidInputError(
            f"Video '{path.name}' must contain exactly one page number, or use --mapping."
        )
    return int(groups[0])


def _historical_page_hrefs(
    book: Path,
    source_pages: set[int],
    current_hrefs: tuple[str, ...],
) -> tuple[dict[int, str], str | None]:
    """Recover an older spine when input numbering exceeds the current ADT spine."""

    positive = {value for value in source_pages if value > 0}
    if not positive or max(positive) <= len(current_hrefs):
        return {}, None
    expected_count = max(positive)
    try:
        root_result = subprocess.run(
            ["git", "-C", str(book), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            **hidden_process_options(),
        )
    except (OSError, subprocess.SubprocessError):
        return {}, None
    if root_result.returncode != 0:
        return {}, None
    git_root = Path(root_result.stdout.strip()).resolve()
    try:
        prefix = book.relative_to(git_root).as_posix()
    except ValueError:
        return {}, None
    blob = f"{prefix}/content/pages.json" if prefix else "content/pages.json"
    try:
        history = subprocess.run(
            ["git", "-C", str(git_root), "log", "--format=%H", "--", blob],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            **hidden_process_options(),
        )
    except (OSError, subprocess.SubprocessError):
        return {}, None
    if history.returncode != 0:
        return {}, None
    for commit in history.stdout.splitlines():
        commit = commit.strip()
        if not commit:
            continue
        try:
            result = subprocess.run(
                ["git", "-C", str(git_root), "show", f"{commit}:{blob}"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                **hidden_process_options(),
            )
        except (OSError, subprocess.SubprocessError):
            return {}, None
        if result.returncode != 0:
            continue
        try:
            document = json.loads(result.stdout)
        except json.JSONDecodeError:
            continue
        if not isinstance(document, list) or len(document) != expected_count:
            continue
        hrefs: list[str] = []
        valid = True
        for item in document:
            if not isinstance(item, dict) or not isinstance(item.get("href"), str):
                valid = False
                break
            try:
                hrefs.append(_safe_relative(item["href"]))
            except PublishFailedError:
                valid = False
                break
        if valid:
            return (
                {position: href for position, href in enumerate(hrefs, start=1)},
                commit[:7],
            )
    return {}, None


def _target_by_href(
    href: str,
    page_targets: tuple[PageTarget, ...],
    *,
    source_page: int | None = None,
    notes: list[str] | None = None,
) -> PageTarget:
    exact = [target for target in page_targets if target.href.casefold() == href.casefold()]
    if len(exact) == 1:
        return exact[0]
    family = PAGE_SECTION_FAMILY_PATTERN.fullmatch(PurePosixPath(href).name)
    if family is not None:
        candidates = [
            target
            for target in page_targets
            if (
                (candidate := PAGE_SECTION_FAMILY_PATTERN.fullmatch(PurePosixPath(target.href).name))
                and candidate.group("page").casefold() == family.group("page").casefold()
            )
        ]
        if len(candidates) == 1:
            target = candidates[0]
            if notes is not None:
                label = f"Historical page {source_page}" if source_page is not None else "Mapping target"
                notes.append(
                    f"{label} '{href}' was joined into '{target.href}'; verify that the "
                    "replacement video covers the complete joined page."
                )
            return target
    raise InvalidInputError(
        f"The mapped ADT page '{href}' no longer has one unambiguous current target. "
        "Use a JSON or CSV mapping with target_href."
    )


def _resolve_mapping_target(
    directive: MappingTarget,
    page_targets: tuple[PageTarget, ...],
) -> PageTarget:
    if directive.href is not None:
        return _target_by_href(directive.href, page_targets)
    if directive.section_id is not None:
        candidates = [
            target
            for target in page_targets
            if target.section_id.casefold() == directive.section_id.casefold()
        ]
        if len(candidates) != 1:
            raise InvalidInputError(
                f"Mapping section '{directive.section_id}' does not identify exactly one ADT page."
            )
        return candidates[0]
    assert directive.page is not None
    if directive.page == 0:
        return page_targets[0]
    if directive.page > len(page_targets):
        raise InvalidInputError(
            f"Mapping targets page {directive.page}, but the ADT spine has {len(page_targets)} pages."
        )
    return page_targets[directive.page - 1]


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
    page_targets: tuple[PageTarget, ...] | None = None,
    historical_page_hrefs: dict[int, str] | None = None,
    notes: list[str] | None = None,
) -> tuple[PlannedVideo, ...]:
    """Map MP4 files to ADT pages without conflating spine and runtime indices."""

    root = Path(videos).expanduser().resolve()
    if not root.is_dir():
        raise InvalidInputError(f"Compressed video directory does not exist: '{root}'.")
    explicit = _mapping_rows(Path(mapping_file).expanduser().resolve()) if mapping_file else None
    candidates = root.rglob("*.mp4") if recursive else root.glob("*.mp4")
    targets = page_targets or tuple(
        PageTarget(
            spine_index=position,
            href=href,
            section_id="",
            declared_video_index=position,
        )
        for position, href in enumerate(page_hrefs, start=1)
    )
    if len(targets) != len(page_hrefs) or any(
        target.href != page_hrefs[position]
        for position, target in enumerate(targets)
    ):
        raise InvalidInputError("The supplied ADT page targets do not match the page href list.")
    items: list[PlannedVideo] = []
    pages: dict[str, str] = {}
    video_indices: dict[int, str] = {}
    discovered_names: set[str] = set()
    for path in sorted(candidates, key=lambda item: str(item).casefold()):
        if not path.is_file():
            continue
        discovered_names.add(path.name.casefold())
        if explicit is not None:
            try:
                directive = explicit[path.name.casefold()]
            except KeyError as exc:
                raise InvalidInputError(f"Page mapping has no entry for video '{path.name}'.") from exc
            target = _resolve_mapping_target(directive, targets)
        else:
            page = _video_number(path)
            if page == 0:
                target = targets[0]
            elif historical_page_hrefs and page in historical_page_hrefs:
                target = _target_by_href(
                    historical_page_hrefs[page],
                    targets,
                    source_page=page,
                    notes=notes,
                )
            elif page <= len(targets):
                target = targets[page - 1]
            else:
                raise InvalidInputError(
                    f"Video '{path.name}' maps to historical page {page}, but the current ADT spine "
                    f"has {len(targets)} pages and no matching history was found. Use a JSON or CSV "
                    "mapping with target_href."
                )
        target_key = target.href.casefold()
        if target_key in pages:
            raise InvalidInputError(
                f"Videos '{pages[target_key]}' and '{path.name}' both map to '{target.href}'."
            )
        video_index = target.video_index
        if video_index in video_indices:
            raise InvalidInputError(
                f"Videos '{video_indices[video_index]}' and '{path.name}' would share runtime key "
                f"'video-{video_index}'. Give the target pages unique page-section-id values."
            )
        size = path.stat().st_size
        if size <= 0:
            raise InvalidInputError(f"Video is empty: '{path}'.")
        pages[target_key] = path.name
        video_indices[video_index] = path.name
        items.append(
            PlannedVideo(
                source=path.resolve(),
                source_filename=path.name,
                page_index=target.spine_index,
                page_href=target.href,
                mapping_key=f"video-{video_index}",
                destination_filename=f"page_{video_index}.mp4",
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


def _inline_object_span(source: str) -> tuple[int, int] | None:
    assignment = re.search(r"\b(?:var|let|const)\s+INLINE\s*=\s*", source)
    if assignment is None:
        return None
    start = assignment.end()
    while start < len(source) and source[start].isspace():
        start += 1
    if start >= len(source) or source[start] != "{":
        return None
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
    return None


def _inspect_offline_preloader(path: Path) -> tuple[str, dict[str, object] | None]:
    if not path.is_file():
        return "missing", None
    try:
        source = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return "unreadable", None
    span = _inline_object_span(source)
    if span is None:
        return "unsupported", None
    try:
        payload = json.loads(source[span[0]:span[1]])
    except json.JSONDecodeError:
        return "invalid-json", None
    if not isinstance(payload, dict):
        return "invalid-json", None
    return "javascript-object", payload


def _external_inline_payload(
    book: Path,
    source: str,
    referring_pages: Iterable[str],
) -> tuple[str, dict[str, object]] | None:
    """Resolve one local script loaded by a wrapper that owns the INLINE map."""

    page_hrefs = tuple(referring_pages) or ("index.html",)
    candidates: dict[str, dict[str, object]] = {}
    for match in SCRIPT_SOURCE_PATTERN.finditer(source):
        raw = match.group("src").split("?", 1)[0].split("#", 1)[0]
        if not raw or raw.startswith("//") or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", raw):
            continue
        resolved: set[str] = set()
        try:
            for href in page_hrefs:
                relative = (
                    posixpath.normpath(raw.lstrip("/"))
                    if raw.startswith("/")
                    else posixpath.normpath(
                        posixpath.join(PurePosixPath(href).parent.as_posix(), raw)
                    )
                )
                resolved.add(_safe_relative(relative))
        except PublishFailedError:
            continue
        if len(resolved) != 1:
            continue
        relative = resolved.pop()
        if PurePosixPath(relative).suffix.casefold() != ".js":
            continue
        payload_format, payload = _inspect_offline_preloader(
            book / Path(*PurePosixPath(relative).parts)
        )
        if payload_format == "javascript-object" and payload is not None:
            candidates[relative] = payload
    if len(candidates) != 1:
        return None
    return next(iter(candidates.items()))


def _inspect_active_offline_preloader(
    book: Path,
    relative: str,
    referring_pages: Iterable[str],
) -> tuple[str, dict[str, object] | None, str | None]:
    path = book / Path(*PurePosixPath(relative).parts)
    preloader_format, payload = _inspect_offline_preloader(path)
    if payload is not None:
        return preloader_format, payload, relative
    if preloader_format != "unsupported":
        return preloader_format, None, None
    try:
        source = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return "unreadable", None, None
    external = _external_inline_payload(book, source, referring_pages)
    if external is None:
        return preloader_format, None, None
    payload_relative, external_payload = external
    return "javascript-object", external_payload, payload_relative


def _offline_resource_files(book: Path, payload: dict[str, object]) -> tuple[str, ...]:
    """Return existing local HTML/JSON sources represented by an INLINE payload."""

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
        if (book / Path(*PurePosixPath(relative).parts)).is_file():
            resources.add(relative)
    return tuple(sorted(resources, key=str.casefold))


def _active_offline_preloaders(
    book: Path,
    hrefs: Iterable[str],
) -> dict[str, list[str]]:
    locations: dict[str, list[str]] = {}
    for href in hrefs:
        page = book / Path(*PurePosixPath(href).parts)
        try:
            source = page.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue
        for match in SCRIPT_SOURCE_PATTERN.finditer(source):
            raw = match.group("src").split("?", 1)[0].split("#", 1)[0]
            relative = posixpath.normpath(
                raw.lstrip("/") if raw.startswith("/")
                else posixpath.join(PurePosixPath(href).parent.as_posix(), raw)
            )
            relative = _safe_relative(relative)
            if OFFLINE_PRELOADER_NAME_PATTERN.fullmatch(PurePosixPath(relative).name):
                locations.setdefault(relative, []).append(href)
    conventional = "assets/offline-preloader.js"
    if not locations and (book / Path(*PurePosixPath(conventional).parts)).is_file():
        locations[conventional] = []
    return locations


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
    page_video_index_updates: dict[str, int]
    videos: tuple[PlannedVideo, ...]
    existing_mappings: dict[str, str]
    active_runtime_files: tuple[str, ...]
    helper_files: dict[str, dict[str, object]]
    active_offline_preloaders: tuple[str, ...]
    offline_preloader_formats: dict[str, str]
    offline_preloader_payload_files: dict[str, str]
    offline_preloader_recoveries: dict[str, str]
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
            "page_video_index_updates": dict(self.page_video_index_updates),
            "videos": [item.to_dict() for item in self.videos],
            "existing_mappings": dict(self.existing_mappings),
            "active_runtime_files": list(self.active_runtime_files),
            "helper_files": self.helper_files,
            "active_offline_preloaders": list(self.active_offline_preloaders),
            "offline_preloader_formats": dict(self.offline_preloader_formats),
            "offline_preloader_recoveries": dict(self.offline_preloader_recoveries),
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
    targets = _page_targets(root)
    hrefs = tuple(target.href for target in targets)
    _config, selected = _select_language(root, language)
    mapping_notes: list[str] = []
    historical_hrefs: dict[int, str] = {}
    historical_commit: str | None = None
    if mapping_file is None:
        candidates = video_root.rglob("*.mp4") if recursive else video_root.glob("*.mp4")
        source_pages = {
            _video_number(path)
            for path in candidates
            if path.is_file()
        }
        historical_hrefs, historical_commit = _historical_page_hrefs(
            root,
            source_pages,
            hrefs,
        )
    planned = plan_videos(
        video_root,
        page_hrefs=hrefs,
        recursive=recursive,
        mapping_file=mapping_file,
        page_targets=targets,
        historical_page_hrefs=historical_hrefs,
        notes=mapping_notes,
    )
    planned_target_hrefs = {item.page_href.casefold() for item in planned}
    page_video_index_updates = {
        target.href: target.video_index
        for target in targets
        if target.declared_video_index != target.video_index
        and target.href.casefold() in planned_target_hrefs
    }
    mapping_path = root / "content" / "i18n" / selected / "videos.json"
    existing_document = _load_json(mapping_path, f"{selected}/videos.json") if mapping_path.is_file() else {}
    if not isinstance(existing_document, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in existing_document.items()
    ):
        raise PublishFailedError(f"{selected}/videos.json must contain string mappings.")
    existing = dict(existing_document)
    desired = {item.mapping_key: item.destination_filename for item in planned}
    retained_mappings = dict(existing) if mode == "merge" else {}
    retained_mappings.update(desired)
    mapping_blockers: list[str] = []
    retained_video_sources: set[str] = set()
    parsed_existing_filenames: dict[str, str] = {}
    for key, reference in existing.items():
        try:
            parsed_existing_filenames[key] = parse_video_reference(reference).filename
        except ValueError:
            pass
    for key, reference in retained_mappings.items():
        match = re.fullmatch(r"video-(0|[1-9][0-9]*)", key)
        if match is None or int(match.group(1)) > len(hrefs):
            mapping_blockers.append(f"Existing videos.json contains an invalid key: '{key}'.")
            continue
        try:
            filename = parse_video_reference(reference).filename
        except ValueError:
            mapping_blockers.append(
                f"Existing videos.json contains an unsafe filename for '{key}'."
            )
            continue
        if key in desired:
            continue
        relative = f"content/i18n/{selected}/video/{filename}"
        retained_video_sources.add(relative)
        if not (root / Path(*PurePosixPath(relative).parts)).is_file():
            mapping_blockers.append(f"Existing mapped video is missing: '{filename}'.")
    manifest = _manifest_files(root)
    runtime, helper_locations = _active_assets(root, hrefs)
    blockers: list[str] = list(mapping_blockers)
    warnings: list[str] = list(mapping_notes)
    if historical_commit is not None:
        warnings.insert(
            0,
            f"Input numbering matches historical {len(historical_hrefs)}-page ADT spine "
            f"{historical_commit}; High2Min mapped videos by stable page hrefs to the current "
            f"{len(hrefs)}-page spine.",
        )
    for href, video_index in page_video_index_updates.items():
        warnings.append(
            f"Page '{href}' shares an unnumbered cover index; High2Min will assign "
            f"page-section-id {video_index} so its sign video remains unique."
        )
    if not runtime:
        blockers.append("No active assets/base.bundle*.js runtime is referenced by the ADT pages.")
    for relative in runtime:
        if not (root / Path(*PurePosixPath(relative).parts)).is_file():
            blockers.append(f"Active runtime is missing: '{relative}'.")
    preloader_locations = _active_offline_preloaders(root, hrefs)
    active_preloaders = tuple(sorted(preloader_locations, key=str.casefold))
    preloader_formats: dict[str, str] = {}
    preloader_payloads: dict[str, dict[str, object]] = {}
    preloader_payload_files: dict[str, str] = {}
    for relative in active_preloaders:
        preloader_format, payload, payload_relative = _inspect_active_offline_preloader(
            root,
            relative,
            preloader_locations[relative],
        )
        preloader_formats[relative] = preloader_format
        if payload is not None:
            preloader_payloads[relative] = payload
        if payload_relative is not None:
            preloader_payload_files[relative] = payload_relative

    valid_preloaders = sorted(
        preloader_payloads,
        key=lambda relative: (-len(preloader_locations[relative]), relative.casefold()),
    )
    preloader_recoveries: dict[str, str] = {}
    unrecoverable_preloaders: list[str] = []
    for relative in active_preloaders:
        preloader_format = preloader_formats[relative]
        if preloader_format == "javascript-object":
            continue
        if preloader_format == "invalid-json" and valid_preloaders:
            preloader_recoveries[relative] = valid_preloaders[0]
            continue
        unrecoverable_preloaders.append(relative)
        blockers.append(
            f"Active offline preloader '{relative}' has {preloader_format} INLINE data "
            "and cannot be updated safely."
        )

    offline_resources = tuple(sorted({
        *(
            payload_relative
            for loader, payload_relative in preloader_payload_files.items()
            if payload_relative != loader
        ),
        *(
            resource
            for payload in preloader_payloads.values()
            for resource in _offline_resource_files(root, payload)
        ),
    }, key=str.casefold))
    if not active_preloaders:
        offline_format = "absent"
    elif unrecoverable_preloaders:
        offline_format = "unsupported"
    elif preloader_recoveries:
        offline_format = "recoverable"
    elif all(value == "javascript-object" for value in preloader_formats.values()):
        offline_format = "javascript-object"
    else:
        offline_format = "unsupported"
    manifest_keys = {relative.casefold() for relative in manifest}
    required_sources = {
        *hrefs,
        *runtime,
        *active_preloaders,
        *offline_resources,
        *retained_video_sources,
        "assets/config.json",
        "content/pages.json",
        f"content/i18n/{selected}/videos.json",
    }
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
    git = _git_state(root)
    if git.get("dirty"):
        warnings.append(
            f"Git has {git.get('changed_count')} existing change(s); publishing will preserve unrelated files."
        )
    if len(runtime) > 1:
        warnings.append("Different pages reference more than one active runtime bundle.")
    if len(active_preloaders) > 1:
        warnings.append(
            f"The active pages reference {len(active_preloaders)} offline preloaders; "
            "High2Min will synchronize each one."
        )
    if preloader_recoveries:
        for relative, recovery in preloader_recoveries.items():
            warnings.append(
                f"Active offline preloader '{relative}' has invalid JSON; High2Min will "
                f"recover its resource map from '{recovery}' before publishing."
            )
    for relative, payload_relative in preloader_payload_files.items():
        if payload_relative != relative:
            warnings.append(
                f"Active offline preloader '{relative}' loads its INLINE resource map from "
                f"'{payload_relative}'; High2Min will synchronize both files."
            )
    if manifest_recoveries:
        warnings.append(
            f"The manifest omits {len(manifest_recoveries)} required website resource(s); "
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
    mutations.update(active_preloaders)
    mutations.update(preloader_payload_files.values())
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
        needs_query_update = "base.bundle" in source or "offline-preloader" in source
        needs_helpers = any(href not in helper_locations[value] for value in APPROVED_HELPERS.values())
        if needs_query_update or needs_helpers:
            mutations.add(href)
    mutations.update(page_video_index_updates)

    removals: list[str] = []
    for item in planned:
        existing_filename = parsed_existing_filenames.get(item.mapping_key)
        if (
            existing_filename is not None
            and existing_filename.casefold() != item.destination_filename.casefold()
        ):
            removals.append(f"content/i18n/{selected}/video/{existing_filename}")
    if mode == "replace":
        for key in existing:
            filename = parsed_existing_filenames.get(key)
            if key not in desired and filename is not None:
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
        page_video_index_updates=page_video_index_updates,
        videos=planned,
        existing_mappings=existing,
        active_runtime_files=runtime,
        helper_files=helper_files,
        active_offline_preloaders=active_preloaders,
        offline_preloader_formats=preloader_formats,
        offline_preloader_payload_files=preloader_payload_files,
        offline_preloader_recoveries=preloader_recoveries,
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
