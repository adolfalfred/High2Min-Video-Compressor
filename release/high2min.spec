# -*- mode: python ; coding: utf-8 -*-
"""Native two-entry-point PyInstaller specification for High2Min releases."""

from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files


project_root = Path(SPEC).resolve().parents[1]
source_root = project_root / "src"
release_root = project_root / "release"
asset_root = source_root / "adt_video_publisher" / "assets"
target = os.environ["HIGH2MIN_BUILD_TARGET"]
ffmpeg = Path(os.environ["HIGH2MIN_BUILD_FFMPEG"]).resolve()
is_windows = target.startswith("windows-")
is_macos = target.startswith("macos-")
target_arch = "arm64" if target.endswith("arm64") else "x86_64"

tk_datas, tk_binaries, tk_hiddenimports = collect_all("tkinterdnd2")
package_datas = collect_data_files("adt_video_publisher")
datas = package_datas + tk_datas + [
    (str(source_root / "adt_video_publisher" / "schemas"), "adt_video_publisher/schemas"),
    (str(source_root / "adt_video_publisher" / "assets"), "adt_video_publisher/assets"),
]
binaries = tk_binaries + [
    (str(ffmpeg), f"adt_video_publisher/bin/{target}"),
]
hiddenimports = sorted(
    set(tk_hiddenimports) | {"tkinter", "tkinter.ttk", "tkinterdnd2"}
)


def analysis(entrypoint: str):
    return Analysis(
        [str(release_root / entrypoint)],
        pathex=[str(source_root)],
        binaries=binaries,
        datas=datas,
        hiddenimports=hiddenimports,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=[],
        noarchive=False,
        optimize=0,
    )


gui_analysis = analysis("gui_entrypoint.py")
cli_analysis = analysis("entrypoint.py")

gui_pyz = PYZ(gui_analysis.pure)
cli_pyz = PYZ(cli_analysis.pure)

gui_executable = EXE(
    gui_pyz,
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    name="High2Min Video Compressor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(
        asset_root
        / (
            "high2min-video-compressor.ico"
            if is_windows
            else "high2min-video-compressor.icns"
            if is_macos
            else "high2min-video-compressor.png"
        )
    ),
    target_arch=target_arch if is_macos else None,
    codesign_identity=None,
    entitlements_file=None,
)

cli_executable = EXE(
    cli_pyz,
    cli_analysis.scripts,
    [],
    exclude_binaries=True,
    name="high2min",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=str(
        asset_root
        / (
            "high2min-video-compressor.ico"
            if is_windows
            else "high2min-video-compressor.icns"
            if is_macos
            else "high2min-video-compressor.png"
        )
    ),
    target_arch=target_arch if is_macos else None,
    codesign_identity=None,
    entitlements_file=None,
)

collection = COLLECT(
    gui_executable,
    cli_executable,
    gui_analysis.binaries,
    gui_analysis.datas,
    cli_analysis.binaries,
    cli_analysis.datas,
    strip=False,
    upx=False,
    name="High2Min Video Compressor",
)

if is_macos:
    application = BUNDLE(
        collection,
        name="High2Min Video Compressor.app",
        icon=str(asset_root / "high2min-video-compressor.icns"),
        bundle_identifier="tz.go.tie.high2min-video-compressor",
        version=os.environ["HIGH2MIN_BUILD_VERSION"],
        info_plist={
            "CFBundleDisplayName": "High2Min Video Compressor",
            "CFBundleName": "High2Min Video Compressor",
            "CFBundlePackageType": "APPL",
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
        },
        target_arch=target_arch,
        codesign_identity=None,
        entitlements_file=None,
    )
