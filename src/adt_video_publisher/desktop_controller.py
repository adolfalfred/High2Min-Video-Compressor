"""UI-independent desktop application controller."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .adt_planning import AdtPublishPlan, analyze_adt_publish
from .batch import BatchRunResult, ProgressCallback, load_resume_request, run_batch
from .compression import (
    DEFAULT_CRF,
    DEFAULT_MAXIMUM_ATTEMPTS,
    DEFAULT_MINIMUM_SSIM,
    DEFAULT_PRESET,
    DEFAULT_STRICT_SIZE,
)
from .diagnostics import DiagnosticLog
from .errors import InvalidInputError
from .paths import BatchPathPlan, build_path_plan
from .planning import DEFAULT_MAXIMUM_BYTES
from .publishing import PublishResult, publish_adt
from .resources import (
    ResourceSnapshot,
    WorkerPlan,
    detect_resources,
    select_worker_plan,
)


@dataclass(frozen=True, slots=True)
class DesktopSettings:
    source: str
    output: str | None = None
    recursive: bool = False
    workers: str | int = "auto"
    maximum_bytes: int = DEFAULT_MAXIMUM_BYTES
    maximum_attempts: int = DEFAULT_MAXIMUM_ATTEMPTS
    preset: str = DEFAULT_PRESET
    crf: int = DEFAULT_CRF
    minimum_ssim: float = DEFAULT_MINIMUM_SSIM
    strict_size: bool = DEFAULT_STRICT_SIZE
    adaptive_scale: bool = True
    ffmpeg_path: str | None = None
    probe_path: str | None = None


@dataclass(frozen=True, slots=True)
class DesktopPublishSettings:
    videos: str
    book: str
    output: str | None = None
    package: str | None = None
    in_place: bool = False
    language: str | None = None
    recursive: bool = False
    mapping_file: str | None = None
    mode: str = "merge"
    confirm_removals: bool = False
    maximum_bytes: int = DEFAULT_MAXIMUM_BYTES
    probe_path: str | None = None
    diagnostic_log: str | None = None


@dataclass(frozen=True, slots=True)
class AnalysisSummary:
    path_plan: BatchPathPlan
    resources: ResourceSnapshot
    worker_plan: WorkerPlan

    def to_dict(self) -> dict[str, object]:
        return {
            "path_plan": self.path_plan.to_dict(),
            "resources": self.resources.to_dict(),
            "worker_plan": self.worker_plan.to_dict(),
        }


def parse_workers(value: str) -> str | int:
    cleaned = value.strip().lower()
    if cleaned == "auto":
        return "auto"
    try:
        workers = int(cleaned)
    except ValueError as exc:
        raise InvalidInputError("Workers must be 'auto' or a positive integer.") from exc
    if workers < 1:
        raise InvalidInputError("Workers must be 'auto' or a positive integer.")
    return workers


def mebibytes_to_bytes(value: str) -> int:
    try:
        mebibytes = float(value.strip())
    except ValueError as exc:
        raise InvalidInputError("Maximum size must be a number of MiB.") from exc
    if mebibytes < 1:
        raise InvalidInputError("Maximum size must be at least 1 MiB.")
    return int(mebibytes * 1024 * 1024)


class DesktopController:
    """Share the same analysis and batch engine with the optional desktop UI."""

    def __init__(
        self,
        *,
        path_planner: Callable[..., BatchPathPlan] = build_path_plan,
        resource_detector: Callable[..., ResourceSnapshot] = detect_resources,
        worker_selector: Callable[..., WorkerPlan] = select_worker_plan,
        batch_runner: Callable[..., BatchRunResult] = run_batch,
        publisher: Callable[..., PublishResult] = publish_adt,
        publish_analyzer: Callable[..., AdtPublishPlan] = analyze_adt_publish,
    ) -> None:
        self.path_planner = path_planner
        self.resource_detector = resource_detector
        self.worker_selector = worker_selector
        self.batch_runner = batch_runner
        self.publisher = publisher
        self.publish_analyzer = publish_analyzer

    def analyze(self, settings: DesktopSettings) -> AnalysisSummary:
        plan = self.path_planner(
            settings.source,
            output=settings.output or None,
            recursive=settings.recursive,
        )
        resources = self.resource_detector(plan.output_root, ffmpeg_path=settings.ffmpeg_path)
        workers = self.worker_selector(
            resources,
            item_count=len(plan.items),
            requested_workers=settings.workers,
            maximum_bytes=settings.maximum_bytes,
        )
        return AnalysisSummary(path_plan=plan, resources=resources, worker_plan=workers)

    def compress(
        self,
        settings: DesktopSettings,
        *,
        cancel_event: threading.Event | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> BatchRunResult:
        return self.batch_runner(
            settings.source,
            output=settings.output or None,
            recursive=settings.recursive,
            requested_workers=settings.workers,
            maximum_bytes=settings.maximum_bytes,
            maximum_attempts=settings.maximum_attempts,
            preset=settings.preset,
            crf=settings.crf,
            minimum_ssim=settings.minimum_ssim,
            strict_size=settings.strict_size,
            adaptive_scale=settings.adaptive_scale,
            ffmpeg_path=settings.ffmpeg_path,
            probe_path=settings.probe_path,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )

    def resume(
        self,
        state_path: str | os.PathLike[str],
        *,
        workers: str | int = "auto",
        ffmpeg_path: str | None = None,
        probe_path: str | None = None,
        cancel_event: threading.Event | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> BatchRunResult:
        request = load_resume_request(state_path)
        return self.batch_runner(
            request.source,
            output=request.output,
            recursive=request.recursive,
            requested_workers=workers,
            maximum_bytes=request.maximum_bytes,
            maximum_attempts=request.maximum_attempts,
            preset=request.preset,
            crf=request.crf,
            minimum_ssim=request.minimum_ssim,
            strict_size=request.strict_size,
            adaptive_scale=request.adaptive_scale,
            ffmpeg_path=ffmpeg_path,
            probe_path=probe_path,
            resume=True,
            state_path=request.state_path,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )

    def publish(
        self,
        settings: DesktopPublishSettings,
        *,
        cancel_event: threading.Event | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> PublishResult:
        try:
            return self.publisher(
                settings.videos,
                book=settings.book,
                output=settings.output,
                package=settings.package or None,
                in_place=settings.in_place,
                language=settings.language or None,
                recursive=settings.recursive,
                mapping_file=settings.mapping_file,
                mode=settings.mode,
                confirm_removals=settings.confirm_removals,
                maximum_bytes=settings.maximum_bytes,
                probe_path=settings.probe_path,
                cancel_event=cancel_event,
                diagnostic_log=settings.diagnostic_log,
                progress_callback=progress_callback,
            )
        except BaseException as exc:
            DiagnosticLog(settings.diagnostic_log).write(
                "desktop_publish_failed",
                {"error_type": type(exc).__name__, "error": str(exc)},
            )
            raise

    def analyze_publish(self, settings: DesktopPublishSettings) -> AdtPublishPlan:
        return self.publish_analyzer(
            settings.videos,
            book=settings.book,
            language=settings.language or None,
            recursive=settings.recursive,
            mapping_file=settings.mapping_file,
            mode=settings.mode,
        )


def suggested_output(source: str) -> str:
    path = Path(source).expanduser()
    if not path.exists():
        return ""
    if path.is_dir():
        return str(path.parent / f"{path.name} - Compressed")
    return str(path.parent / "Compressed")
