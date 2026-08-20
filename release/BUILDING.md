# Building portable releases

Build each release on its target operating system. PyInstaller is deliberately not treated as a cross-compiler.

Requirements:

- Windows 10/11 x86-64 or ARM64, macOS 11+ Intel/Apple Silicon, or a glibc-based Linux x86-64/ARM64 system;
- Python 3.11–3.14 with Tk and `venv`;
- network access to install the two pinned build dependencies.

Windows PowerShell:

```powershell
& .\release\build-current.ps1 -Python python -SmokeVideo "D:\videos\page_1.mp4"
```

Linux or macOS:

```sh
chmod +x release/build-current.sh
./release/build-current.sh --smoke-video /path/to/page_1.mp4
```

Ubuntu WSL without permission to install system packages can build Linux without changing the WSL installation:

```sh
./release/build-linux-from-wsl.sh --smoke-video /mnt/d/path/to/page_1.mp4
```

For a headless Linux builder, provide a virtual display such as `xvfb-run`; use `--skip-ui-smoke` only when no display is available, then require the UI smoke test on a graphical host before distribution.

Every build:

- creates an isolated temporary environment and removes it;
- bundles the target platform's FFmpeg executable from `imageio-ffmpeg`;
- builds an onedir executable with the schemas and Tk runtime;
- smoke-tests version, JSON contract, schemas, a real video when supplied, and the full desktop interface;
- creates a deterministic ZIP on Windows or `.tar.gz` on Linux/macOS;
- validates every archive entry against `RELEASE-MANIFEST.json`;
- writes a `.sha256` sidecar.

Verify any completed archive:

```sh
python release/verify_release.py releases/High2Min-Video-Compressor-VERSION-PLATFORM.ARCHIVE
```

macOS builds are unsigned. Internal users may need to approve the application in Privacy & Security. Public distribution should add Developer ID signing and notarization after the verified build and then regenerate its archive checksum.

When no Mac builder is available, `build_portable_macos.py` can assemble Intel and Apple Silicon source-runtime releases from pinned python-build-standalone and imageio-ffmpeg assets. It verifies upstream SHA-256 values, Mach-O executables, bundled Tk files, the complete release manifest, and archive integrity. Because it cannot execute macOS binaries on another operating system, run `high2min --version`, `high2min verify --input page_1.mp4 --json`, and `high2min ui --smoke-test` on each Mac architecture before approval.
