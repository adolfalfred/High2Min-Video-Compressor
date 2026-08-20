"""Deterministic, integrity-checked portable release archives."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import platform
import re
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Final

RELEASE_SCHEMA_VERSION: Final = "1.0"
MANIFEST_NAME: Final = "RELEASE-MANIFEST.json"
FIXED_ZIP_TIMESTAMP: Final = (1980, 1, 1, 0, 0, 0)


class ReleaseValidationError(RuntimeError):
    """Raised when a portable release is incomplete, corrupt, or unsafe."""


def platform_tag() -> str:
    system = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(
        platform.system(), platform.system().lower()
    )
    machine = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "x86_64": "x86_64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(platform.machine().lower(), platform.machine().lower())
    tag = f"{system}-{machine}"
    if tag not in {
        "windows-x86_64",
        "windows-arm64",
        "linux-x86_64",
        "linux-arm64",
        "macos-x86_64",
        "macos-arm64",
    }:
        raise ReleaseValidationError(f"Unsupported release platform: {tag}")
    return tag


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_relative(value: str) -> PurePosixPath:
    if not value or "\\" in value:
        raise ReleaseValidationError(f"Invalid release path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseValidationError(f"Unsafe release path: {value!r}")
    return path


def _safe_symlink_target(relative: str, target: str) -> None:
    if not target or "\\" in target or PurePosixPath(target).is_absolute():
        raise ReleaseValidationError(f"Unsafe symbolic-link target: {target!r}")
    stack = list(PurePosixPath(relative).parent.parts)
    for part in PurePosixPath(target).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not stack:
                raise ReleaseValidationError(f"Symbolic link escapes the release: {relative!r}")
            stack.pop()
        else:
            stack.append(part)


def _iter_entries(root: Path) -> list[Path]:
    entries = [
        path
        for path in root.rglob("*")
        if (path.is_file() or path.is_symlink()) and path.name != MANIFEST_NAME
    ]
    return sorted(entries, key=lambda path: path.relative_to(root).as_posix().casefold())


def _entry_document(root: Path, path: Path) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    _safe_relative(relative)
    if path.is_symlink():
        target = os.readlink(path)
        _safe_symlink_target(relative, target)
        encoded = target.encode("utf-8")
        return {
            "path": relative,
            "kind": "symlink",
            "target": target,
            "size_bytes": len(encoded),
            "sha256": _sha256_bytes(encoded),
            "executable": False,
        }
    mode = path.stat().st_mode
    return {
        "path": relative,
        "kind": "file",
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "executable": bool(mode & 0o111),
    }


def write_release_manifest(
    root: Path,
    *,
    product_version: str,
    target: str,
    dependencies: dict[str, str],
) -> dict[str, Any]:
    document = {
        "schema_version": RELEASE_SCHEMA_VERSION,
        "product": "High2Min Video Compressor",
        "product_version": product_version,
        "target": target,
        "dependencies": dict(sorted(dependencies.items())),
        "files": [_entry_document(root, path) for path in _iter_entries(root)],
    }
    (root / MANIFEST_NAME).write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return document


def _load_manifest_bytes(value: bytes) -> dict[str, Any]:
    try:
        document = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseValidationError("Release manifest is not valid UTF-8 JSON.") from exc
    if not isinstance(document, dict) or document.get("schema_version") != RELEASE_SCHEMA_VERSION:
        raise ReleaseValidationError("Release manifest has an unsupported schema version.")
    files = document.get("files")
    if not isinstance(files, list) or not files:
        raise ReleaseValidationError("Release manifest contains no files.")
    return document


def verify_release_directory(root: Path) -> dict[str, Any]:
    manifest_path = root / MANIFEST_NAME
    if not root.is_dir() or not manifest_path.is_file():
        raise ReleaseValidationError(f"Release directory is incomplete: {root}")
    document = _load_manifest_bytes(manifest_path.read_bytes())
    expected = {entry["path"]: entry for entry in document["files"]}
    actual_paths = {path.relative_to(root).as_posix() for path in _iter_entries(root)}
    if actual_paths != set(expected):
        raise ReleaseValidationError("Release directory does not exactly match its manifest.")
    for relative, entry in expected.items():
        path = root / Path(*PurePosixPath(relative).parts)
        actual = _entry_document(root, path)
        if actual != entry:
            raise ReleaseValidationError(f"Release file failed integrity validation: {relative}")
    return document


def _zip_info(name: str, *, executable: bool) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, FIXED_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    mode = 0o100755 if executable else 0o100644
    info.external_attr = mode << 16
    info.flag_bits |= 0x800
    return info


def build_zip(root: Path, archive: Path) -> None:
    if any(path.is_symlink() for path in root.rglob("*")):
        raise ReleaseValidationError("ZIP releases cannot safely contain symbolic links.")
    entries = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root.parent).as_posix().casefold(),
    )
    with zipfile.ZipFile(archive, "w", allowZip64=True, compresslevel=9) as output:
        for path in entries:
            relative = path.relative_to(root.parent).as_posix()
            executable = bool(path.stat().st_mode & 0o111)
            output.writestr(_zip_info(relative, executable=executable), path.read_bytes())


def _normalized_tar_info(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 0
    return info


def build_tar_gz(root: Path, archive: Path) -> None:
    with archive.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as output:
                output.add(root, arcname=root.name, recursive=True, filter=_normalized_tar_info)


def build_release_archive(root: Path, archive: Path, *, target: str) -> None:
    if target.startswith("windows-"):
        if archive.suffix.lower() != ".zip":
            raise ReleaseValidationError("Windows releases must use .zip archives.")
        build_zip(root, archive)
    else:
        if not archive.name.endswith(".tar.gz"):
            raise ReleaseValidationError("Linux and macOS releases must use .tar.gz archives.")
        build_tar_gz(root, archive)
    verify_release_archive(archive)


def _verify_manifest_entries(document: dict[str, Any], payloads: dict[str, tuple[str, bytes]]) -> None:
    expected = {entry["path"]: entry for entry in document["files"]}
    if set(payloads) != set(expected):
        raise ReleaseValidationError("Archive contents do not exactly match its release manifest.")
    for relative, entry in expected.items():
        kind, value = payloads[relative]
        if kind != entry["kind"]:
            raise ReleaseValidationError(f"Archive entry type changed: {relative}")
        if len(value) != entry["size_bytes"] or _sha256_bytes(value) != entry["sha256"]:
            raise ReleaseValidationError(f"Archive entry failed integrity validation: {relative}")


def verify_release_archive(archive: Path) -> dict[str, Any]:
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as source:
            names = source.namelist()
            if len(names) != len(set(names)) or source.testzip() is not None:
                raise ReleaseValidationError("Release ZIP contains duplicates or corrupt entries.")
            for name in names:
                _safe_relative(name)
            manifest_names = [name for name in names if PurePosixPath(name).name == MANIFEST_NAME]
            if len(manifest_names) != 1:
                raise ReleaseValidationError("Release ZIP must contain exactly one manifest.")
            root_name = PurePosixPath(manifest_names[0]).parts[0]
            document = _load_manifest_bytes(source.read(manifest_names[0]))
            payloads: dict[str, tuple[str, bytes]] = {}
            for name in names:
                relative = PurePosixPath(name).relative_to(root_name).as_posix()
                if relative == MANIFEST_NAME:
                    continue
                payloads[relative] = ("file", source.read(name))
            _verify_manifest_entries(document, payloads)
            return document

    if archive.name.endswith(".tar.gz"):
        with tarfile.open(archive, "r:gz") as source:
            members = source.getmembers()
            names = [member.name for member in members if member.isfile() or member.issym()]
            if len(names) != len(set(names)):
                raise ReleaseValidationError("Release archive contains duplicate entries.")
            for name in names:
                _safe_relative(name)
            manifest_members = [member for member in members if PurePosixPath(member.name).name == MANIFEST_NAME]
            if len(manifest_members) != 1:
                raise ReleaseValidationError("Release archive must contain exactly one manifest.")
            manifest_stream = source.extractfile(manifest_members[0])
            if manifest_stream is None:
                raise ReleaseValidationError("Release manifest could not be read.")
            document = _load_manifest_bytes(manifest_stream.read())
            root_name = PurePosixPath(manifest_members[0].name).parts[0]
            payloads = {}
            for member in members:
                if not (member.isfile() or member.issym()):
                    continue
                relative = PurePosixPath(member.name).relative_to(root_name).as_posix()
                if relative == MANIFEST_NAME:
                    continue
                if member.issym():
                    _safe_symlink_target(relative, member.linkname)
                    payloads[relative] = ("symlink", member.linkname.encode("utf-8"))
                else:
                    stream = source.extractfile(member)
                    if stream is None:
                        raise ReleaseValidationError(f"Archive entry could not be read: {relative}")
                    payloads[relative] = ("file", stream.read())
            _verify_manifest_entries(document, payloads)
            return document
    raise ReleaseValidationError(f"Unsupported release archive: {archive}")
