# Changelog

## 0.11.0 - 2026-08-27

- Add a read-only ADT compatibility and exact-change preview to the CLI and desktop UI.
- Accept flexible video filenames with one positive numeric group and optional JSON/CSV page maps.
- Default publishing to non-destructive merge mode and require confirmation for replace-mode removals.
- Stop rewriting minified ADT runtime bundles; preserve active/inactive bundles and authored CSS byte-for-byte.
- Install targeted media-independence and touch/pointer-draggable sign-video compatibility adapters only when missing.
- Commit individual allowlisted files with compare-before-swap conflict detection and ZIP sentinels.
- Preserve manifest comments, unrelated attributes, ordering, BOM/newline style, and nonstandard cache versions.
- Parse offline preloader maps with a quote-aware brace scanner and preserve unrelated query parameters.
- Add semantic staging/final validation plus a real Chromium mobile/desktop accessibility contract.
- Add ten compact fixtures representing the ADT repository profiles used during responsiveness work.

## 0.10.0 - 2026-08-22

- Reserve the next development version for flexible number-based video ordering and verified in-app platform downloads.
- Document the safety, platform-selection, progress, cancellation, integrity-verification, and regression-test requirements before implementation.
- Avoid forced physical disk synchronization after every staged video, preventing long Linux stalls on FUSE, NTFS, exFAT, SMB, and NFS filesystems while retaining durable transaction-journal synchronization.

## 0.9.0 - 2026-08-22

- Replace full-site in-place ADT staging with a minimal overlay containing only videos and generated files.
- Add writable create/rename/delete preflight checks and storage estimates based on the actual staged transaction.
- Commit repository changes with same-filesystem renames and a durable recovery journal instead of copying the old video directory.
- Restore interrupted transactions automatically and retain rollback protection through final ADT validation.
- Report real preflight, validation, staging, runtime, offline-cache, commit, final-validation, and cleanup progress.
- Copy and hash videos in one chunked pass with safe cancellation before the repository transaction begins.
- Detect stalled publishing in the desktop UI and save durable JSON-lines publishing diagnostics.
- Avoid copying macOS/Linux extended attributes and ACL metadata into temporary generated files.
- Add large-manifest, permission, cancellation, recovery, diagnostics, and monotonic-progress regression tests.

## 0.8.5 - 2026-08-21

- Check the latest public GitHub release in a background thread when the desktop app starts.
- Prompt users to open the verified release page when a newer stable version is available.
- Cache successful automatic checks for 24 hours and keep offline startup silent.
- Add a manual **Check for updates** button for immediate checks and visible connection errors.

## 0.8.4 - 2026-08-21

- Recover valid ADT runtime bundles omitted from older `imsmanifest.xml` files.
- Show insufficient-storage requirements and availability in MB instead of raw bytes.
- Remove state and report files when every compression item fails before producing output.
- Explain zero-output cleanup consistently in the desktop UI and CLI.

## 0.8.3 - 2026-08-21

- Synchronize generated offline-preloader settings and video mappings during ADT publishing.
- Version the offline preloader and runtime bundle URLs so browsers load the newly published controls.
- Resume the visible silent sign-language video after narration starts on media-focus-limited browsers.
- Preserve transactional rollback for every HTML and preloader file changed by synchronization.

## 0.8.2 - 2026-08-21

- Suppress FFmpeg and FFprobe console-window popups during Windows desktop jobs.
- Make the desktop Publish ADT step update the selected website in place.
- Preserve repository and development files while transactionally replacing ADT video assets.
- Validate staged content before updating the website and restore original assets on failure.
- Prevent the desktop in-place workflow from creating, replacing, or modifying ZIP packages.
- Add explicit `publish --in-place` support for command-line and agent automation.
- Preserve the ADT hand-sign video control and verify that the published runtime supports it.
- Allow sign-language video and voice-over narration to play together without pausing each other.

## 0.8.1 - 2026-08-20

- Add a terminal-free Windows desktop executable while retaining the console CLI for automation.
- Replace the Windows VBScript launcher with a directly launchable executable.
- Build native macOS application bundles with PyInstaller instead of a manual shell wrapper.
- Validate macOS processor architecture and strict nested bundle integrity on native runners.
- Save terminal-free desktop startup failures to a durable user log and show its location.
- Build and smoke-test final native archives in CI before a version tag can be approved.

## 0.8.0

- Initial public cross-platform release with silent ≤5 MiB compression, quality validation,
  concurrency controls, drag-and-drop desktop UI, automation CLI, and ADT publishing.
