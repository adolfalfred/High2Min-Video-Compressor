# CLI contract

## Design rules

- The CLI never opens the desktop UI.
- Commands are non-interactive unless an explicit interactive option is introduced later.
- `--json` may appear before or after the command.
- JSON output is written to standard output; diagnostics are written to standard error.
- `--progress ndjson` writes one progress-event JSON object per line to standard error.
- Progress and final JSON streams are independent, so agents can parse both safely.
- Every JSON document includes `schema_version`.
- Exit codes are stable and are part of the public automation contract.
- Original videos must never be modified or replaced.
- Output videos must be written to a separate destination.

## Available commands

```text
high2min contract [--json]
high2min schema {job-plan|progress-event|publish-result|result}
high2min inspect --input PATH [--recursive] [--ffmpeg PATH] [--json]
high2min plan --input PATH [--output PATH] [--workers auto|N] [--json]
high2min compress --input PATH [--output PATH] [--workers auto|N] [--json]
high2min verify --input PATH [--recursive] [--ffmpeg PATH] [--json]
high2min resume --job PATH [--workers auto|N] [--json]
high2min publish --input COMPRESSED_DIR --book ADT_DIR --output NEW_ADT_DIR [--package FILE.zip] [--language CODE] [--json]
high2min ui
high2min --version
```

The `--json` and `--progress ndjson` flags may appear before or after the command. Compression writes durable state to `.adt-video-job.json` in the output directory. The `resume` command verifies completed source/output hashes and processes only unfinished or failed items.

## Example for coding agents

```powershell
high2min compress --input "D:\videos" --output "D:\videos-compressed" `
  --workers auto --json --progress ndjson
```

The `publish` command validates every compressed video, creates a new website copy, replaces the selected language's video mapping authoritatively, increments the bundle version, reconciles the SCORM manifest, and optionally creates a deterministic ZIP plus SHA-256 sidecar. Existing source and output paths are never replaced.

The UI is optional. `high2min ui` and `high2min-ui` open it; none of the automation commands import or open it.
