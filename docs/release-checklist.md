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
git tag -a v0.8.3 -m "High2Min Video Compressor 0.8.3"
git push origin v0.8.3
```

- [ ] Open **Actions → Build release** and wait for all native build jobs and the draft-release job to succeed.
- [ ] Open **Releases** and select the new draft.
- [ ] Confirm it contains four archives, four `.sha256` sidecars, `SHA256SUMS.txt`, and a release index.
- [ ] Download at least the Windows archive and verify its checksum and provenance.
- [ ] Confirm the Windows application opens and performs a small compression test.
- [ ] Confirm `High2Min Video Compressor.exe` opens without a terminal and `high2min.exe --version` prints `0.8.3` in PowerShell.
- [ ] Confirm compression shows no FFmpeg/FFprobe console popups on Windows.
- [ ] Confirm desktop Publish ADT updates the chosen repo itself and leaves existing ZIP files unchanged.
- [ ] Confirm the published ADT shows the hand-sign control after a fresh browser reload.
- [ ] Confirm voice-over can play while the sign-language video's playback time continues advancing.
- [ ] Confirm an ADT with `assets/offline-preloader.js` receives refreshed embedded config, video mappings, HTML, and versioned script URLs.
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

If a problem is found, delete or retain the faulty draft for diagnosis, correct the source, increment the project version, and create a new patch tag such as `v0.8.3`. Do not reuse a release tag or silently replace a published asset.
