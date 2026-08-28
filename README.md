# High2Min Video Compressor

High2Min Video Compressor is a cross-platform application for producing compact, quality-checked video copies. Its ADT workflow completely removes audio from sign-language videos, targets a hard 5 MiB maximum, and can publish the results into Accessible Digital Textbook websites.

## Download

Download the latest Windows, Linux, Apple Silicon Mac, or Intel Mac archive from the [High2Min Releases page](https://github.com/adolfalfred/High2Min-Video-Compressor/releases/latest). A GitHub account is not required.

These certificate-free binaries are built on native GitHub-hosted runners and published with SHA-256 checksums and GitHub provenance attestations. They are not Windows Authenticode-signed or Apple-notarized, so operating-system publisher warnings are expected. Read [Downloading High2Min safely](docs/download-and-verify.md) before opening a release.

Maintainers can follow the [certificate-free release checklist](docs/release-checklist.md) to build and publish a new version.

The 0.11.x publishing design is documented in [Safe ADT publishing compatibility](docs/v0.11.0-implementation-plan.md).

Version 0.11.2 is the current release line and retains the proven compact workflow for ADT websites: H.264 CRF 35 with the `medium` preset, complete audio removal, preserved frame rate and aspect ratio, and a hard 5 MiB maximum. Full resolution is kept whenever it fits; only longer outputs are proportionately downscaled from the untouched original until they fit. Every final output must also pass the default 0.95 SSIM floor at its delivered resolution. The desktop UI accepts a video or folder by native drag-and-drop and shows live encoding, validation, and overall percentages. FFmpeg, FFprobe, and Git inspection remain hidden during Windows desktop jobs, so compression and ADT analysis do not flash console windows.

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

Preview an ADT update without changing any file. Each MP4 filename may contain any words, but its stem must contain exactly one positive number; that number is the ADT spine position. Use `--mapping pages.json` or `--mapping pages.csv` when source/PDF numbering differs from the website spine:

```powershell
python -m adt_video_publisher publish-plan `
  --input "D:\videos-compressed" `
  --book "D:\book-adt" `
  --mode merge `
  --json
```

Update the ADT website itself without creating or modifying a ZIP:

```powershell
python -m adt_video_publisher publish `
  --input "D:\videos-compressed" `
  --book "D:\book-adt" `
  --in-place `
  --mode merge `
  --json --progress ndjson
```

The desktop Publish ADT step always uses this in-place mode and offers a read-only **Analyze ADT changes** preview first. Merge mode preserves existing page videos. Replace mode lists removals and requires explicit confirmation. The preview identifies active pages and offline reader resources omitted from legacy manifests, plus stale declarations for files that no longer exist. Publishing safely repairs those declarations, stages only allowlisted files, verifies baseline hashes immediately before commit, and aborts if another process changes a target. It never rewrites `base.bundle` files, authored CSS, inactive page variants, or quiz pages. Existing approved helpers are preserved; missing media-independence and draggable sign-player adapters are installed in a validated order. A durable journal, same-filesystem file renames, final semantic validation, rollback, and automatic recovery protect the repository. Repository/development files and existing ZIP packages remain byte-identical.

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

Sparse page mappings are supported. Merge mode combines them with existing mappings; replace mode is authoritative only when explicitly selected and confirmed. Publishing enables sign language and read-aloud, advances a compatible cache version, updates only targeted manifest entries while preserving comments/order, refreshes quote-aware offline-preloader data, and validates the production website. The separate-copy CLI mode can also write an exact-manifest ZIP and its `.sha256` sidecar.

Run the real-browser compatibility contract on Chrome, Chromium, or Edge:

```powershell
python -m adt_video_publisher browser-test --json
```

It checks 320, 390, 767, 1024, and 1440 pixel viewports, one-row controls, visible/draggable sign video, explicit close behavior, and independent narration/video playback in both orders.

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
