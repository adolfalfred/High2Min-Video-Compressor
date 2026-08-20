"""Concurrent, resumable batch compression with durable reports."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from .compression import (
    DEFAULT_CRF,
    DEFAULT_MAXIMUM_ATTEMPTS,
    DEFAULT_MINIMUM_SSIM,
    DEFAULT_PRESET,
    DEFAULT_STRICT_SIZE,
    CompressionResult,
    compress_video,
    fingerprint_source,
)
from .contracts import CONTRACT_SCHEMA_VERSION, ExitCode
from .errors import InvalidInputError, UnsafePathError
from .paths import BatchPathPlan, FilePathPlan, build_path_plan
from .planning import DEFAULT_MAXIMUM_BYTES
from .resources import ResourceSnapshot, WorkerPlan, detect_resources, select_worker_plan

BatchItemStatus = Literal["completed", "skipped", "failed"]
CompressionFunction = Callable[..., CompressionResult]
ProgressCallback = Callable[[str, str, dict[str, object]], None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.is_file():
            temporary.unlink()


def _atomic_write_json(path: Path, document: dict[str, object]) -> None:
    _atomic_write_text(path, json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


@dataclass(frozen=True, slots=True)
class BatchItemResult:
    source: Path
    output: Path
    status: BatchItemStatus
    original_bytes: int
    output_bytes: int
    reduction_percent: float | None
    attempts: int
    quality_score: float | None
    target_exceeded: bool
    error: dict[str, str] | None

    def to_result_dict(self) -> dict[str, object]:
        return {
            "source": str(self.source),
            "output": str(self.output),
            "status": self.status,
            "original_bytes": self.original_bytes,
            "output_bytes": self.output_bytes,
            "quality_score": self.quality_score,
            "target_exceeded": self.target_exceeded,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class BatchSummary:
    total: int
    completed: int
    skipped: int
    failed: int
    original_bytes: int
    output_bytes: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BatchRunResult:
    job_id: str
    ok: bool
    exit_code: int
    interrupted: bool
    summary: BatchSummary
    items: tuple[BatchItemResult, ...]
    resources: ResourceSnapshot
    worker_plan: WorkerPlan
    state_path: Path
    json_report_path: Path
    csv_report_path: Path

    def to_result_document(self) -> dict[str, object]:
        return {
            "schema_version": CONTRACT_SCHEMA_VERSION,
            "job_id": self.job_id,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "summary": self.summary.to_dict(),
            "items": [item.to_result_dict() for item in self.items],
        }

    def to_dict(self) -> dict[str, object]:
        document = self.to_result_document()
        document.update(
            {
                "interrupted": self.interrupted,
                "resources": self.resources.to_dict(),
                "worker_plan": self.worker_plan.to_dict(),
                "state_path": str(self.state_path),
                "json_report_path": str(self.json_report_path),
                "csv_report_path": str(self.csv_report_path),
            }
        )
        return document


@dataclass(frozen=True, slots=True)
class ResumeRequest:
    state_path: Path
    source: str
    output: str
    maximum_bytes: int
    maximum_attempts: int
    preset: str
    crf: int
    minimum_ssim: float
    strict_size: bool
    adaptive_scale: bool
    recursive: bool


def load_resume_request(state_path: str | os.PathLike[str]) -> ResumeRequest:
    """Read only the trusted settings needed to resume; run_batch revalidates the full state."""

    path = Path(state_path).expanduser().resolve()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        settings = state["settings"]
        source = state["source"]
        output = state["output"]
        maximum_bytes = settings["maximum_bytes"]
        maximum_attempts = settings["maximum_attempts"]
        preset = settings["preset"]
        crf = settings.get("crf", DEFAULT_CRF)
        minimum_ssim = settings.get("minimum_ssim", DEFAULT_MINIMUM_SSIM)
        strict_size = settings.get("strict_size", DEFAULT_STRICT_SIZE)
        adaptive_scale = settings.get("adaptive_scale", strict_size)
        recursive = settings["recursive"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise InvalidInputError(f"Job state is unreadable or incomplete: '{path}'.") from exc
    if not isinstance(source, str) or not source or not isinstance(output, str) or not output:
        raise InvalidInputError(f"Job state has invalid source/output paths: '{path}'.")
    if not isinstance(maximum_bytes, int) or isinstance(maximum_bytes, bool) or maximum_bytes < 1:
        raise InvalidInputError(f"Job state has an invalid maximum size: '{path}'.")
    if (
        not isinstance(maximum_attempts, int)
        or isinstance(maximum_attempts, bool)
        or not 1 <= maximum_attempts <= 8
    ):
        raise InvalidInputError(f"Job state has an invalid attempt count: '{path}'.")
    if (
        not isinstance(preset, str)
        or not preset
        or not isinstance(crf, int)
        or isinstance(crf, bool)
        or not 0 <= crf <= 51
        or not isinstance(minimum_ssim, (int, float))
        or isinstance(minimum_ssim, bool)
        or not 0 < float(minimum_ssim) <= 1
        or not isinstance(strict_size, bool)
        or not isinstance(adaptive_scale, bool)
        or not isinstance(recursive, bool)
    ):
        raise InvalidInputError(f"Job state has invalid compression settings: '{path}'.")
    return ResumeRequest(
        state_path=path,
        source=source,
        output=output,
        maximum_bytes=maximum_bytes,
        maximum_attempts=maximum_attempts,
        preset=preset,
        crf=crf,
        minimum_ssim=float(minimum_ssim),
        strict_size=strict_size,
        adaptive_scale=adaptive_scale,
        recursive=recursive,
    )


class JobStateStore:
    """Serialize all state transitions so every saved document is complete JSON."""

    def __init__(self, path: Path, document: dict[str, object]) -> None:
        self.path = path
        self.document = document
        self._lock = threading.Lock()
        self._items = {
            str(item["source"]): item
            for item in document["items"]  # type: ignore[index]
        }

    def save(self) -> None:
        with self._lock:
            self._save_unlocked()

    def _save_unlocked(self) -> None:
        self.document["updated_at"] = _utc_now()
        _atomic_write_json(self.path, self.document)

    def update_job_status(self, status: str) -> None:
        with self._lock:
            self.document["status"] = status
            self._save_unlocked()

    def update_item(self, source: Path, **changes: object) -> None:
        with self._lock:
            item = self._items[str(source)]
            item.update(changes)
            self._save_unlocked()

    def item(self, source: Path) -> dict[str, object]:
        return self._items[str(source)]


def _new_state(
    *,
    job_id: str,
    plan: BatchPathPlan,
    maximum_bytes: int,
    preset: str,
    crf: int,
    minimum_ssim: float,
    strict_size: bool,
    adaptive_scale: bool,
    maximum_attempts: int,
    recursive: bool,
    resources: ResourceSnapshot,
    workers: WorkerPlan,
) -> dict[str, object]:
    now = _utc_now()
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "job_id": job_id,
        "status": "ready",
        "created_at": now,
        "updated_at": now,
        "source": str(plan.source),
        "output": str(plan.output_root),
        "settings": {
            "maximum_bytes": maximum_bytes,
            "preset": preset,
            "crf": crf,
            "minimum_ssim": minimum_ssim,
            "strict_size": strict_size,
            "adaptive_scale": adaptive_scale,
            "maximum_attempts": maximum_attempts,
            "recursive": recursive,
        },
        "resources": resources.to_dict(),
        "worker_plan": workers.to_dict(),
        "items": [
            {
                "source": str(item.source),
                "output": str(item.output),
                "status": "pending",
                "run_count": 0,
                "result": None,
                "error": None,
            }
            for item in plan.items
        ],
    }


def _load_state(path: Path) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidInputError(f"Job state is unreadable: '{path}'.") from exc
    if not isinstance(document, dict):
        raise InvalidInputError(f"Job state must be a JSON object: '{path}'.")
    return document


def _validate_resume_state(
    state: dict[str, object],
    *,
    plan: BatchPathPlan,
    maximum_bytes: int,
    preset: str,
    crf: int,
    minimum_ssim: float,
    strict_size: bool,
    adaptive_scale: bool,
    maximum_attempts: int,
    recursive: bool,
) -> None:
    if state.get("schema_version") != CONTRACT_SCHEMA_VERSION:
        raise InvalidInputError("Job state schema version is unsupported.")
    if not isinstance(state.get("job_id"), str) or not state["job_id"]:
        raise InvalidInputError("Job state has no valid job identifier.")
    if state.get("source") != str(plan.source) or state.get("output") != str(plan.output_root):
        raise UnsafePathError("Job state source/output paths do not match the requested batch.")
    expected_settings = {
        "maximum_bytes": maximum_bytes,
        "preset": preset,
        "crf": crf,
        "minimum_ssim": minimum_ssim,
        "strict_size": strict_size,
        "adaptive_scale": adaptive_scale,
        "maximum_attempts": maximum_attempts,
        "recursive": recursive,
    }
    actual_settings = state.get("settings")
    if isinstance(actual_settings, dict):
        actual_settings = dict(actual_settings)
        actual_settings.setdefault(
            "adaptive_scale",
            bool(actual_settings.get("strict_size", DEFAULT_STRICT_SIZE)),
        )
    if actual_settings != expected_settings:
        raise InvalidInputError("Job state compression settings do not match the requested batch.")
    raw_items = state.get("items")
    if not isinstance(raw_items, list):
        raise InvalidInputError("Job state has no valid item collection.")
    expected = {(str(item.source), str(item.output)) for item in plan.items}
    actual = {
        (str(item.get("source")), str(item.get("output")))
        for item in raw_items
        if isinstance(item, dict)
    }
    if actual != expected or len(raw_items) != len(plan.items):
        raise UnsafePathError("Job state items do not match the current safe path plan.")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resume_item_is_valid(plan: FilePathPlan, state_item: dict[str, object]) -> bool:
    if state_item.get("status") != "completed":
        return False
    result = state_item.get("result")
    if not isinstance(result, dict) or not plan.output.is_file() or not plan.source.is_file():
        return False
    try:
        return (
            plan.source.stat().st_size == int(result["source_size_bytes"])
            and plan.output.stat().st_size == int(result["output_size_bytes"])
            and _file_sha256(plan.source) == result["source_sha256"]
            and _file_sha256(plan.output) == result["output_sha256"]
        )
    except (KeyError, OSError, TypeError, ValueError):
        return False


def _state_result(result: CompressionResult) -> dict[str, object]:
    final_validation = result.attempts[-1].validation if result.attempts else None
    return {
        "source_size_bytes": result.source_size_bytes,
        "output_size_bytes": result.output_size_bytes,
        "reduction_percent": result.reduction_percent,
        "source_sha256": result.source_sha256,
        "output_sha256": result.output_sha256,
        "attempts": len(result.attempts),
        "quality_score": final_validation.quality_score if final_validation else None,
        "target_exceeded": (
            not final_validation.checks["size_within_limit"] if final_validation else False
        ),
    }


def _skipped_from_state(plan: FilePathPlan, state_item: dict[str, object]) -> BatchItemResult:
    result = state_item["result"]
    assert isinstance(result, dict)
    return BatchItemResult(
        source=plan.source,
        output=plan.output,
        status="skipped",
        original_bytes=int(result["source_size_bytes"]),
        output_bytes=int(result["output_size_bytes"]),
        reduction_percent=float(result["reduction_percent"]),
        attempts=int(result["attempts"]),
        quality_score=(
            float(result["quality_score"])
            if result.get("quality_score") is not None
            else None
        ),
        target_exceeded=bool(result.get("target_exceeded", False)),
        error=None,
    )


def _cancelled_item(plan: FilePathPlan) -> BatchItemResult:
    return BatchItemResult(
        source=plan.source,
        output=plan.output,
        status="skipped",
        original_bytes=plan.source.stat().st_size,
        output_bytes=0,
        reduction_percent=None,
        attempts=0,
        quality_score=None,
        target_exceeded=False,
        error={"type": "Interrupted", "message": "Item was not started before cancellation."},
    )


def _summary(items: list[BatchItemResult]) -> BatchSummary:
    return BatchSummary(
        total=len(items),
        completed=sum(item.status == "completed" for item in items),
        skipped=sum(item.status == "skipped" for item in items),
        failed=sum(item.status == "failed" for item in items),
        original_bytes=sum(item.original_bytes for item in items),
        output_bytes=sum(item.output_bytes for item in items),
    )


def _csv_report(items: list[BatchItemResult]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "source",
            "output",
            "status",
            "original_bytes",
            "output_bytes",
            "reduction_percent",
            "attempts",
            "quality_score",
            "target_exceeded",
            "error_type",
            "error_message",
        ],
    )
    writer.writeheader()
    for item in items:
        writer.writerow(
            {
                "source": str(item.source),
                "output": str(item.output),
                "status": item.status,
                "original_bytes": item.original_bytes,
                "output_bytes": item.output_bytes,
                "reduction_percent": item.reduction_percent if item.reduction_percent is not None else "",
                "attempts": item.attempts,
                "quality_score": item.quality_score if item.quality_score is not None else "",
                "target_exceeded": item.target_exceeded,
                "error_type": item.error["type"] if item.error else "",
                "error_message": item.error["message"] if item.error else "",
            }
        )
    return output.getvalue()


def run_batch(
    source: str | os.PathLike[str],
    *,
    output: str | os.PathLike[str] | None = None,
    recursive: bool = False,
    requested_workers: str | int = "auto",
    maximum_bytes: int = DEFAULT_MAXIMUM_BYTES,
    maximum_attempts: int = DEFAULT_MAXIMUM_ATTEMPTS,
    preset: str = DEFAULT_PRESET,
    crf: int = DEFAULT_CRF,
    minimum_ssim: float = DEFAULT_MINIMUM_SSIM,
    strict_size: bool = DEFAULT_STRICT_SIZE,
    adaptive_scale: bool = True,
    ffmpeg_path: str | os.PathLike[str] | None = None,
    probe_path: str | os.PathLike[str] | None = None,
    resume: bool = False,
    state_path: str | os.PathLike[str] | None = None,
    cancel_event: threading.Event | None = None,
    resource_snapshot: ResourceSnapshot | None = None,
    compression_function: CompressionFunction = compress_video,
    progress_callback: ProgressCallback | None = None,
) -> BatchRunResult:
    """Compress a directory concurrently and preserve enough state for safe resume."""

    plan = build_path_plan(source, output=output, recursive=recursive)
    plan.output_root.mkdir(parents=True, exist_ok=True)
    state_file = (
        Path(state_path).expanduser().resolve(strict=False)
        if state_path is not None
        else plan.output_root / ".adt-video-job.json"
    )
    json_report = plan.output_root / "compression-report.json"
    csv_report = plan.output_root / "compression-report.csv"
    try:
        state_file.relative_to(plan.output_root)
    except ValueError as exc:
        raise UnsafePathError("Job state must be stored inside the separate output directory.") from exc
    if state_file.suffix.lower() != ".json" or state_file == json_report:
        raise UnsafePathError("Job state must use its own .json file inside the output directory.")

    valid_resume_sources: set[str] = set()
    if resume:
        if not state_file.is_file():
            raise InvalidInputError(f"Resume state does not exist: '{state_file}'.")
        state = _load_state(state_file)
        _validate_resume_state(
            state,
            plan=plan,
            maximum_bytes=maximum_bytes,
            preset=preset,
            crf=crf,
            minimum_ssim=minimum_ssim,
            strict_size=strict_size,
            adaptive_scale=adaptive_scale,
            maximum_attempts=maximum_attempts,
            recursive=recursive,
        )
        job_id = str(state["job_id"])
        state_items = {
            str(item["source"]): item
            for item in state["items"]  # type: ignore[index]
        }
        for item in plan.items:
            if _resume_item_is_valid(item, state_items[str(item.source)]):
                valid_resume_sources.add(str(item.source))
    else:
        if state_file.exists():
            raise UnsafePathError(f"Job state already exists; use resume: '{state_file}'.")
        job_id = uuid.uuid4().hex

    resources = resource_snapshot or detect_resources(plan.output_root, ffmpeg_path=ffmpeg_path)
    pending_count = len(plan.items) - len(valid_resume_sources)
    if pending_count:
        worker_plan = select_worker_plan(
            resources,
            item_count=pending_count,
            requested_workers=requested_workers,
            maximum_bytes=maximum_bytes,
        )
    else:
        physical = resources.physical_cpu_count or max(1, resources.logical_cpu_count // 2)
        worker_plan = WorkerPlan(
            requested_workers=requested_workers,
            workers=1,
            encoder_threads_per_worker=max(1, resources.logical_cpu_count),
            cpu_worker_limit=max(1, physical // 2),
            memory_worker_limit=0,
            item_limit=0,
            limiting_factor="resume",
            estimated_required_disk_bytes=0,
        )

    if resume:
        state["resources"] = resources.to_dict()
        state["worker_plan"] = worker_plan.to_dict()
    else:
        state = _new_state(
            job_id=job_id,
            plan=plan,
            maximum_bytes=maximum_bytes,
            preset=preset,
            crf=crf,
            minimum_ssim=minimum_ssim,
            strict_size=strict_size,
            adaptive_scale=adaptive_scale,
            maximum_attempts=maximum_attempts,
            recursive=recursive,
            resources=resources,
            workers=worker_plan,
        )

    store = JobStateStore(state_file, state)
    store.update_job_status("running")
    cancellation = cancel_event or threading.Event()
    item_results: dict[str, BatchItemResult] = {}
    pending: list[FilePathPlan] = []

    def notify(event: str, payload: dict[str, object]) -> None:
        if progress_callback is not None:
            progress_callback(job_id, event, payload)

    notify(
        "job_started",
        {
            "total": len(plan.items),
            "pending": pending_count,
            "workers": worker_plan.workers,
            "encoder_threads_per_worker": worker_plan.encoder_threads_per_worker,
            "output": str(plan.output_root),
        },
    )
    if worker_plan.limiting_factor in {"cpu", "memory"}:
        notify(
            "resource_throttled",
            {
                "limiting_factor": worker_plan.limiting_factor,
                "workers": worker_plan.workers,
                "cpu_worker_limit": worker_plan.cpu_worker_limit,
                "memory_worker_limit": worker_plan.memory_worker_limit,
            },
        )

    for item in plan.items:
        state_item = store.item(item.source)
        if str(item.source) in valid_resume_sources:
            item_results[str(item.source)] = _skipped_from_state(item, state_item)
            notify(
                "item_completed",
                {"source": str(item.source), "output": str(item.output), "status": "skipped"},
            )
            continue
        if state_item.get("status") == "running":
            store.update_item(item.source, status="pending")
        pending.append(item)

    def process(item: FilePathPlan) -> BatchItemResult:
        if cancellation.is_set():
            cancelled = _cancelled_item(item)
            notify(
                "item_completed",
                {"source": str(item.source), "output": str(item.output), "status": "skipped"},
            )
            return cancelled
        current = store.item(item.source)
        run_count = int(current.get("run_count", 0)) + 1
        store.update_item(item.source, status="running", run_count=run_count, error=None)
        notify(
            "item_started",
            {"source": str(item.source), "output": str(item.output), "run_count": run_count},
        )
        try:
            result = compression_function(
                item.source,
                item.output,
                ffmpeg_path=ffmpeg_path,
                probe_path=probe_path,
                maximum_bytes=maximum_bytes,
                maximum_attempts=maximum_attempts,
                preset=preset,
                crf=crf,
                minimum_ssim=minimum_ssim,
                strict_size=strict_size,
                adaptive_scale=adaptive_scale,
                encoder_threads=worker_plan.encoder_threads_per_worker,
                progress_callback=lambda phase, attempt, percent: notify(
                    "item_progress",
                    {
                        "source": str(item.source),
                        "output": str(item.output),
                        "phase": phase,
                        "attempt": attempt,
                        "percent": round(percent, 1),
                    },
                ),
            )
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            store.update_item(item.source, status="failed", error=error)
            notify(
                "item_failed",
                {"source": str(item.source), "output": str(item.output), "error": error},
            )
            return BatchItemResult(
                source=item.source,
                output=item.output,
                status="failed",
                original_bytes=item.source.stat().st_size,
                output_bytes=item.output.stat().st_size if item.output.is_file() else 0,
                reduction_percent=None,
                attempts=0,
                quality_score=None,
                target_exceeded=False,
                error=error,
            )
        result_state = _state_result(result)
        store.update_item(item.source, status="completed", result=result_state, error=None)
        notify(
            "item_completed",
            {
                "source": str(item.source),
                "output": str(item.output),
                "status": "completed",
                "original_bytes": result.source_size_bytes,
                "output_bytes": result.output_size_bytes,
                "reduction_percent": result.reduction_percent,
                "quality_score": result_state["quality_score"],
                "target_exceeded": result_state["target_exceeded"],
            },
        )
        return BatchItemResult(
            source=item.source,
            output=item.output,
            status="completed",
            original_bytes=result.source_size_bytes,
            output_bytes=result.output_size_bytes,
            reduction_percent=result.reduction_percent,
            attempts=len(result.attempts),
            quality_score=(
                result.attempts[-1].validation.quality_score if result.attempts else None
            ),
            target_exceeded=(
                not result.attempts[-1].validation.checks["size_within_limit"]
                if result.attempts
                else False
            ),
            error=None,
        )

    futures: dict[Future[BatchItemResult], FilePathPlan] = {}
    interrupted = False
    with ThreadPoolExecutor(max_workers=worker_plan.workers, thread_name_prefix="adt-video") as executor:
        for item in pending:
            futures[executor.submit(process, item)] = item
        try:
            for future in as_completed(futures):
                result = future.result()
                item_results[str(result.source)] = result
        except KeyboardInterrupt:
            interrupted = True
            cancellation.set()
        finally:
            for future, item in futures.items():
                if str(item.source) in item_results:
                    continue
                if future.cancelled():
                    result = _cancelled_item(item)
                else:
                    try:
                        result = future.result()
                    except Exception as exc:  # defensive: process normally converts errors
                        result = BatchItemResult(
                            source=item.source,
                            output=item.output,
                            status="failed",
                            original_bytes=item.source.stat().st_size,
                            output_bytes=0,
                            reduction_percent=None,
                            attempts=0,
                            quality_score=None,
                            target_exceeded=False,
                            error={"type": type(exc).__name__, "message": str(exc)},
                        )
                item_results[str(result.source)] = result

    if cancellation.is_set():
        interrupted = True
    ordered_items = [item_results[str(item.source)] for item in plan.items]
    summary = _summary(ordered_items)
    if interrupted:
        exit_code = int(ExitCode.INTERRUPTED)
        final_status = "interrupted"
    elif summary.failed:
        exit_code = int(ExitCode.PARTIAL_SUCCESS)
        final_status = "partial"
    else:
        exit_code = int(ExitCode.SUCCESS)
        final_status = "completed"
    store.update_job_status(final_status)

    result = BatchRunResult(
        job_id=job_id,
        ok=exit_code == int(ExitCode.SUCCESS),
        exit_code=exit_code,
        interrupted=interrupted,
        summary=summary,
        items=tuple(ordered_items),
        resources=resources,
        worker_plan=worker_plan,
        state_path=state_file,
        json_report_path=json_report,
        csv_report_path=csv_report,
    )
    _atomic_write_json(json_report, result.to_result_document())
    _atomic_write_text(csv_report, _csv_report(ordered_items))
    notify(
        "job_interrupted" if interrupted else "job_completed",
        {
            "ok": result.ok,
            "exit_code": result.exit_code,
            "summary": result.summary.to_dict(),
            "json_report": str(json_report),
            "csv_report": str(csv_report),
        },
    )
    return result
