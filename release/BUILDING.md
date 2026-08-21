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
- creates a terminal-free `High2Min Video Compressor.exe` beside the console-enabled
  `high2min.exe` on Windows, both sharing one runtime;
- creates the macOS `.app` directly with PyInstaller on the matching native architecture and
  validates its nested ad-hoc signatures and Mach-O architecture;
- validates every archive entry against `RELEASE-MANIFEST.json`;
- writes a `.sha256` sidecar.

Verify any completed archive:

```sh
python release/verify_release.py releases/High2Min-Video-Compressor-VERSION-PLATFORM.ARCHIVE
```

macOS builds are ad-hoc signed for internal bundle integrity but remain unsigned from Apple's
publisher-trust perspective. Internal users may need to approve the application in Privacy &
Security. Public distribution should add Developer ID signing and notarization after the verified
native build and then regenerate its archive checksum.

macOS releases must be built on the matching native GitHub runner. Cross-built Python runtime
archives are no longer accepted because they cannot execute, validate the complete `.app`, or
reproduce Gatekeeper behavior before publication.
