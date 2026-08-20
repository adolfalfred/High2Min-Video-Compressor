# High2Min Video Compressor

High2Min Video Compressor is a cross-platform application for producing compact, quality-checked video copies. Its ADT workflow completely removes audio from sign-language videos, targets a hard 5 MiB maximum, and can publish the results into Accessible Digital Textbook websites.

## Download

Download the latest Windows, Linux, Apple Silicon Mac, or Intel Mac archive from the [High2Min Releases page](https://github.com/adolfalfred/High2Min-Video-Compressor/releases/latest). A GitHub account is not required.

These certificate-free binaries are built on native GitHub-hosted runners and published with SHA-256 checksums and GitHub provenance attestations. They are not Windows Authenticode-signed or Apple-notarized, so operating-system publisher warnings are expected. Read [Downloading High2Min safely](docs/download-and-verify.md) before opening a release.

Maintainers can follow the [certificate-free release checklist](docs/release-checklist.md) to build and publish a new version.

Version 0.8 uses the proven compact workflow for ADT websites: H.264 CRF 35 with the `medium` preset, complete audio removal, preserved frame rate and aspect ratio, and a hard 5 MiB maximum. Full resolution is kept whenever it fits; only longer outputs are proportionately downscaled from the untouched original until they fit. Every final output must also pass the default 0.95 SSIM floor at its delivered resolution. The desktop UI accepts a video or folder by native drag-and-drop and shows live encoding, validation, and overall percentages.

The project is CLI-first: Codex, Claude Code, scripts, and CI systems can operate it without opening the desktop UI. The UI will call the same core engine.

## Complete implementation

The verified application now includes the CLI contract, deterministic exit codes, versioned JSON schemas, media probing, safe path planning, silent FFmpeg compression, SSIM validation, adaptive scaling, live percentage events, CPU/RAM/disk detection, adaptive concurrent batches, durable resume state, JSON/CSV reports, transactional ADT website publishing, deterministic SCORM ZIP creation, and an accessible desktop interface with native file/folder drag-and-drop.

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

Publish compressed `page_N.mp4` videos into a new ADT website copy and create a deployment package:

```powershell
python -m adt_video_publisher publish `
  --input "D:\videos-compressed" `
  --book "D:\book-adt" `
  --output "D:\book-adt-published" `
  --package "D:\deployment\book-adt-v2.zip" `
  --json --progress ndjson
```

Publishing never modifies the source ADT website. The compressed folder is authoritative for `videos.json`; sparse page mappings are supported. The publisher enables sign language, increments `bundleVersion`, rebuilds `imsmanifest.xml`, excludes undeclared development files, validates the new website, writes an exact-manifest ZIP, and creates its `.sha256` sidecar.

With `--json`, the final result is written to stdout. With `--progress ndjson`, structured progress events are written independently to stderr.

Open the optional desktop interface:

```powershell
python -m adt_video_publisher ui
```

After installation, the same automation interface is available as `high2min`, and the standalone UI launcher is `high2min-ui`.

The desktop interface and CLI use the same processing and publishing engines. The recommended desktop profile enforces ≤5 MiB and uses adaptive scaling only when required. The optional high-quality profile keeps dimensions unchanged and permits larger files. Closing during a compression job requests a safe stop and waits for current encodes to finish.

Hard-size and adaptive-scale behavior are the CLI defaults. Use `--soft-size --no-adaptive-scale --crf 21` for the optional quality-first workflow. Re-encode from untouched originals after changing a quality setting; never use an already compressed copy as input.

## Portable releases

Self-contained release archives are in `releases/` for Windows x86-64, Linux x86-64, macOS Apple Silicon, and macOS Intel. They include Python/runtime components where needed, the correct FFmpeg executable, schemas, UI launchers, license notices, a per-file release manifest, and an archive SHA-256 sidecar. See `releases/README.md` for verification status and launch instructions.

Reproducible native builders and the independent archive verifier are documented in `release/BUILDING.md`.

The repository intentionally contains no nested Git repository.
