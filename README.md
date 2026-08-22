# High2Min Video Compressor

High2Min Video Compressor is a cross-platform application for producing compact, quality-checked video copies. Its ADT workflow completely removes audio from sign-language videos, targets a hard 5 MiB maximum, and can publish the results into Accessible Digital Textbook websites.

## Download

Download the latest Windows, Linux, Apple Silicon Mac, or Intel Mac archive from the [High2Min Releases page](https://github.com/adolfalfred/High2Min-Video-Compressor/releases/latest). A GitHub account is not required.

These certificate-free binaries are built on native GitHub-hosted runners and published with SHA-256 checksums and GitHub provenance attestations. They are not Windows Authenticode-signed or Apple-notarized, so operating-system publisher warnings are expected. Read [Downloading High2Min safely](docs/download-and-verify.md) before opening a release.

Maintainers can follow the [certificate-free release checklist](docs/release-checklist.md) to build and publish a new version.

Version 0.9.0 uses the proven compact workflow for ADT websites: H.264 CRF 35 with the `medium` preset, complete audio removal, preserved frame rate and aspect ratio, and a hard 5 MiB maximum. Full resolution is kept whenever it fits; only longer outputs are proportionately downscaled from the untouched original until they fit. Every final output must also pass the default 0.95 SSIM floor at its delivered resolution. The desktop UI accepts a video or folder by native drag-and-drop and shows live encoding, validation, and overall percentages. FFmpeg and FFprobe remain hidden during Windows desktop jobs, so compression does not flash console windows.

The desktop app checks the latest public GitHub release in the background at startup, at most once every 24 hours after a successful check. When a newer stable version exists, it asks before opening the verified GitHub release page; it never silently replaces its own files. Offline automatic checks stay silent, and **Check for updates** provides an immediate manual check.

Windows releases provide a terminal-free `High2Min Video Compressor.exe` for desktop users and a separate console-enabled `high2min.exe` for Codex, Claude Code, scripts, and CI. Native macOS builds provide a real `.app` bundle with an architecture-matched embedded runtime; no separate Python installation is needed.

The project is CLI-first: Codex, Claude Code, scripts, and CI systems can operate it without opening the desktop UI. The UI will call the same core engine.

## Complete implementation

The verified application now includes the CLI contract, deterministic exit codes, versioned JSON schemas, media probing, safe path planning, silent FFmpeg compression, SSIM validation, adaptive scaling, live percentage events, CPU/RAM/disk detection, adaptive concurrent batches, durable resume state, JSON/CSV reports, transactional in-place ADT website publishing, optional deterministic SCORM ZIP creation, and an accessible desktop interface with native file/folder drag-and-drop.

Run the current contract commands without installation:

```powershell
$env:PYTHONPATH = "src"
python -m adt_video_publisher contract --json
python -m adt_video_publisher schema job-plan
```

Plan and run compression without opening a UI:

```powershell
$env:PYTHONPATH = "src"
python -m adt_video_publisher plan --input "D:\videos" --output "D:\videos-compressed" --json
python -m adt_video_publisher compress --input "D:\videos" --output "D:\videos-compressed" --workers auto --maximum-bytes 5242880 --crf 35 --preset medium --minimum-ssim 0.95 --json --progress ndjson
python -m adt_video_publisher resume --job "D:\videos-compressed\.adt-video-job.json" --json --progress ndjson
python -m adt_video_publisher verify --input "D:\videos-compressed" --json
```

Update an ADT website repo itself with compressed `page_N.mp4` videos without creating or modifying a ZIP:

```powershell
python -m adt_video_publisher publish `
  --input "D:\videos-compressed" `
  --book "D:\book-adt" `
  --in-place `
  --json --progress ndjson
```

The desktop Publish ADT step always uses this in-place mode. It stages and validates only files that can change instead of copying the complete website. Before work begins, it verifies create/rename/delete permissions and calculates temporary storage from the real transaction. It keeps the hand-sign video control enabled, refreshes generated offline-preloader data and cache versions, and makes sign-language video independent from voice-over narration so both can play together. A durable journal and same-filesystem renames provide rollback and automatic recovery after interruption. Real phase progress, safe pre-commit cancellation, stall detection, and a diagnostic log keep the final publishing stages observable. Repository/development files and existing ZIP packages remain untouched.

Insufficient-storage errors report required and available capacity in MB. If every compression item fails before any output is produced, High2Min removes the temporary job state and report files so the failed job does not block a clean retry.

The CLI can alternatively publish into a new ADT website copy and create a deployment package:

```powershell
python -m adt_video_publisher publish `
  --input "D:\videos-compressed" `
  --book "D:\book-adt" `
  --output "D:\book-adt-published" `
  --package "D:\deployment\book-adt-v2.zip" `
  --json --progress ndjson
```

The compressed folder is authoritative for `videos.json`; sparse page mappings are supported. Both modes enable sign language, increment `bundleVersion`, rebuild `imsmanifest.xml`, and validate the production website. The separate-copy CLI mode can also write an exact-manifest ZIP and its `.sha256` sidecar.

With `--json`, the final result is written to stdout. With `--progress ndjson`, structured progress events are written independently to stderr.

Open the optional desktop interface:

```powershell
python -m adt_video_publisher ui
```

After installation, the same automation interface is available as `high2min`, and the standalone UI launcher is `high2min-ui`.

The desktop interface and CLI use the same processing and publishing engines. The recommended desktop profile enforces ≤5 MiB and uses adaptive scaling only when required. The optional high-quality profile keeps dimensions unchanged and permits larger files. Closing during a compression job requests a safe stop and waits for current encodes to finish.

Hard-size and adaptive-scale behavior are the CLI defaults. Use `--soft-size --no-adaptive-scale --crf 21` for the optional quality-first workflow. Re-encode from untouched originals after changing a quality setting; never use an already compressed copy as input.

## Portable releases

Self-contained release archives are in `releases/` for Windows x86-64, Linux x86-64, macOS Apple Silicon, and macOS Intel. They include Python/runtime components where needed, the correct FFmpeg executable, schemas, native desktop launchers, the automation CLI, license notices, a per-file release manifest, and an archive SHA-256 sidecar. See `releases/README.md` for verification status and launch instructions.

Reproducible native builders and the independent archive verifier are documented in `release/BUILDING.md`.

The repository intentionally contains no nested Git repository.
