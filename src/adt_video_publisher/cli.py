"""Non-interactive command line interface for people, agents, and CI."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import TextIO

from . import __version__
from .batch import BatchRunResult, load_resume_request, run_batch
from .compression import (
    DEFAULT_CRF,
    DEFAULT_MAXIMUM_ATTEMPTS,
    DEFAULT_MINIMUM_SSIM,
    DEFAULT_PRESET,
    DEFAULT_STRICT_SIZE,
    validate_candidate,
)
from .contracts import (
    CONTRACT_SCHEMA_VERSION,
    SCHEMA_FILES,
    TOOL_NAME,
    ExitCode,
    contract_document,
    schema_resource,
)
from .errors import (
    AdtVideoError,
    EncodingFailedError,
    InvalidInputError,
    ProbeFailedError,
    ProbeUnavailableError,
    PublishFailedError,
    PublishingInterruptedError,
    ResourceLimitError,
    UnsafePathError,
    ValidationFailedError,
)
from .media import probe_media
from .paths import build_path_plan, discover_videos, resolve_source
from .planning import DEFAULT_MAXIMUM_BYTES
from .publishing import PublishResult, publish_adt
from .resources import detect_resources, select_worker_plan

PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow")


class UsageError(Exception):
    """Raised instead of allowing argparse to terminate on invalid input."""


class ContractArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise UsageError(message)


class ProgressEmitter:
    """Write schema-valid, thread-safe NDJSON progress events to one stream."""

    def __init__(self, stream: TextIO, *, enabled: bool) -> None:
        self.stream = stream
        self.enabled = enabled
        self._sequence = 0
        self._lock = threading.Lock()

    def emit(self, job_id: str, event: str, payload: dict[str, object]) -> None:
        if not self.enabled:
            return
        with self._lock:
            document = {
                "schema_version": CONTRACT_SCHEMA_VERSION,
                "job_id": job_id,
                "sequence": self._sequence,
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "event": event,
                "payload": payload,
            }
            self._sequence += 1
            try:
                self.stream.write(json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n")
                self.stream.flush()
            except OSError:
                self.enabled = False


def _workers_argument(value: str) -> str | int:
    if value == "auto":
        return value
    try:
        workers = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be 'auto' or a positive integer") from exc
    if workers < 1:
        raise argparse.ArgumentTypeError("must be 'auto' or a positive integer")
    return workers


def _add_input_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", required=True, help="Video file or directory to process.")
    parser.add_argument("--recursive", action="store_true", help="Include nested video directories.")
    parser.add_argument("--ffmpeg", help="Explicit FFmpeg executable path.")
    parser.add_argument("--probe", help="Explicit FFprobe or FFmpeg executable path for inspection.")


def _add_batch_flags(parser: argparse.ArgumentParser) -> None:
    _add_input_flags(parser)
    parser.add_argument("--output", help="Separate destination directory for compressed copies.")
    parser.add_argument("--workers", type=_workers_argument, default="auto")
    parser.add_argument(
        "--maximum-bytes",
        type=int,
        default=DEFAULT_MAXIMUM_BYTES,
        help="Hard maximum output size per video (default: 5 MiB).",
    )
    parser.add_argument(
        "--attempts", type=int, choices=range(1, 9), default=DEFAULT_MAXIMUM_ATTEMPTS
    )
    parser.add_argument("--preset", choices=PRESETS, default=DEFAULT_PRESET)
    parser.add_argument(
        "--crf",
        type=int,
        choices=range(0, 52),
        default=DEFAULT_CRF,
        help=f"H.264 constant-quality value; lower is higher quality (default: {DEFAULT_CRF}).",
    )
    parser.add_argument(
        "--minimum-ssim",
        type=float,
        default=DEFAULT_MINIMUM_SSIM,
        help="Minimum source/output SSIM quality score (default: 0.95).",
    )
    size_group = parser.add_mutually_exclusive_group()
    size_group.add_argument(
        "--strict-size",
        dest="strict_size",
        action="store_true",
        default=DEFAULT_STRICT_SIZE,
        help="Enforce the byte limit (default).",
    )
    size_group.add_argument(
        "--soft-size",
        dest="strict_size",
        action="store_false",
        help="Allow outputs above the size target for a quality-first workflow.",
    )
    scale_group = parser.add_mutually_exclusive_group()
    scale_group.add_argument(
        "--adaptive-scale",
        dest="adaptive_scale",
        action="store_true",
        default=True,
        help="Reduce dimensions proportionately only when required to meet the limit (default).",
    )
    scale_group.add_argument(
        "--no-adaptive-scale",
        dest="adaptive_scale",
        action="store_false",
        help="Never reduce video dimensions.",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = ContractArgumentParser(
        prog=TOOL_NAME,
        description="Compress sign-language videos and publish them into ADT websites.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("contract", help="Print the stable automation contract.")

    schema_parser = subparsers.add_parser("schema", help="Print a versioned JSON schema.")
    schema_parser.add_argument("name", choices=tuple(SCHEMA_FILES))

    inspect_parser = subparsers.add_parser("inspect", help="Read media metadata without changing files.")
    _add_input_flags(inspect_parser)

    plan_parser = subparsers.add_parser("plan", help="Calculate safe output paths and worker limits.")
    _add_batch_flags(plan_parser)

    compress_parser = subparsers.add_parser("compress", help="Create silent compressed copies.")
    _add_batch_flags(compress_parser)
    compress_parser.add_argument("--state", help="Job state JSON path inside the output directory.")
    compress_parser.add_argument("--resume", action="store_true", help="Resume the selected state.")

    verify_parser = subparsers.add_parser("verify", help="Validate compressed videos without changing them.")
    _add_input_flags(verify_parser)
    verify_parser.add_argument("--maximum-bytes", type=int, default=DEFAULT_MAXIMUM_BYTES)
    verify_parser.add_argument("--strict-size", action="store_true")

    resume_parser = subparsers.add_parser("resume", help="Resume a job from its durable state JSON.")
    resume_parser.add_argument("--job", required=True, help="Path to .adt-video-job.json.")
    resume_parser.add_argument("--workers", type=_workers_argument, default="auto")
    resume_parser.add_argument("--ffmpeg", help="Explicit FFmpeg executable path.")
    resume_parser.add_argument("--probe", help="Explicit FFprobe or FFmpeg executable path.")

    publish_parser = subparsers.add_parser(
        "publish", help="Publish compressed videos into an ADT website."
    )
    publish_parser.add_argument("--input", required=True, help="Directory of compressed page_N.mp4 videos.")
    publish_parser.add_argument("--book", required=True, help="Source ADT website directory.")
    publish_destination = publish_parser.add_mutually_exclusive_group(required=True)
    publish_destination.add_argument("--output", help="New directory for a published ADT website copy.")
    publish_destination.add_argument(
        "--in-place",
        action="store_true",
        help="Update the selected ADT website itself; no ZIP package is created or modified.",
    )
    publish_parser.add_argument("--package", help="Optional deployment ZIP output; a .sha256 file is also created.")
    publish_parser.add_argument("--language", help="ADT language code; defaults to config.json.")
    publish_parser.add_argument("--recursive", action="store_true", help="Find page videos in nested folders.")
    publish_parser.add_argument("--maximum-bytes", type=int, default=DEFAULT_MAXIMUM_BYTES)
    publish_parser.add_argument("--probe", help="Explicit FFprobe or FFmpeg executable path.")

    ui_parser = subparsers.add_parser("ui", help="Open the optional desktop interface.")
    ui_parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Build the interface invisibly, verify Tk, and exit.",
    )
    return parser


def _extract_global_flags(arguments: list[str]) -> tuple[list[str], bool, str]:
    """Allow final-output and progress flags before or after a subcommand."""

    json_output = False
    progress = "none"
    cleaned: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--json":
            json_output = True
        elif argument.startswith("--progress="):
            progress = argument.split("=", 1)[1]
        elif argument == "--progress":
            index += 1
            if index >= len(arguments):
                raise UsageError("argument --progress: expected one argument")
            progress = arguments[index]
        else:
            cleaned.append(argument)
        index += 1
    if progress not in {"none", "ndjson"}:
        raise UsageError("argument --progress: choose from 'none', 'ndjson'")
    return cleaned, json_output, progress


def _write_json(stream: TextIO, payload: object) -> None:
    json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
    stream.write("\n")


def _error_payload(code: ExitCode, message: str, error_type: str) -> dict[str, object]:
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "ok": False,
        "error": {
            "code": int(code),
            "name": code.name,
            "type": error_type,
            "message": message,
        },
    }


def _error_exit_code(error: BaseException) -> ExitCode:
    if isinstance(error, UnsafePathError):
        return ExitCode.UNSAFE_PATH
    if isinstance(error, InvalidInputError):
        return ExitCode.INVALID_INPUT
    if isinstance(error, (ProbeUnavailableError, ProbeFailedError)):
        return ExitCode.PROBE_FAILED
    if isinstance(error, ResourceLimitError):
        return ExitCode.RESOURCE_LIMIT
    if isinstance(error, EncodingFailedError):
        return ExitCode.ENCODING_FAILED
    if isinstance(error, PublishFailedError):
        return ExitCode.PUBLISH_FAILED
    if isinstance(error, PublishingInterruptedError):
        return ExitCode.INTERRUPTED
    if isinstance(error, ValidationFailedError):
        return ExitCode.VALIDATION_FAILED
    if isinstance(error, KeyboardInterrupt):
        return ExitCode.INTERRUPTED
    return ExitCode.INTERNAL_ERROR


def _probe_path(options: argparse.Namespace) -> str | None:
    return options.probe or options.ffmpeg


def _inspect(options: argparse.Namespace, progress: ProgressEmitter) -> tuple[int, dict[str, object], str]:
    source = resolve_source(options.input)
    videos = discover_videos(source, recursive=options.recursive)
    job_id = uuid.uuid4().hex
    progress.emit(job_id, "job_started", {"command": "inspect", "total": len(videos)})
    items: list[dict[str, object]] = []
    failed = 0
    for video in videos:
        progress.emit(job_id, "item_started", {"source": str(video)})
        try:
            media = probe_media(video, probe_path=_probe_path(options))
        except (ProbeUnavailableError, ProbeFailedError) as exc:
            failed += 1
            error = {"type": type(exc).__name__, "message": str(exc)}
            items.append({"source": str(video), "status": "failed", "error": error})
            progress.emit(job_id, "item_failed", {"source": str(video), "error": error})
        else:
            items.append({"source": str(video), "status": "inspected", "media": media.to_dict()})
            progress.emit(
                job_id,
                "item_completed",
                {"source": str(video), "status": "inspected", "size_bytes": media.size_bytes},
            )
    exit_code = int(ExitCode.SUCCESS if not failed else ExitCode.PROBE_FAILED)
    document = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "job_id": job_id,
        "command": "inspect",
        "ok": failed == 0,
        "exit_code": exit_code,
        "summary": {"total": len(videos), "inspected": len(videos) - failed, "failed": failed},
        "items": items,
    }
    progress.emit(job_id, "job_completed", {"ok": failed == 0, "exit_code": exit_code})
    human = f"Inspected {len(videos) - failed}/{len(videos)} video(s); {failed} failed.\n"
    return exit_code, document, human


def _plan(options: argparse.Namespace, progress: ProgressEmitter) -> tuple[int, dict[str, object], str]:
    plan = build_path_plan(options.input, output=options.output, recursive=options.recursive)
    resources = detect_resources(plan.output_root, ffmpeg_path=options.ffmpeg)
    workers = select_worker_plan(
        resources,
        item_count=len(plan.items),
        requested_workers=options.workers,
        maximum_bytes=options.maximum_bytes,
    )
    job_id = uuid.uuid4().hex
    document = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "job_id": job_id,
        "source": str(plan.source),
        "output": str(plan.output_root),
        "settings": {
            "maximum_bytes": options.maximum_bytes,
            "encoder": "libx264",
            "quality_mode": (
                "hard-limit-adaptive" if options.strict_size and options.adaptive_scale
                else "constant-quality"
            ),
            "crf": options.crf,
            "minimum_ssim": options.minimum_ssim,
            "strict_size": options.strict_size,
            "adaptive_scale": options.adaptive_scale,
            "workers": workers.workers,
            "resume": False,
        },
        "resources": resources.to_contract_dict(),
        "items": [
            {"source": str(item.source), "output": str(item.output), "status": "ready"}
            for item in plan.items
        ],
    }
    progress.emit(
        job_id,
        "job_planned",
        {"total": len(plan.items), "workers": workers.workers, "output": str(plan.output_root)},
    )
    human = (
        f"Planned {len(plan.items)} video(s) to '{plan.output_root}' with "
        f"{workers.workers} worker(s).\n"
    )
    return int(ExitCode.SUCCESS), document, human


def _verify(options: argparse.Namespace, progress: ProgressEmitter) -> tuple[int, dict[str, object], str]:
    source = resolve_source(options.input)
    videos = discover_videos(source, recursive=options.recursive)
    job_id = uuid.uuid4().hex
    progress.emit(job_id, "job_started", {"command": "verify", "total": len(videos)})
    items: list[dict[str, object]] = []
    valid_count = 0
    for video in videos:
        progress.emit(job_id, "item_started", {"source": str(video)})
        try:
            media = probe_media(video, probe_path=_probe_path(options))
            report = validate_candidate(
                media=media,
                source_duration_seconds=media.duration_seconds,
                maximum_bytes=options.maximum_bytes,
                strict_size=options.strict_size,
            )
        except (ProbeUnavailableError, ProbeFailedError) as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            items.append({"source": str(video), "status": "failed", "error": error})
            progress.emit(job_id, "item_failed", {"source": str(video), "error": error})
            continue
        status = "valid" if report.valid else "invalid"
        valid_count += int(report.valid)
        items.append(
            {
                "source": str(video),
                "status": status,
                "size_bytes": media.size_bytes,
                "checks": report.checks,
                "errors": list(report.errors),
            }
        )
        event = "item_completed" if report.valid else "item_failed"
        progress.emit(job_id, event, {"source": str(video), "status": status, "errors": list(report.errors)})
    invalid_count = len(videos) - valid_count
    exit_code = int(ExitCode.SUCCESS if not invalid_count else ExitCode.VALIDATION_FAILED)
    document = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "job_id": job_id,
        "command": "verify",
        "ok": invalid_count == 0,
        "exit_code": exit_code,
        "summary": {"total": len(videos), "valid": valid_count, "invalid": invalid_count},
        "items": items,
    }
    progress.emit(job_id, "job_completed", {"ok": invalid_count == 0, "exit_code": exit_code})
    human = f"Verified {valid_count}/{len(videos)} video(s); {invalid_count} invalid.\n"
    return exit_code, document, human


def _batch_result(result: BatchRunResult) -> tuple[int, dict[str, object], str]:
    summary = result.summary
    if getattr(result, "job_details_removed", False):
        human = (
            f"Completed 0, skipped 0, failed {summary.failed} of {summary.total} video(s). "
            "No output was produced, so temporary job details were removed.\n"
        )
    else:
        human = (
            f"Completed {summary.completed}, skipped {summary.skipped}, failed {summary.failed} "
            f"of {summary.total} video(s). Reports: '{result.json_report_path}', "
            f"'{result.csv_report_path}'.\n"
        )
    return result.exit_code, result.to_result_document(), human


def _compress(options: argparse.Namespace, progress: ProgressEmitter) -> tuple[int, dict[str, object], str]:
    result = run_batch(
        options.input,
        output=options.output,
        recursive=options.recursive,
        requested_workers=options.workers,
        maximum_bytes=options.maximum_bytes,
        maximum_attempts=options.attempts,
        preset=options.preset,
        crf=options.crf,
        minimum_ssim=options.minimum_ssim,
        strict_size=options.strict_size,
        adaptive_scale=options.adaptive_scale,
        ffmpeg_path=options.ffmpeg,
        probe_path=options.probe,
        resume=options.resume,
        state_path=options.state,
        progress_callback=progress.emit if progress.enabled else None,
    )
    return _batch_result(result)


def _resume(options: argparse.Namespace, progress: ProgressEmitter) -> tuple[int, dict[str, object], str]:
    request = load_resume_request(options.job)
    result = run_batch(
        request.source,
        output=request.output,
        recursive=request.recursive,
        requested_workers=options.workers,
        maximum_bytes=request.maximum_bytes,
        maximum_attempts=request.maximum_attempts,
        preset=request.preset,
        crf=request.crf,
        minimum_ssim=request.minimum_ssim,
        strict_size=request.strict_size,
        adaptive_scale=request.adaptive_scale,
        ffmpeg_path=options.ffmpeg,
        probe_path=options.probe,
        resume=True,
        state_path=request.state_path,
        progress_callback=progress.emit if progress.enabled else None,
    )
    return _batch_result(result)


def _publish(options: argparse.Namespace, progress: ProgressEmitter) -> tuple[int, dict[str, object], str]:
    try:
        result: PublishResult = publish_adt(
            options.input,
            book=options.book,
            output=options.output,
            package=options.package,
            in_place=options.in_place,
            language=options.language,
            recursive=options.recursive,
            maximum_bytes=options.maximum_bytes,
            probe_path=options.probe,
            progress_callback=progress.emit if progress.enabled else None,
        )
    except OSError as exc:
        raise PublishFailedError(f"ADT publishing failed: {exc}") from exc
    package_text = f" Package: '{result.package.path}'." if result.package else ""
    human = (
        f"Published {len(result.videos)} video(s) into '{result.output_book}' "
        f"for {result.language}; bundle version {result.bundle_version}.{package_text}\n"
    )
    return int(ExitCode.SUCCESS), result.to_result_document(), human


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Execute the CLI and return a stable integer exit code."""

    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    raw_arguments = list(argv if argv is not None else sys.argv[1:])
    try:
        arguments, json_output, progress_mode = _extract_global_flags(raw_arguments)
        options = _build_parser().parse_args(arguments)
    except UsageError as exc:
        if "--json" in raw_arguments:
            _write_json(output, _error_payload(ExitCode.USAGE_ERROR, str(exc), "UsageError"))
        else:
            errors.write(f"{TOOL_NAME}: error: {exc}\n")
            errors.write(f"Try '{TOOL_NAME} --help' for usage.\n")
        return int(ExitCode.USAGE_ERROR)

    progress = ProgressEmitter(errors, enabled=progress_mode == "ndjson")
    try:
        if options.command == "contract":
            document = contract_document()
            if json_output:
                _write_json(output, document)
            else:
                output.write(
                    f"{TOOL_NAME} {__version__} automation contract v{CONTRACT_SCHEMA_VERSION}\n"
                )
                for command in document["commands"]:
                    output.write(
                        f"  {command['name']:<10} {command['status']:<9} {command['purpose']}\n"
                    )
            return int(ExitCode.SUCCESS)

        if options.command == "schema":
            with schema_resource(options.name).open("r", encoding="utf-8") as schema_file:
                document = json.load(schema_file)
            _write_json(output, document)
            return int(ExitCode.SUCCESS)

        if options.command == "ui":
            if json_output or progress.enabled:
                raise InvalidInputError("The interactive UI does not use JSON or progress-stream flags.")
            from .desktop import run as run_desktop, smoke_test as smoke_test_desktop

            if options.smoke_test:
                return smoke_test_desktop()
            return run_desktop()

        operations = {
            "inspect": _inspect,
            "plan": _plan,
            "compress": _compress,
            "verify": _verify,
            "resume": _resume,
            "publish": _publish,
        }
        exit_code, document, human = operations[options.command](options, progress)
        if json_output:
            _write_json(output, document)
        else:
            output.write(human)
        return exit_code
    except (AdtVideoError, OSError, KeyboardInterrupt) as exc:
        code = _error_exit_code(exc)
        if json_output:
            _write_json(output, _error_payload(code, str(exc), type(exc).__name__))
        else:
            errors.write(f"{TOOL_NAME}: {code.name}: {exc}\n")
        return int(code)
    except Exception as exc:
        code = ExitCode.INTERNAL_ERROR
        if json_output:
            _write_json(output, _error_payload(code, str(exc), type(exc).__name__))
        else:
            errors.write(f"{TOOL_NAME}: {code.name}: {exc}\n")
        return int(code)


def run() -> int:
    """Console-script entry point."""

    return main()
