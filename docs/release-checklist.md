# Release checklist: certificate-free GitHub binaries

## One-time repository setup

- [ ] Open **Settings → Actions → General** and allow GitHub Actions.
- [ ] Under **Workflow permissions**, allow read and write access so the tagged workflow can create a draft release.
- [ ] Enable private vulnerability reporting under **Settings → Security → Code security** if it is available.
- [ ] Optionally enable immutable releases before the first stable publication.

## Before creating a tag

- [ ] Commit and push `.github/workflows/ci.yml`, `.github/workflows/release.yml`, the release helpers, and documentation.
- [ ] Confirm all four test jobs and all four native package jobs in the **CI** workflow are green.
- [ ] Confirm `pyproject.toml` contains the intended version.
- [ ] Confirm there are no book videos, PDFs, credentials, or local release binaries in the commit.
- [ ] Review the release notes and known limitations, especially the unsigned Windows/macOS warning.

## Build a draft release

```sh
git tag -a v0.11.3 -m "High2Min Video Compressor 0.11.3"
git push origin v0.11.3
```

- [ ] Open **Actions → Build release** and wait for all native build jobs and the draft-release job to succeed.
- [ ] Open **Releases** and select the new draft.
- [ ] Confirm it contains four archives, four `.sha256` sidecars, `SHA256SUMS.txt`, and a release index.
- [ ] Download at least the Windows archive and verify its checksum and provenance.
- [ ] Confirm the Windows application opens and performs a small compression test.
- [ ] Confirm `High2Min Video Compressor.exe` opens without a terminal and `high2min.exe --version` prints `0.11.3` in PowerShell.
- [ ] Confirm the automatic startup check and manual update action report that published `0.11.3` is current.
- [ ] Confirm compression shows no FFmpeg/FFprobe console popups on Windows.
- [ ] Confirm desktop Publish ADT updates the chosen repo itself and leaves existing ZIP files unchanged.
- [ ] Confirm **Analyze ADT changes** performs no writes and displays source names, ADT spine pages, destination names, mutations, and removals.
- [ ] Confirm Analyze ADT changes and Update ADT website do not flash Git, FFmpeg, or FFprobe terminal windows on Windows.
- [ ] Confirm the preview reports active/offline resources omitted from the manifest and stale declarations, then publishing repairs both safely.
- [ ] Confirm merge preserves existing page videos and replace refuses removals until they are explicitly confirmed.
- [ ] Confirm flexible names with one numeric group and JSON/CSV mapping overrides publish to the intended ADT spine pages.
- [ ] Confirm active/inactive `base.bundle` files, existing helpers, authored CSS/content/audio, and unrelated dirty Git files are byte-identical after publishing.
- [ ] Run `high2min browser-test --json` and confirm all five responsive viewport contracts pass.
- [ ] Confirm Publish ADT shows named progress phases through final validation and cleanup rather than stopping near 98%.
- [ ] Confirm a cancelled pre-commit publication leaves the repository unchanged and a simulated interrupted transaction recovers on retry.
- [ ] Confirm the published ADT shows the hand-sign control after a fresh browser reload.
- [ ] Confirm voice-over can play while the sign-language video's playback time continues advancing.
- [ ] Confirm an ADT with `assets/offline-preloader.js` receives refreshed embedded config, video mappings, HTML, and versioned script URLs.
- [ ] Confirm publishing succeeds when a valid `base.bundle*.js` exists but the legacy manifest omits it.
- [ ] Confirm insufficient disk errors use MB and zero-output failed jobs leave no state/report files.
- [ ] Confirm both macOS archives contain the native `.app`, pass strict nested code-integrity validation, and match their labelled processor architecture.
- [ ] If possible, ask trusted users with Apple Silicon, Intel Mac, and Linux machines to test the draft archives before publication.

## Publish

- [ ] Keep the unsigned-binary warning in the release notes.
- [ ] Mark the release as the latest stable release.
- [ ] Publish the draft.
- [ ] Open the public `/releases/latest` link in a signed-out browser and confirm downloads work without an account.

## Stop or rollback when

- Any CI or release job is red.
- One of the four platform archives is missing.
- A SHA-256 value or provenance attestation fails.
- The archive contains audio, exceeds the intended video limit, or the UI does not open.
- The release page does not clearly state that Windows and macOS builds are unsigned.

If a problem is found, delete or retain the faulty draft for diagnosis, correct the source, increment the project version, and create a new patch tag such as `v0.11.3`. Do not reuse a release tag or silently replace a published asset.
