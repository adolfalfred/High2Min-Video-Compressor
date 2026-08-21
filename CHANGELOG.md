# Changelog

## 0.8.1 - in development

- Add a terminal-free Windows desktop executable while retaining the console CLI for automation.
- Replace the Windows VBScript launcher with a directly launchable executable.
- Build native macOS application bundles with PyInstaller instead of a manual shell wrapper.
- Validate macOS processor architecture and strict nested bundle integrity on native runners.
- Save terminal-free desktop startup failures to a durable user log and show its location.
- Build and smoke-test final native archives in CI before a version tag can be approved.

## 0.8.0

- Initial public cross-platform release with silent ≤5 MiB compression, quality validation,
  concurrency controls, drag-and-drop desktop UI, automation CLI, and ADT publishing.
