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
- ADT website selection, optional language selection, and transactional in-place video publishing.
- a throttled background update check plus a manual **Check for updates** button.

ADT publishing uses the compressed-copy folder as its authoritative input and updates the selected website itself. It first verifies local create/rename/delete permissions and temporary storage. It stages only the chosen language's videos and generated config, runtime, offline-preloader, affected HTML, and manifest files; it does not duplicate the complete website. The hand-sign control remains enabled, and voice-over narration can play together with sign-language video. A durable journal, rename-based commit, final validation, rollback, and automatic interrupted-transaction recovery protect the repository. Real phase percentages, current-file details, safe cancellation before commit, a 30-second no-progress warning, and a durable JSON-lines diagnostic log make slow filesystems visible. Development files and ZIP packages are never changed.

Resource-analysis failures show disk requirements in MB. When all compression items fail before producing output, the interface removes temporary state and report files and reports that cleanup instead of leaving a resumable job that cannot contain completed work.

On Windows, FFmpeg and FFprobe run without visible child console windows. Error and completion information remains available in the interface's status and activity log.

At startup, the desktop app checks the latest public stable GitHub release without blocking the interface. A successful automatic check is cached for 24 hours. If a newer version exists, the app asks before opening the verified GitHub release page; it does not download or replace binaries by itself. Network failures stay silent during startup but are shown after a manual update check.

Keyboard shortcuts:

- `Ctrl+O`: choose a source folder;
- `Ctrl+Shift+O`: choose a source video;
- `Ctrl+R`: choose a saved job to resume;
- `Tab` and `Shift+Tab`: move through all controls.

The development build accepts an explicit FFmpeg path. Portable releases carry the appropriate runtime, so neither a system-wide Python nor FFmpeg installation is required.
