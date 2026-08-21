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
- source ADT website selection, a separate published-copy destination, language selection, and optional exact-manifest deployment ZIP creation.

ADT publishing uses the compressed-copy folder as its authoritative input. It never changes the selected source website, refuses to replace an existing destination, increments the copied book's bundle version, and writes a SHA-256 sidecar beside a deployment ZIP.

Keyboard shortcuts:

- `Ctrl+O`: choose a source folder;
- `Ctrl+Shift+O`: choose a source video;
- `Ctrl+R`: choose a saved job to resume;
- `Tab` and `Shift+Tab`: move through all controls.

The development build accepts an explicit FFmpeg path. Portable releases carry the appropriate runtime, so neither a system-wide Python nor FFmpeg installation is required.
