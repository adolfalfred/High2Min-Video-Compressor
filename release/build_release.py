"""Build one native, self-contained High2Min Video Compressor release."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from release.support import (  # noqa: E402
    ReleaseValidationError,
    build_release_archive,
    platform_tag,
    sha256_file,
    verify_release_archive,
    verify_release_directory,
    write_release_manifest,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
ENTRYPOINT = Path(__file__).resolve().parent / "entrypoint.py"
ASSET_ROOT = SOURCE_ROOT / "adt_video_publisher" / "assets"


def _run(command: list[str], *, timeout: int = 600, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=check,
    )


def _package_version() -> str:
    namespace: dict[str, str] = {}
    exec((SOURCE_ROOT / "adt_video_publisher" / "__init__.py").read_text(encoding="utf-8"), namespace)
    return namespace["__version__"]


def _resolve_ffmpeg(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
    else:
        try:
            import imageio_ffmpeg
        except ImportError as exc:
            raise ReleaseValidationError(
                "imageio-ffmpeg is unavailable; install release/requirements-build.txt."
            ) from exc
        path = Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()
    if not path.is_file():
        raise ReleaseValidationError(f"FFmpeg executable was not found: {path}")
    result = _run([str(path), "-version"], timeout=30, check=False)
    if result.returncode != 0 or "ffmpeg version" not in result.stdout.lower():
        raise ReleaseValidationError(f"FFmpeg executable is not usable: {path}")
    return path


def _copy_distribution_license(distribution_name: str, destination: Path) -> None:
    distribution = importlib.metadata.distribution(distribution_name)
    candidates = [
        item for item in (distribution.files or ())
        if Path(str(item)).name.lower() in {"license", "license.txt", "copying.txt", "copying"}
    ]
    if not candidates:
        destination.write_text(
            f"See the installed {distribution_name} {distribution.version} distribution for license terms.\n",
            encoding="utf-8",
        )
        return
    source = Path(distribution.locate_file(candidates[0]))
    shutil.copy2(source, destination)


def _write_launchers(root: Path, target: str) -> None:
    if target.startswith("windows-"):
        launcher = root / "High2Min Video Compressor.vbs"
        launcher.write_text(
            'Set fso = CreateObject("Scripting.FileSystemObject")\r\n'
            'Set shell = CreateObject("WScript.Shell")\r\n'
            'folder = fso.GetParentFolderName(WScript.ScriptFullName)\r\n'
            'command = Chr(34) & folder & "\\high2min.exe" & Chr(34) & " ui"\r\n'
            'shell.Run command, 1, False\r\n',
            encoding="ascii",
        )
        return

    launcher = root / "high2min-ui"
    launcher.write_text(
        "#!/bin/sh\n"
        'APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        'exec "$APP_DIR/high2min" ui "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if target.startswith("macos-"):
        app = root / "High2Min Video Compressor.app" / "Contents"
        executable = app / "MacOS" / "launcher"
        executable.parent.mkdir(parents=True)
        executable.write_text(
            "#!/bin/sh\n"
            'APP_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)\n'
            'exec "$APP_ROOT/high2min" ui\n',
            encoding="utf-8",
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        (app / "Info.plist").write_text(
            """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>High2Min Video Compressor</string>
<key>CFBundleDisplayName</key><string>High2Min Video Compressor</string>
<key>CFBundleIdentifier</key><string>tz.go.tie.high2min-video-compressor</string>
<key>CFBundleVersion</key><string>1</string>
<key>CFBundleExecutable</key><string>launcher</string>
<key>CFBundleIconFile</key><string>high2min-video-compressor.icns</string>
<key>LSMinimumSystemVersion</key><string>11.0</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
""",
            encoding="utf-8",
        )
        resources = app / "Resources"
        resources.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ASSET_ROOT / "high2min-video-compressor.icns", resources)


def _write_release_documents(root: Path, *, target: str, ffmpeg: Path) -> dict[str, str]:
    licenses = root / "licenses"
    licenses.mkdir()
    _copy_distribution_license("imageio-ffmpeg", licenses / "imageio-ffmpeg-LICENSE.txt")
    _copy_distribution_license("pyinstaller", licenses / "PyInstaller-LICENSE.txt")
    _copy_distribution_license("tkinterdnd2", licenses / "tkinterdnd2-LICENSE.txt")
    ffmpeg_license = _run([str(ffmpeg), "-L"], timeout=30, check=False)
    (licenses / "FFmpeg-LICENSE.txt").write_text(
        (ffmpeg_license.stdout or ffmpeg_license.stderr).strip() + "\n", encoding="utf-8"
    )
    ffmpeg_version = _run([str(ffmpeg), "-version"], timeout=30).stdout.splitlines()[0]
    dependencies = {
        "python": platform_python_version(),
        "pyinstaller": importlib.metadata.version("pyinstaller"),
        "imageio-ffmpeg": importlib.metadata.version("imageio-ffmpeg"),
        "tkinterdnd2": importlib.metadata.version("tkinterdnd2"),
        "ffmpeg": ffmpeg_version,
    }
    ui_instruction = (
        "Double-click 'High2Min Video Compressor.vbs'."
        if target.startswith("windows-")
        else "Run ./high2min-ui (or open the .app bundle on macOS)."
    )
    (root / "README.txt").write_text(
        "High2Min Video Compressor portable release\n"
        "==========================================\n\n"
        f"Target: {target}\n\n"
        f"Desktop UI: {ui_instruction}\n"
        "CLI: run high2min --help (high2min.exe on Windows).\n\n"
        "Recommended ADT defaults: CRF 35, medium preset, a hard 5 MiB maximum, adaptive "
        "proportional scaling only when needed, and SSIM quality validation.\n\n"
        "No Python or system FFmpeg installation is required. The application creates copies and "
        "refuses to overwrite source videos or source ADT websites.\n",
        encoding="utf-8",
    )
    (root / "THIRD-PARTY-NOTICES.txt").write_text(
        "This release bundles CPython runtime components, tkinterdnd2/TkDND for native file "
        "drag-and-drop, and an FFmpeg executable supplied by imageio-ffmpeg. License texts are in "
        "the licenses directory. PyInstaller was used as the freezing tool; its license is included "
        "for build provenance.\n",
        encoding="utf-8",
    )
    return dependencies


def platform_python_version() -> str:
    return ".".join(str(part) for part in sys.version_info[:3])


def _build_with_pyinstaller(ffmpeg: Path, target: str, temporary: Path) -> Path:
    dist = temporary / "dist"
    work = temporary / "work"
    spec = temporary / "spec"
    destination = f"adt_video_publisher/bin/{target}"
    packaged_ffmpeg = temporary / ("ffmpeg.exe" if target.startswith("windows-") else "ffmpeg")
    shutil.copy2(ffmpeg, packaged_ffmpeg)
    if not target.startswith("windows-"):
        packaged_ffmpeg.chmod(
            packaged_ffmpeg.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--console",
        "--name",
        "high2min",
        "--distpath",
        str(dist),
        "--workpath",
        str(work),
        "--specpath",
        str(spec),
        "--icon",
        str(
            ASSET_ROOT / (
                "high2min-video-compressor.ico"
                if target.startswith("windows-")
                else "high2min-video-compressor.icns"
                if target.startswith("macos-")
                else "high2min-video-compressor.png"
            )
        ),
        "--paths",
        str(SOURCE_ROOT),
        "--collect-data",
        "adt_video_publisher",
        "--collect-all",
        "tkinterdnd2",
        "--add-data",
        f"{SOURCE_ROOT / 'adt_video_publisher' / 'schemas'}{os.pathsep}adt_video_publisher/schemas",
        "--hidden-import",
        "tkinter",
        "--hidden-import",
        "tkinter.ttk",
        "--hidden-import",
        "tkinterdnd2",
        "--add-binary",
        f"{packaged_ffmpeg}{os.pathsep}{destination}",
        str(ENTRYPOINT),
    ]
    result = _run(command, timeout=1200, check=False)
    if result.returncode != 0:
        raise ReleaseValidationError(
            "PyInstaller failed:\n" + (result.stderr or result.stdout)[-6000:]
        )
    built = dist / "high2min"
    if not built.is_dir():
        raise ReleaseValidationError("PyInstaller did not create the expected onedir release.")
    return built


def _smoke_release(root: Path, target: str, smoke_video: str | None, skip_ui: bool) -> None:
    executable = root / ("high2min.exe" if target.startswith("windows-") else "high2min")
    checks = [
        [str(executable), "--version"],
        [str(executable), "contract", "--json"],
        [str(executable), "schema", "publish-result"],
    ]
    if smoke_video:
        checks.append([str(executable), "verify", "--input", str(Path(smoke_video).resolve()), "--json"])
    if not skip_ui:
        checks.append([str(executable), "ui", "--smoke-test"])
    for command in checks:
        result = _run(command, timeout=180, check=False)
        if result.returncode != 0:
            raise ReleaseValidationError(
                f"Release smoke test failed ({' '.join(command[1:])}):\n{result.stdout}\n{result.stderr}"
            )
    contract = json.loads(_run([str(executable), "contract", "--json"]).stdout)
    if contract["tool"]["version"] != _package_version():
        raise ReleaseValidationError("Frozen executable reports the wrong application version.")


def build_release(arguments: argparse.Namespace) -> dict[str, object]:
    target = platform_tag()
    version = _package_version()
    output = Path(arguments.output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    release_name = f"High2Min-Video-Compressor-{version}-{target}"
    extension = ".zip" if target.startswith("windows-") else ".tar.gz"
    archive = output / f"{release_name}{extension}"
    checksum = Path(str(archive) + ".sha256")
    if archive.exists() or checksum.exists():
        raise ReleaseValidationError(f"Release output already exists: {archive}")
    ffmpeg = _resolve_ffmpeg(arguments.ffmpeg)

    with tempfile.TemporaryDirectory(prefix="high2min-release-") as temporary_name:
        temporary = Path(temporary_name)
        built = _build_with_pyinstaller(ffmpeg, target, temporary)
        root = temporary / release_name
        built.replace(root)
        _write_launchers(root, target)
        dependencies = _write_release_documents(root, target=target, ffmpeg=ffmpeg)
        write_release_manifest(
            root,
            product_version=version,
            target=target,
            dependencies=dependencies,
        )
        verify_release_directory(root)
        _smoke_release(root, target, arguments.smoke_video, arguments.skip_ui_smoke)
        build_release_archive(root, archive, target=target)

    verify_release_archive(archive)
    digest = sha256_file(archive)
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    return {
        "ok": True,
        "version": version,
        "target": target,
        "archive": str(archive),
        "size_bytes": archive.stat().st_size,
        "sha256": digest,
        "checksum": str(checksum),
        "smoke_video": str(Path(arguments.smoke_video).resolve()) if arguments.smoke_video else None,
        "ui_smoke_tested": not arguments.skip_ui_smoke,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(PROJECT_ROOT / "releases"))
    parser.add_argument("--ffmpeg", help="Explicit FFmpeg binary; defaults to imageio-ffmpeg.")
    parser.add_argument("--smoke-video", help="Real compressed video to validate with the frozen tool.")
    parser.add_argument("--skip-ui-smoke", action="store_true", help="Skip Tk display smoke test (headless builder only).")
    arguments = parser.parse_args()
    try:
        result = build_release(arguments)
    except (OSError, subprocess.SubprocessError, ReleaseValidationError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
