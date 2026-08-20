"""Assemble self-contained macOS releases from pinned, prebuilt runtimes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from release.build_release import _package_version, _write_launchers  # noqa: E402
from release.support import (  # noqa: E402
    ReleaseValidationError,
    build_release_archive,
    sha256_file,
    verify_release_archive,
    verify_release_directory,
    write_release_manifest,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PACKAGE = PROJECT_ROOT / "src" / "adt_video_publisher"
PYTHON_VERSION = "3.13.15"
PYTHON_BUILD = "20260814"
IMAGEIO_VERSION = "0.6.0"
TKINTERDND2_VERSION = "0.6.2"
TKINTERDND2_WHEEL = "tkinterdnd2-0.6.2-py3-none-any.whl"
TKINTERDND2_SHA256 = "b6a8b229d26286c022bb2fbd311c2e431e4d9bbab8133be80e9c98e7bcf9fe59"
TKINTERDND2_URL = (
    "https://files.pythonhosted.org/packages/33/1b/"
    "039642c212c24887a941af706b006365f3733d88aab383f0cf151768403c/"
    + TKINTERDND2_WHEEL
)

ASSETS = {
    "arm64": {
        "python_name": "cpython-3.13.15+20260814-aarch64-apple-darwin-install_only.tar.gz",
        "python_sha256": "7d50bb42813a5644db7c40d3ad79361d0b724bb29d25a91fab1048c2c5c6a8c5",
        "ffmpeg_name": "imageio_ffmpeg-0.6.0-py3-none-macosx_11_0_arm64.whl",
        "ffmpeg_sha256": "b1ae3173414b5fc5f538a726c4e48ea97edc0d2cdc11f103afee655c463fa742",
        "ffmpeg_url": "https://files.pythonhosted.org/packages/40/5c/f3d8a657d362cc93b81aab8feda487317da5b5d31c0e1fdfd5e986e55d17/imageio_ffmpeg-0.6.0-py3-none-macosx_11_0_arm64.whl",
    },
    "x86_64": {
        "python_name": "cpython-3.13.15+20260814-x86_64-apple-darwin-install_only.tar.gz",
        "python_sha256": "44bb8a1d97c070deb30880b2b7fe681c1e9cf727cb950709e022dc195cdfdf4f",
        "ffmpeg_name": "imageio_ffmpeg-0.6.0-py3-none-macosx_10_9_intel.macosx_10_9_x86_64.whl",
        "ffmpeg_sha256": "9d2baaf867088508d4a3458e61eeb30e945c4ad8016025545f66c4b5aaef0a61",
        "ffmpeg_url": "https://files.pythonhosted.org/packages/da/58/87ef68ac83f4c7690961bce288fd8e382bc5f1513860fc7f90a9c1c1c6bf/imageio_ffmpeg-0.6.0-py3-none-macosx_10_9_intel.macosx_10_9_x86_64.whl",
    },
}


def _download(url: str, destination: Path, expected_sha256: str) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "High2Min-Video-Compressor-Builder/0.8"})
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)
    actual = sha256_file(destination)
    if actual != expected_sha256:
        raise ReleaseValidationError(
            f"Downloaded asset checksum mismatch for {destination.name}: {actual}"
        )


def _safe_extract_tar(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:gz") as source:
        for member in source.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ReleaseValidationError(f"Unsafe Python runtime archive entry: {member.name}")
        source.extractall(destination, filter="data")


def _copy_source(destination: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {
            name for name in names
            if name == "__pycache__" or name.endswith((".pyc", ".pyo"))
        }

    shutil.copytree(SOURCE_PACKAGE, destination, ignore=ignore)


def _extract_tkinterdnd2(wheel: Path, app_root: Path, license_path: Path) -> None:
    """Vendor the pure-Python wrapper and its native macOS TkDND libraries."""

    with zipfile.ZipFile(wheel) as archive:
        members = [name for name in archive.namelist() if name.startswith("tkinterdnd2/")]
        if not members:
            raise ReleaseValidationError("The tkinterdnd2 wheel has no package files.")
        for name in members:
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ReleaseValidationError(f"Unsafe tkinterdnd2 wheel entry: {name}")
            if name.endswith("/"):
                continue
            destination = app_root.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(name))
        license_member = next(
            (
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/licenses/LICENSE")
            ),
            None,
        )
        if license_member is not None:
            license_path.write_bytes(archive.read(license_member))


def _extract_ffmpeg(wheel: Path, destination: Path, license_destination: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        binaries = [
            name for name in archive.namelist()
            if name.startswith("imageio_ffmpeg/binaries/ffmpeg-") and not name.endswith("/")
        ]
        if len(binaries) != 1:
            raise ReleaseValidationError("The imageio-ffmpeg wheel has an unexpected binary layout.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(archive.read(binaries[0]))
        destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        licenses = [
            name for name in archive.namelist()
            if name.endswith(".dist-info/LICENSE") or name.endswith(".dist-info/LICENSE.txt")
        ]
        if licenses:
            license_destination.write_bytes(archive.read(licenses[0]))
        else:
            license_destination.write_text(
                "imageio-ffmpeg is distributed under the BSD 2-Clause License.\n", encoding="utf-8"
            )


def _macho(path: Path) -> bool:
    resolved = path.resolve()
    magic = resolved.read_bytes()[:4]
    return magic in {
        bytes.fromhex("feedface"),
        bytes.fromhex("cefaedfe"),
        bytes.fromhex("feedfacf"),
        bytes.fromhex("cffaedfe"),
        bytes.fromhex("cafebabe"),
        bytes.fromhex("bebafeca"),
    }


def _write_documents(root: Path, target: str, runtime: Path) -> None:
    licenses = root / "licenses"
    python_license_candidates = [runtime / "LICENSE", runtime / "LICENSE.txt"]
    python_license = next((path for path in python_license_candidates if path.is_file()), None)
    if python_license:
        shutil.copy2(python_license, licenses / "CPython-LICENSE.txt")
    (licenses / "FFmpeg-NOTICE.txt").write_text(
        "FFmpeg is supplied by the pinned imageio-ffmpeg 0.6.0 platform wheel. "
        "Run app/adt_video_publisher/bin/macos-*/ffmpeg -L on macOS for its compiled license text.\n",
        encoding="utf-8",
    )
    (root / "README.txt").write_text(
        "High2Min Video Compressor portable macOS release\n"
        "================================================\n\n"
        f"Target: {target}\n"
        "No Python or FFmpeg installation is required.\n"
        "Open 'High2Min Video Compressor.app' for the UI or run ./high2min --help for automation.\n\n"
        "The recommended ADT defaults use CRF 35, the medium preset, a hard 5 MiB maximum, "
        "adaptive proportional scaling only when needed, and SSIM quality validation.\n\n"
        "This internal release is unsigned. If macOS quarantines it, approve it in Privacy & Security. "
        "Public distribution requires Developer ID signing and notarization.\n",
        encoding="utf-8",
    )
    (root / "THIRD-PARTY-NOTICES.txt").write_text(
        "This release bundles a redistributable CPython runtime from python-build-standalone, "
        "tkinterdnd2/TkDND for native file drag-and-drop, and an FFmpeg executable from "
        "imageio-ffmpeg. License and notice files are in licenses/.\n",
        encoding="utf-8",
    )


def build_architecture(architecture: str, output: Path) -> dict[str, object]:
    metadata = ASSETS[architecture]
    target = f"macos-{architecture}"
    version = _package_version()
    name = f"High2Min-Video-Compressor-{version}-{target}"
    archive = output / f"{name}.tar.gz"
    checksum = Path(str(archive) + ".sha256")
    if archive.exists() or checksum.exists():
        raise ReleaseValidationError(f"Release output already exists: {archive}")
    output.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"high2min-{target}-") as temporary_name:
        temporary = Path(temporary_name)
        python_archive = temporary / str(metadata["python_name"])
        python_url = (
            f"https://github.com/astral-sh/python-build-standalone/releases/download/{PYTHON_BUILD}/"
            f"{metadata['python_name']}"
        )
        wheel = temporary / str(metadata["ffmpeg_name"])
        dnd_wheel = temporary / TKINTERDND2_WHEEL
        _download(python_url, python_archive, str(metadata["python_sha256"]))
        _download(str(metadata["ffmpeg_url"]), wheel, str(metadata["ffmpeg_sha256"]))
        _download(TKINTERDND2_URL, dnd_wheel, TKINTERDND2_SHA256)
        extracted = temporary / "extracted"
        _safe_extract_tar(python_archive, extracted)
        python_tree = extracted / "python"
        if not (python_tree / "bin" / "python3").exists():
            raise ReleaseValidationError("The portable CPython archive has an unexpected layout.")

        root = temporary / name
        runtime = root / "runtime"
        shutil.copytree(python_tree, runtime, symlinks=True)
        app_package = root / "app" / "adt_video_publisher"
        app_package.parent.mkdir(parents=True)
        _copy_source(app_package)
        licenses = root / "licenses"
        licenses.mkdir()
        _extract_tkinterdnd2(
            dnd_wheel,
            root / "app",
            licenses / "tkinterdnd2-LICENSE.txt",
        )
        ffmpeg = app_package / "bin" / target / "ffmpeg"
        _extract_ffmpeg(wheel, ffmpeg, licenses / "imageio-ffmpeg-LICENSE.txt")
        launcher = root / "high2min"
        launcher.write_text(
            "#!/bin/sh\n"
            'APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
            'export PYTHONPATH="$APP_DIR/app"\n'
            'exec "$APP_DIR/runtime/bin/python3" -m adt_video_publisher "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        _write_launchers(root, target)
        _write_documents(root, target, runtime)

        tkinter_modules = list(runtime.rglob("_tkinter*.so"))
        tcl_files = list(runtime.rglob("init.tcl"))
        dnd_architecture = "arm64" if architecture == "arm64" else "x64"
        dnd_libraries = list(
            (root / "app" / "tkinterdnd2" / "tkdnd" / f"osx-{dnd_architecture}").glob("*.dylib")
        )
        if not tkinter_modules or not tcl_files:
            raise ReleaseValidationError("The portable CPython runtime does not contain complete Tk support.")
        if not dnd_libraries or not all(_macho(library) for library in dnd_libraries):
            raise ReleaseValidationError("The portable release has no valid macOS TkDND library.")
        if not _macho(runtime / "bin" / "python3") or not _macho(ffmpeg):
            raise ReleaseValidationError("A pinned macOS runtime executable is not Mach-O.")

        write_release_manifest(
            root,
            product_version=version,
            target=target,
            dependencies={
                "python": f"{PYTHON_VERSION}+{PYTHON_BUILD} python-build-standalone",
                "imageio-ffmpeg": IMAGEIO_VERSION,
                "tkinterdnd2": TKINTERDND2_VERSION,
                "ffmpeg": metadata["ffmpeg_name"],
                "packaging": "portable CPython source runtime",
            },
        )
        verify_release_directory(root)
        build_release_archive(root, archive, target=target)

    verify_release_archive(archive)
    digest = sha256_file(archive)
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    return {
        "ok": True,
        "target": target,
        "archive": str(archive),
        "size_bytes": archive.stat().st_size,
        "sha256": digest,
        "runtime_execution_tested": False,
        "structural_checks": ["pinned SHA-256", "Mach-O", "Tk files", "release manifest", "archive CRC"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(PROJECT_ROOT / "releases"))
    parser.add_argument("--architecture", choices=("arm64", "x86_64", "all"), default="all")
    arguments = parser.parse_args()
    architectures = tuple(ASSETS) if arguments.architecture == "all" else (arguments.architecture,)
    try:
        results = [build_architecture(architecture, Path(arguments.output).resolve()) for architecture in architectures]
    except (OSError, tarfile.TarError, zipfile.BadZipFile, ReleaseValidationError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "releases": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
