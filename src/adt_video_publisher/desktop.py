"""Optional cross-platform Tk desktop interface."""

from __future__ import annotations

import os
import queue
import sys
import threading
from pathlib import Path
from typing import Any

from .batch import BatchRunResult
from .desktop_controller import (
    AnalysisSummary,
    DesktopController,
    DesktopPublishSettings,
    DesktopSettings,
    mebibytes_to_bytes,
    parse_workers,
    suggested_output,
)
from .errors import AdtVideoError, InvalidInputError
from .paths import SUPPORTED_VIDEO_EXTENSIONS
from .publishing import PublishResult


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _application_icon() -> Path:
    """Return the packaged window icon without relying on the working directory."""

    return Path(__file__).resolve().parent / "assets" / "high2min-video-compressor.png"


def create_application(root: Any, controller: DesktopController | None = None) -> Any:
    """Build the UI on an existing Tk root; kept separate for smoke testing."""

    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    class DesktopApplication:
        def __init__(self) -> None:
            self.root = root
            self.controller = controller or DesktopController()
            self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
            self.cancel_event = threading.Event()
            self.busy = False
            self.active_kind = ""
            self.close_requested = False
            self.completed_items = 0

            root.title("High2Min Video Compressor")
            root.minsize(860, 700)
            root.geometry("1080x880")
            self._window_icon = None
            icon_path = _application_icon()
            if icon_path.is_file():
                try:
                    self._window_icon = tk.PhotoImage(file=str(icon_path))
                    root.iconphoto(True, self._window_icon)
                except tk.TclError:
                    self._window_icon = None
            try:
                root.tk.call("tk", "scaling", 1.2)
            except tk.TclError:
                pass

            self.source_var = tk.StringVar()
            self.output_var = tk.StringVar()
            self.workers_var = tk.StringVar(value="auto")
            self.maximum_size_var = tk.StringVar(value="5")
            self.preset_var = tk.StringVar(value="medium")
            self.crf_var = tk.IntVar(value=35)
            self.profile_var = tk.StringVar(value="ADT website — ≤ 5 MiB (Recommended)")
            self.profile_info_var = tk.StringVar(
                value=(
                    "Good sign-language clarity with a hard 5 MiB limit. Full resolution is kept "
                    "whenever possible; longer videos are reduced proportionately only as needed."
                )
            )
            self.recursive_var = tk.BooleanVar(value=False)
            self.ffmpeg_var = tk.StringVar()
            self.book_var = tk.StringVar()
            self.language_var = tk.StringVar()
            self.status_var = tk.StringVar(value="Choose a video file or folder to begin.")
            self.analysis_var = tk.StringVar(value="System has not been analyzed yet.")
            self.progress_var = tk.DoubleVar(value=0)
            self.current_progress_var = tk.DoubleVar(value=0)
            self.overall_progress_text_var = tk.StringVar(value="Overall progress: 0%")
            self.current_progress_text_var = tk.StringVar(value="Current video: waiting")

            self._build(ttk, tk)
            self.profile_selector.bind("<<ComboboxSelected>>", self._apply_profile)
            root.protocol("WM_DELETE_WINDOW", self._on_close)
            root.bind("<Control-o>", lambda _event: self.choose_source_folder())
            root.bind("<Control-Shift-O>", lambda _event: self.choose_source_file())
            root.bind("<Control-r>", lambda _event: self.resume_job())
            root.after(100, self._poll_messages)

        def _build(self, ttk: Any, tk: Any) -> None:
            style = ttk.Style(self.root)
            style.configure("Primary.TButton", font=("TkDefaultFont", 10, "bold"), padding=(12, 8))
            style.configure("Title.TLabel", font=("TkDefaultFont", 19, "bold"))
            style.configure("Muted.TLabel", foreground="#4b5563")
            main = ttk.Frame(self.root, padding=16)
            main.grid(row=0, column=0, sticky="nsew")
            self.root.rowconfigure(0, weight=1)
            self.root.columnconfigure(0, weight=1)
            main.columnconfigure(1, weight=1)
            main.rowconfigure(5, weight=1)

            heading = ttk.Frame(main)
            heading.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 12))
            ttk.Label(heading, text="High2Min Video Compressor", style="Title.TLabel").grid(
                row=0, column=0, sticky="w"
            )
            ttk.Label(
                heading,
                text="Compress silent page videos, verify quality, and publish them safely into an ADT book.",
                style="Muted.TLabel",
            ).grid(row=1, column=0, sticky="w", pady=(3, 0))

            notebook = ttk.Notebook(main)
            notebook.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(0, 10))
            compress_tab = ttk.Frame(notebook, padding=10)
            publish_tab = ttk.Frame(notebook, padding=10)
            notebook.add(compress_tab, text="1. Compress videos")
            notebook.add(publish_tab, text="2. Publish ADT")
            compress_tab.columnconfigure(0, weight=1)
            publish_tab.columnconfigure(0, weight=1)

            source_group = ttk.LabelFrame(compress_tab, text="Videos and destination", padding=12)
            source_group.grid(row=0, column=0, sticky="ew", pady=(0, 10))
            source_group.columnconfigure(1, weight=1)
            self.drop_zone = tk.Label(
                source_group,
                text="Drop one video file or a folder of videos here",
                background="#eefbf4",
                foreground="#14532d",
                relief="groove",
                borderwidth=2,
                padx=12,
                pady=16,
                cursor="hand2",
            )
            self.drop_zone.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 12))
            self._configure_drop_target()
            ttk.Entry(source_group, textvariable=self.source_var).grid(
                row=1, column=1, sticky="ew", padx=8
            )
            ttk.Button(source_group, text="Choose folder…", command=self.choose_source_folder).grid(
                row=1, column=2, padx=(0, 6)
            )
            ttk.Button(source_group, text="Choose file…", command=self.choose_source_file).grid(
                row=1, column=3
            )
            ttk.Label(source_group, text="Source file or folder:").grid(row=1, column=0, sticky="w")
            ttk.Label(source_group, text="Compressed-copy folder:").grid(
                row=2, column=0, sticky="w", pady=(10, 0)
            )
            ttk.Entry(source_group, textvariable=self.output_var).grid(
                row=2, column=1, sticky="ew", padx=8, pady=(10, 0)
            )
            ttk.Button(source_group, text="Choose destination…", command=self.choose_output).grid(
                row=2, column=2, columnspan=2, sticky="ew", pady=(10, 0)
            )

            settings = ttk.LabelFrame(compress_tab, text="Compression settings", padding=12)
            settings.grid(row=1, column=0, sticky="ew")
            for column in (1, 3, 5):
                settings.columnconfigure(column, weight=1)
            ttk.Label(settings, text="Compression profile:").grid(row=0, column=0, sticky="w")
            self.profile_selector = ttk.Combobox(
                settings,
                textvariable=self.profile_var,
                values=(
                    "ADT website — ≤ 5 MiB (Recommended)",
                    "High quality — larger files",
                ),
                state="readonly",
            )
            self.profile_selector.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(6, 16))
            ttk.Label(
                settings,
                textvariable=self.profile_info_var,
                wraplength=540,
                justify="left",
                style="Muted.TLabel",
            ).grid(row=0, column=3, columnspan=3, sticky="w")

            ttk.Label(settings, text="Workers:").grid(row=1, column=0, sticky="w", pady=(12, 0))
            worker_values = ["auto", *[str(number) for number in range(1, max(2, (os.cpu_count() or 1) + 1))]]
            ttk.Combobox(
                settings,
                textvariable=self.workers_var,
                values=worker_values,
                width=9,
            ).grid(row=1, column=1, sticky="ew", padx=(6, 16), pady=(12, 0))
            ttk.Label(settings, text="Maximum size per video (MiB):").grid(
                row=1, column=2, sticky="w", pady=(12, 0)
            )
            ttk.Spinbox(
                settings,
                from_=1,
                to=100,
                increment=0.5,
                textvariable=self.maximum_size_var,
                width=9,
            ).grid(row=1, column=3, sticky="ew", padx=(6, 16), pady=(12, 0))
            ttk.Label(settings, text="Speed/quality preset:").grid(
                row=1, column=4, sticky="w", pady=(12, 0)
            )
            ttk.Combobox(
                settings,
                textvariable=self.preset_var,
                values=("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"),
                state="readonly",
                width=11,
            ).grid(row=1, column=5, sticky="ew", padx=(6, 0), pady=(12, 0))
            ttk.Checkbutton(
                settings,
                text="Include videos in subfolders",
                variable=self.recursive_var,
            ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
            ttk.Label(settings, text="FFmpeg (optional):").grid(
                row=2, column=2, sticky="w", pady=(10, 0)
            )
            ttk.Entry(settings, textvariable=self.ffmpeg_var).grid(
                row=2, column=3, columnspan=2, sticky="ew", padx=6, pady=(10, 0)
            )
            ttk.Button(settings, text="Choose FFmpeg…", command=self.choose_ffmpeg).grid(
                row=2, column=5, sticky="ew", pady=(10, 0)
            )
            ttk.Label(settings, text="Quality (CRF):").grid(row=3, column=0, sticky="w", pady=(10, 0))
            ttk.Spinbox(
                settings,
                from_=16,
                to=40,
                increment=1,
                textvariable=self.crf_var,
                width=9,
            ).grid(row=3, column=1, sticky="ew", padx=(6, 16), pady=(10, 0))
            ttk.Label(
                settings,
                text=(
                    "Audio is always removed. Each final video is checked for browser compatibility, "
                    "duration, frame rate, file size, and visual quality."
                ),
                wraplength=690,
                justify="left",
            ).grid(row=3, column=2, columnspan=4, sticky="w", pady=(10, 0))

            publishing = ttk.LabelFrame(publish_tab, text="Publish compressed videos", padding=12)
            publishing.grid(row=0, column=0, sticky="ew")
            publishing.columnconfigure(1, weight=1)
            ttk.Label(publishing, text="ADT website to update:").grid(row=0, column=0, sticky="w")
            ttk.Entry(publishing, textvariable=self.book_var).grid(
                row=0, column=1, sticky="ew", padx=8
            )
            ttk.Button(publishing, text="Choose book…", command=self.choose_book).grid(
                row=0, column=2, padx=(0, 6)
            )
            ttk.Label(publishing, text="Language (optional):").grid(row=0, column=3, sticky="w")
            ttk.Entry(publishing, textvariable=self.language_var, width=10).grid(
                row=0, column=4, sticky="ew", padx=(6, 0)
            )
            ttk.Label(
                publishing,
                text=(
                    "Publishing updates this ADT website in place with the compressed-copy folder above. "
                    "It does not create, replace, or modify any ZIP package."
                ),
                wraplength=850,
                justify="left",
            ).grid(row=1, column=0, columnspan=5, sticky="w", pady=(10, 0))

            system = ttk.LabelFrame(main, text="System analysis", padding=12)
            system.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(0, 10))
            system.columnconfigure(0, weight=1)
            ttk.Label(
                system,
                textvariable=self.analysis_var,
                justify="left",
                wraplength=880,
            ).grid(row=0, column=0, sticky="w")

            actions = ttk.Frame(main)
            actions.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(0, 10))
            self.analyze_button = ttk.Button(actions, text="Analyze system", command=self.analyze)
            self.analyze_button.grid(row=0, column=0, padx=(0, 8))
            self.start_button = ttk.Button(
                actions, text="Compress videos", command=self.compress, style="Primary.TButton"
            )
            self.start_button.grid(row=0, column=1, padx=(0, 8))
            self.resume_button = ttk.Button(actions, text="Resume saved job…", command=self.resume_job)
            self.resume_button.grid(row=0, column=2, padx=(0, 8))
            self.publish_button = ttk.Button(actions, text="Update ADT website", command=self.publish)
            self.publish_button.grid(row=0, column=3, padx=(0, 8))
            self.cancel_button = ttk.Button(
                actions,
                text="Stop after current videos",
                command=self.cancel,
                state="disabled",
            )
            self.cancel_button.grid(row=0, column=4)

            progress_frame = ttk.Frame(main)
            progress_frame.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(0, 10))
            progress_frame.columnconfigure(0, weight=1)
            ttk.Progressbar(
                progress_frame,
                variable=self.progress_var,
                maximum=100,
                mode="determinate",
            ).grid(row=0, column=0, sticky="ew")
            ttk.Label(progress_frame, textvariable=self.overall_progress_text_var).grid(
                row=0, column=1, sticky="e", padx=(10, 0)
            )
            ttk.Progressbar(
                progress_frame,
                variable=self.current_progress_var,
                maximum=100,
                mode="determinate",
            ).grid(row=1, column=0, sticky="ew", pady=(7, 0))
            ttk.Label(progress_frame, textvariable=self.current_progress_text_var).grid(
                row=1, column=1, sticky="e", padx=(10, 0), pady=(7, 0)
            )
            ttk.Label(
                progress_frame,
                textvariable=self.status_var,
                wraplength=880,
                justify="left",
            ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

            log_group = ttk.LabelFrame(main, text="Activity log", padding=8)
            log_group.grid(row=5, column=0, columnspan=4, sticky="nsew")
            log_group.rowconfigure(0, weight=1)
            log_group.columnconfigure(0, weight=1)
            self.log = tk.Text(log_group, height=12, wrap="word", state="disabled", takefocus=True)
            scrollbar = ttk.Scrollbar(log_group, orient="vertical", command=self.log.yview)
            self.log.configure(yscrollcommand=scrollbar.set)
            self.log.grid(row=0, column=0, sticky="nsew")
            scrollbar.grid(row=0, column=1, sticky="ns")

        def _configure_drop_target(self) -> None:
            try:
                from tkinterdnd2 import DND_FILES

                self.drop_zone.drop_target_register(DND_FILES)
                self.drop_zone.dnd_bind("<<DropEnter>>", self._on_drop_enter)
                self.drop_zone.dnd_bind("<<DropLeave>>", self._on_drop_leave)
                self.drop_zone.dnd_bind("<<Drop>>", self._on_drop)
                self.drop_zone.bind("<Button-1>", lambda _event: self.choose_source_folder())
            except Exception:
                self.drop_zone.configure(
                    text="Drag-and-drop is unavailable; use Choose folder or Choose file",
                    background="#f3f4f6",
                    foreground="#4b5563",
                    cursor="arrow",
                )

        def _on_drop_enter(self, _event: object) -> str:
            from tkinterdnd2 import COPY

            self.drop_zone.configure(background="#d1fae5", relief="solid")
            return COPY

        def _on_drop_leave(self, _event: object) -> str:
            from tkinterdnd2 import COPY

            self.drop_zone.configure(background="#eefbf4", relief="groove")
            return COPY

        def _on_drop(self, event: object) -> str:
            from tkinterdnd2 import COPY, REFUSE_DROP

            self._on_drop_leave(event)
            try:
                raw_paths = self.root.tk.splitlist(getattr(event, "data", ""))
            except Exception:
                raw_paths = ()
            paths = [Path(raw).expanduser() for raw in raw_paths if str(raw).strip()]
            if len(paths) != 1:
                messagebox.showinfo(
                    "Drop one source",
                    "Drop one video file or one folder at a time. A folder may contain many videos.",
                    parent=self.root,
                )
                return REFUSE_DROP
            selected = paths[0]
            if not selected.exists():
                messagebox.showerror("Invalid drop", f"The dropped path does not exist:\n{selected}", parent=self.root)
                return REFUSE_DROP
            if selected.is_file() and selected.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
                messagebox.showerror(
                    "Unsupported file",
                    "Drop a supported video file (MP4, MOV, M4V, AVI, MKV, or WEBM) or a folder.",
                    parent=self.root,
                )
                return REFUSE_DROP
            if not selected.is_file() and not selected.is_dir():
                messagebox.showerror("Invalid drop", "The dropped item is not a file or folder.", parent=self.root)
                return REFUSE_DROP
            self._set_source(str(selected.resolve()))
            self._append_log(f"Dropped source: {selected.resolve()}")
            return COPY

        def choose_source_folder(self) -> None:
            selected = filedialog.askdirectory(title="Choose folder containing videos")
            if selected:
                self._set_source(selected)

        def choose_source_file(self) -> None:
            selected = filedialog.askopenfilename(
                title="Choose a video",
                filetypes=[
                    ("Video files", "*.mp4 *.mov *.m4v *.avi *.mkv *.webm"),
                    ("All files", "*.*"),
                ],
            )
            if selected:
                self._set_source(selected)

        def _set_source(self, selected: str) -> None:
            self.source_var.set(selected)
            self.output_var.set(suggested_output(selected))
            self.status_var.set("Source selected. Analyze the system or begin compression.")

        def choose_output(self) -> None:
            selected = filedialog.askdirectory(
                title="Choose compressed-copy destination",
                mustexist=False,
            )
            if selected:
                self.output_var.set(selected)

        def choose_ffmpeg(self) -> None:
            selected = filedialog.askopenfilename(title="Choose FFmpeg executable")
            if selected:
                self.ffmpeg_var.set(selected)

        def choose_book(self) -> None:
            selected = filedialog.askdirectory(title="Choose ADT website to update")
            if not selected:
                return
            self.book_var.set(selected)

        def _apply_profile(self, _event: object | None = None) -> None:
            if self.profile_var.get().startswith("ADT website"):
                self.maximum_size_var.set("5")
                self.crf_var.set(35)
                self.preset_var.set("medium")
                self.profile_info_var.set(
                    "Good sign-language clarity with a hard 5 MiB limit. Full resolution is kept "
                    "whenever possible; longer videos are reduced proportionately only as needed."
                )
            else:
                self.crf_var.set(21)
                self.preset_var.set("medium")
                self.profile_info_var.set(
                    "Keeps the original dimensions and prioritizes maximum fidelity. Files may be "
                    "larger than the selected size target."
                )

        def _settings(self) -> DesktopSettings:
            source = self.source_var.get().strip()
            if not source:
                raise InvalidInputError("Choose a source video or folder first.")
            output = self.output_var.get().strip() or None
            ffmpeg = self.ffmpeg_var.get().strip() or None
            hard_limit = self.profile_var.get().startswith("ADT website")
            return DesktopSettings(
                source=source,
                output=output,
                recursive=self.recursive_var.get(),
                workers=parse_workers(self.workers_var.get()),
                maximum_bytes=mebibytes_to_bytes(self.maximum_size_var.get()),
                maximum_attempts=8 if hard_limit else 2,
                preset=self.preset_var.get(),
                crf=self.crf_var.get(),
                strict_size=hard_limit,
                adaptive_scale=hard_limit,
                ffmpeg_path=ffmpeg,
                probe_path=ffmpeg,
            )

        def analyze(self) -> None:
            try:
                settings = self._settings()
            except AdtVideoError as exc:
                messagebox.showerror("Cannot analyze", str(exc), parent=self.root)
                return
            self._start_task("analysis", lambda: self.controller.analyze(settings))

        def compress(self) -> None:
            try:
                settings = self._settings()
            except AdtVideoError as exc:
                messagebox.showerror("Cannot start compression", str(exc), parent=self.root)
                return
            self._start_task(
                "compression",
                lambda: self.controller.compress(
                    settings,
                    cancel_event=self.cancel_event,
                    progress_callback=self._queue_progress,
                ),
            )

        def publish(self) -> None:
            compressed = self.output_var.get().strip()
            book = self.book_var.get().strip()
            if not compressed or not book:
                messagebox.showerror(
                    "Cannot publish",
                    "Choose the compressed-copy folder and the ADT website to update.",
                    parent=self.root,
                )
                return
            try:
                settings = DesktopPublishSettings(
                    videos=compressed,
                    book=book,
                    in_place=True,
                    language=self.language_var.get().strip() or None,
                    recursive=self.recursive_var.get(),
                    maximum_bytes=mebibytes_to_bytes(self.maximum_size_var.get()),
                    probe_path=self.ffmpeg_var.get().strip() or None,
                )
            except AdtVideoError as exc:
                messagebox.showerror("Cannot publish", str(exc), parent=self.root)
                return
            self._start_task(
                "publishing",
                lambda: self.controller.publish(settings, progress_callback=self._queue_progress),
            )

        def resume_job(self) -> None:
            selected = filedialog.askopenfilename(
                title="Choose saved High2Min Video Compressor job",
                filetypes=[("ADT job state", "*.json"), ("All files", "*.*")],
            )
            if not selected:
                return
            try:
                workers = parse_workers(self.workers_var.get())
            except AdtVideoError as exc:
                messagebox.showerror("Cannot resume", str(exc), parent=self.root)
                return
            ffmpeg = self.ffmpeg_var.get().strip() or None
            self._start_task(
                "resume",
                lambda: self.controller.resume(
                    selected,
                    workers=workers,
                    ffmpeg_path=ffmpeg,
                    probe_path=ffmpeg,
                    cancel_event=self.cancel_event,
                    progress_callback=self._queue_progress,
                ),
            )

        def _start_task(self, kind: str, work: Any) -> None:
            if self.busy:
                messagebox.showinfo("Job in progress", "Wait for the current task to finish.", parent=self.root)
                return
            self.busy = True
            self.active_kind = kind
            self.cancel_event = threading.Event()
            self.completed_items = 0
            self.progress_var.set(0)
            self.current_progress_var.set(0)
            self.overall_progress_text_var.set("Overall progress: 0%")
            self.current_progress_text_var.set("Current video: starting")
            self._set_controls_busy(True)
            self.status_var.set("Working…")
            self._append_log(f"Started {kind}.")

            def runner() -> None:
                try:
                    value = work()
                except BaseException as exc:
                    self.messages.put(("error", exc))
                else:
                    self.messages.put(("done", value))

            threading.Thread(target=runner, name=f"adt-ui-{kind}", daemon=False).start()

        def _queue_progress(self, job_id: str, event: str, payload: dict[str, object]) -> None:
            self.messages.put(("progress", (job_id, event, payload)))

        def _poll_messages(self) -> None:
            try:
                while True:
                    kind, value = self.messages.get_nowait()
                    if kind == "progress":
                        self._handle_progress(value)
                    elif kind == "done":
                        self._handle_done(value)
                    elif kind == "error":
                        self._handle_error(value)
            except queue.Empty:
                pass
            if self.root.winfo_exists():
                self.root.after(100, self._poll_messages)

        def _handle_progress(self, value: object) -> None:
            _job_id, event, payload = value  # type: ignore[misc]
            if event == "job_started":
                total = max(1, int(payload.get("total", 1)))
                self.progress_var.set(0)
                self._progress_maximum = total
                self.overall_progress_text_var.set("Overall progress: 0%")
                self.status_var.set(f"Processing {total} video(s) — 0% complete…")
            elif event == "item_progress":
                percent = max(0.0, min(100.0, float(payload.get("percent", 0))))
                source = Path(str(payload.get("source", "video"))).name
                attempt = int(payload.get("attempt", 1))
                phase = str(payload.get("phase", "encoding"))
                self.current_progress_var.set(percent)
                if phase == "validating":
                    phase_text = "validating quality"
                else:
                    phase_text = f"encoding pass {attempt}"
                self.current_progress_text_var.set(
                    f"{source}: {phase_text} — {percent:.1f}%"
                )
            elif event in {"item_completed", "item_failed"}:
                self.completed_items += 1
                total = getattr(self, "_progress_maximum", max(1, self.completed_items))
                overall_percent = min(100.0, self.completed_items / total * 100)
                self.progress_var.set(overall_percent)
                self.overall_progress_text_var.set(f"Overall progress: {overall_percent:.1f}%")
                source = Path(str(payload.get("source", "video"))).name
                status = str(payload.get("status", "failed" if event == "item_failed" else "completed"))
                self.current_progress_var.set(100 if status != "failed" else 0)
                self.current_progress_text_var.set(f"{source}: {status}")
                self.status_var.set(
                    f"{self.completed_items}/{total}: {source} — {status}; "
                    f"{overall_percent:.1f}% complete"
                )
                quality = payload.get("quality_score")
                quality_text = f", SSIM {float(quality):.4f}" if quality is not None else ""
                target_text = ", above size limit" if payload.get("target_exceeded") else ""
                self._append_log(f"{source}: {status}{quality_text}{target_text}")
            elif event == "resource_throttled":
                self._append_log(
                    f"Worker count limited by {payload.get('limiting_factor', 'available resources')}."
                )
            elif event in {"job_completed", "job_interrupted"}:
                self._append_log(event.replace("_", " ").capitalize() + ".")

        def _handle_done(self, value: object) -> None:
            if isinstance(value, AnalysisSummary):
                resource = value.resources
                workers = value.worker_plan
                hardware = ", ".join(resource.hardware_encoders) or "none detected"
                self.analysis_var.set(
                    f"{resource.logical_cpu_count} logical CPU(s), "
                    f"{resource.physical_cpu_count or 'unknown'} physical core(s); "
                    f"{_format_bytes(resource.available_memory_bytes)} RAM available; "
                    f"{_format_bytes(resource.available_disk_bytes)} disk available.\n"
                    f"Safe plan: {workers.workers} concurrent worker(s), "
                    f"{workers.encoder_threads_per_worker} encoder thread(s) each; "
                    f"hardware H.264: {hardware}. Videos found: {len(value.path_plan.items)}."
                )
                self.status_var.set("System analysis completed. No videos were changed.")
                self._append_log("System analysis completed.")
            elif isinstance(value, BatchRunResult):
                summary = value.summary
                self.progress_var.set(100 if summary.total else 0)
                self.overall_progress_text_var.set(
                    "Overall progress: 100%" if summary.total else "Overall progress: 0%"
                )
                self.current_progress_var.set(100 if summary.completed else 0)
                self.current_progress_text_var.set("Compression job finished")
                if value.job_details_removed:
                    self.status_var.set(
                        f"Failed: no videos were completed. {summary.failed} failed; "
                        "temporary job details were removed."
                    )
                    self._append_log(
                        "No output was produced; temporary job details were removed."
                    )
                else:
                    self.status_var.set(
                        f"Finished: {summary.completed} completed, {summary.skipped} skipped, "
                        f"{summary.failed} failed. Reports are in {value.json_report_path.parent}."
                    )
                    self._append_log(f"JSON report: {value.json_report_path}")
                    self._append_log(f"CSV report: {value.csv_report_path}")
            elif isinstance(value, PublishResult):
                self.progress_var.set(100)
                self.overall_progress_text_var.set("Overall progress: 100%")
                self.current_progress_var.set(100)
                self.current_progress_text_var.set("Publishing finished")
                self.status_var.set(
                    f"Updated {value.output_book} with {len(value.videos)} video(s). "
                    f"Bundle version: {value.bundle_version}. No ZIP package was changed."
                )
                self._append_log(f"Updated ADT website in place: {value.output_book}")
            self._finish_task()

        def _handle_error(self, value: object) -> None:
            message = str(value)
            self.status_var.set(f"Task failed: {message}")
            self._append_log(f"Error: {message}")
            messagebox.showerror("High2Min Video Compressor", message, parent=self.root)
            self._finish_task()

        def _finish_task(self) -> None:
            self.busy = False
            self.active_kind = ""
            self._set_controls_busy(False)
            if self.close_requested:
                self.root.destroy()

        def _set_controls_busy(self, busy: bool) -> None:
            normal = "disabled" if busy else "normal"
            for button in (
                self.analyze_button,
                self.start_button,
                self.resume_button,
                self.publish_button,
            ):
                button.configure(state=normal)
            self.cancel_button.configure(
                state="normal" if busy and self.active_kind in {"compression", "resume"} else "disabled"
            )

        def cancel(self) -> None:
            if not self.busy:
                return
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.status_var.set(
                "Stop requested. Current encodes will finish safely; videos not yet started will be skipped."
            )
            self._append_log("Safe stop requested.")

        def _append_log(self, message: str) -> None:
            self.log.configure(state="normal")
            self.log.insert("end", message + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")

        def _on_close(self) -> None:
            if not self.busy:
                self.root.destroy()
                return
            should_close = messagebox.askyesno(
                "Job in progress",
                "Request a safe stop and close after current videos finish?",
                parent=self.root,
            )
            if should_close:
                self.close_requested = True
                self.cancel()

    return DesktopApplication()


def run() -> int:
    """Launch the optional desktop UI without affecting CLI-only environments."""

    try:
        from tkinterdnd2 import TkinterDnD

        root = TkinterDnD.Tk()
    except Exception as exc:
        # TclError is platform-specific; keep the CLI usable when no display is available.
        sys.stderr.write(f"high2min-ui: desktop interface is unavailable: {exc}\n")
        return 69
    application = create_application(root)
    _ = application
    root.mainloop()
    return 0


def smoke_test() -> int:
    """Construct the complete desktop interface invisibly for release validation."""

    try:
        from tkinterdnd2 import TkinterDnD

        root = TkinterDnD.Tk()
        root.withdraw()
        application = create_application(root)
        root.update_idletasks()
        if application.publish_button.cget("text") != "Update ADT website":
            raise RuntimeError("Publishing controls were not created.")
        if "Drop one video" not in application.drop_zone.cget("text"):
            raise RuntimeError("Drag-and-drop controls were not created.")
        root.destroy()
    except Exception as exc:
        sys.stderr.write(f"high2min-ui: desktop smoke test failed: {exc}\n")
        return 69
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
