# Desktop interface

Launch the optional interface with either command:

```text
high2min ui
high2min-ui
```

In a Windows release, extract the complete ZIP and double-click
`High2Min Video Compressor.exe`; it does not open a terminal. Keep the `_internal` directory beside
the executable. On macOS, extract the complete archive and open
`High2Min Video Compressor.app`. The separate `high2min` executable remains available for
automation on every platform.

If the terminal-free desktop launcher cannot initialize, it writes `startup.log` under the user's
normal application-log directory and displays that location in an error dialog.

The interface creates compressed copies in a separate destination and never replaces source videos. It provides:

- video-file or folder selection, including optional subfolders;
- native drag-and-drop for one video file or one folder containing many videos;
- automatic CPU, memory, disk, and H.264 hardware analysis;
- safe automatic or manually limited concurrency;
- a recommended ADT profile that enforces ≤5 MiB while preserving full resolution when possible;
- an optional high-quality profile, CRF control, and speed/quality preset;
- automatic source-versus-output SSIM validation with higher-quality retry;
- live per-video encoding/validation percentages, overall completion percentage, and an accessible text activity log;
- safe stop after currently running encodes;
- saved-job resume and links to JSON/CSV report locations.
- separate compression and publishing tabs;
- ADT website selection, optional language/mapping selection, merge/replace choice, read-only change preview, and transactional in-place video publishing.
- a throttled background update check plus a manual **Check for updates** button.

**Analyze ADT changes** first shows the inferred source filename → ADT spine page → normalized `page_N.mp4` mapping and the exact number of mutations, removals, warnings, and blockers without writing. Merge mode preserves existing mappings and is the default. Replace mode lists removals and asks for confirmation.

Publishing first verifies local create/rename/delete permissions and temporary storage. It stages only allowlisted video-integration files and never modifies active/inactive runtime bundles, authored CSS/content/audio, development files, or ZIP packages. Missing approved helpers keep the hand-sign control visible/touch-draggable and allow voice-over narration and sign-language video to play independently. Baseline hashes detect concurrent edits before commit. A durable journal, individual file renames, semantic final validation, rollback, and interrupted-transaction recovery protect the repository. Real phase percentages, safe cancellation before commit, a no-progress warning, and a JSON-lines diagnostic log make slow filesystems visible.

Resource-analysis failures show disk requirements in MB. When all compression items fail before producing output, the interface removes temporary state and report files and reports that cleanup instead of leaving a resumable job that cannot contain completed work.

On Windows, FFmpeg and FFprobe run without visible child console windows. Error and completion information remains available in the interface's status and activity log.

At startup, the desktop app checks the latest public stable GitHub release without blocking the interface. A successful automatic check is cached for 24 hours. If a newer version exists, the app asks before opening the verified GitHub release page; it does not download or replace binaries by itself. Network failures stay silent during startup but are shown after a manual update check.

Keyboard shortcuts:

- `Ctrl+O`: choose a source folder;
- `Ctrl+Shift+O`: choose a source video;
- `Ctrl+R`: choose a saved job to resume;
- `Tab` and `Shift+Tab`: move through all controls.

The development build accepts an explicit FFmpeg path. Portable releases carry the appropriate runtime, so neither a system-wide Python nor FFmpeg installation is required.
