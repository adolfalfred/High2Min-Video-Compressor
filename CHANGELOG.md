# Changelog

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
