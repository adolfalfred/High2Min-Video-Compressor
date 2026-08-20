# High2Min Video Compressor 0.8.0

This directory contains only the latest portable release. All archives have matching SHA-256 sidecars and an internal `RELEASE-MANIFEST.json` that covers every packaged file.

| Platform | Archive | Verification status |
|---|---|---|
| Windows x86-64 | `High2Min-Video-Compressor-0.8.0-windows-x86_64.zip` | Native CLI, bundled FFmpeg, drag-and-drop UI, icon, and UI smoke test passed |
| Linux x86-64 | `High2Min-Video-Compressor-0.8.0-linux-x86_64.tar.gz` | Native CLI, bundled FFmpeg, drag-and-drop UI, and UI smoke test passed through WSLg |
| macOS Apple Silicon | `High2Min-Video-Compressor-0.8.0-macos-arm64.tar.gz` | Pinned downloads, Mach-O files, Tk/TkDND, app bundle/icon, manifest, and archive verified; native signing and execution remain required |
| macOS Intel | `High2Min-Video-Compressor-0.8.0-macos-x86_64.tar.gz` | Pinned downloads, Mach-O files, Tk/TkDND, app bundle/icon, manifest, and archive verified; native signing and execution remain required |

Windows users extract the ZIP and double-click `High2Min Video Compressor.vbs`, or run `high2min.exe`. Linux users extract the TAR and run `./high2min-ui` or `./high2min`. macOS users extract the TAR and open `High2Min Video Compressor.app` or run `./high2min`.

Before public distribution, build these packages on their native GitHub runners, sign Windows and macOS executables, notarize macOS packages, verify all checksums, and publish them from a draft immutable GitHub Release.
