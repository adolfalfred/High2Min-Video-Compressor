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
high2min schema {job-plan|progress-event|publish-plan|publish-result|result}
high2min inspect --input PATH [--recursive] [--ffmpeg PATH] [--json]
high2min plan --input PATH [--output PATH] [--workers auto|N] [--json]
high2min compress --input PATH [--output PATH] [--workers auto|N] [--json]
high2min verify --input PATH [--recursive] [--ffmpeg PATH] [--json]
high2min resume --job PATH [--workers auto|N] [--json]
high2min publish-plan --input COMPRESSED_DIR --book ADT_DIR [--mapping FILE.json|FILE.csv] [--mode merge|replace] [--json]
high2min publish --input COMPRESSED_DIR --book ADT_DIR (--in-place|--output NEW_ADT_DIR) [--package FILE.zip] [--mapping FILE] [--mode merge|replace] [--confirm-removals] [--language CODE] [--json]
high2min browser-test [--browser CHROMIUM_PATH] [--json]
high2min ui
high2min --version
```

The `--json` and `--progress ndjson` flags may appear before or after the command. Compression writes durable state to `.adt-video-job.json` in the output directory. The `resume` command verifies completed source/output hashes and processes only unfinished or failed items.

## Example for coding agents

```powershell
high2min compress --input "D:\videos" --output "D:\videos-compressed" `
  --workers auto --json --progress ndjson
```

`publish-plan` is read-only and returns exact ADT spine mappings, active runtime/helper capabilities, Git state, mutations, removals, warnings, blockers, and ZIP sentinels. A video stem may contain any words but must contain exactly one positive number unless a JSON/CSV mapping is provided.

`publish` validates every compressed video and defaults to merging mappings. Replace mode requires `--confirm-removals` when existing videos would be deleted. It uses targeted compatibility adapters without modifying runtime bundles or authored CSS, commits allowlisted files transactionally, preserves repository ZIP files, and optionally creates a deterministic ZIP plus SHA-256 sidecar only in separate-output mode.

The UI is optional. `high2min ui` and `high2min-ui` open it; none of the automation commands import or open it.
