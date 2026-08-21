"""Silent video compression, validation, retry, and atomic publishing."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, Sequence

from .errors import (
    EncodingFailedError,
    InvalidInputError,
    UnsafePathError,
    ValidationFailedError,
)
from .media import MediaInfo, probe_media
from .planning import DEFAULT_MAXIMUM_BYTES, EncodingPlan, calculate_encoding_plan
from .processes import hidden_process_options

DEFAULT_PRESET = "medium"
DEFAULT_CRF = 35
DEFAULT_MINIMUM_SSIM = 0.95
DEFAULT_MAXIMUM_ATTEMPTS = 8
DEFAULT_STRICT_SIZE = True
SIZE_RETRY_SAFETY_RATIO = 0.94
MINIMUM_RETRY_SCALE_FACTOR = 0.50
MAXIMUM_RETRY_SCALE_FACTOR = 0.90

CommandRunner = Callable[[Sequence[str], float | None], subprocess.CompletedProcess[str]]
ProbeFunction = Callable[..., MediaInfo]
QualityFunction = Callable[..., float]
EncodingProgressFunction = Callable[[str, int, float], None]


@dataclass(frozen=True, slots=True)
class SourceFingerprint:
    size_bytes: int
    modified_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ValidationReport:
    valid: bool
    checks: dict[str, bool]
    errors: tuple[str, ...]
    media: MediaInfo
    quality_score: float | None = None
    minimum_quality_score: float | None = None
    size_limit_enforced: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "checks": dict(self.checks),
            "errors": list(self.errors),
            "media": self.media.to_dict(),
            "quality_score": self.quality_score,
            "minimum_quality_score": self.minimum_quality_score,
            "size_limit_enforced": self.size_limit_enforced,
        }


@dataclass(frozen=True, slots=True)
class EncodingAttempt:
    number: int
    action: str
    target_video_bitrate: int | None
    crf: int | None
    scale_factor: float
    output_size_bytes: int
    validation: ValidationReport

    def to_dict(self) -> dict[str, object]:
        return {
            "number": self.number,
            "action": self.action,
            "target_video_bitrate": self.target_video_bitrate,
            "crf": self.crf,
            "scale_factor": self.scale_factor,
            "output_size_bytes": self.output_size_bytes,
            "validation": self.validation.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class CompressionResult:
    source: Path
    output: Path
    source_size_bytes: int
    output_size_bytes: int
    reduction_percent: float
    source_sha256: str
    output_sha256: str
    attempts: tuple[EncodingAttempt, ...]

    def to_dict(self) -> dict[str, object]:
        document = asdict(self)
        document["source"] = str(self.source)
        document["output"] = str(self.output)
        document["attempts"] = [attempt.to_dict() for attempt in self.attempts]
        return document


def _platform_tag() -> str:
    system = platform.system().lower()
    system = {"darwin": "macos", "windows": "windows", "linux": "linux"}.get(system, system)
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(
        machine, machine
    )
    return f"{system}-{machine}"


def resolve_ffmpeg(explicit_path: str | os.PathLike[str] | None = None) -> Path:
    """Find an encoder supplied explicitly, by environment, bundled, or on PATH."""

    executable_suffix = ".exe" if os.name == "nt" else ""
    package_bin = Path(__file__).resolve().parent / "bin" / _platform_tag()
    candidates = (
        explicit_path,
        os.environ.get("ADT_VIDEO_FFMPEG"),
        package_bin / f"ffmpeg{executable_suffix}",
        shutil.which("ffmpeg"),
    )
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser().resolve()
        if path.is_file() and "ffprobe" not in path.name.lower():
            return path
    raise EncodingFailedError(
        "FFmpeg was not found. Supply an explicit path, set ADT_VIDEO_FFMPEG, "
        "or use a packaged release."
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_source(path: Path) -> SourceFingerprint:
    stat = path.stat()
    return SourceFingerprint(
        size_bytes=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        sha256=_sha256(path),
    )


def _validate_paths(source: str | os.PathLike[str], output: str | os.PathLike[str]) -> tuple[Path, Path]:
    source_path = Path(source).expanduser().resolve()
    output_path = Path(output).expanduser().resolve(strict=False)
    if not source_path.is_file():
        raise InvalidInputError(f"Video file does not exist: '{source_path}'.")
    if source_path.stat().st_size <= 0:
        raise InvalidInputError(f"Video file is empty: '{source_path}'.")
    if output_path.suffix.lower() != ".mp4":
        raise InvalidInputError("Compressed output must use the .mp4 extension.")
    if os.path.normcase(str(source_path)) == os.path.normcase(str(output_path)):
        raise UnsafePathError("The compressed output cannot replace its source video.")
    if os.path.normcase(str(source_path.parent)) == os.path.normcase(str(output_path.parent)):
        raise UnsafePathError("The compressed output must be stored outside the source directory.")
    return source_path, output_path


def _run_ffmpeg(command: Sequence[str], timeout_seconds: float | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
        **hidden_process_options(),
    )


def build_ffmpeg_command(
    *,
    ffmpeg: Path,
    source: Path,
    candidate: Path,
    action: str,
    target_video_bitrate: int | None,
    preset: str,
    crf: int = DEFAULT_CRF,
    scale_factor: float = 1.0,
    encoder_threads: int | None = None,
    progress_pipe: bool = False,
) -> tuple[str, ...]:
    """Build a deterministic command that maps only the primary video stream."""

    command = [
        str(ffmpeg),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
    ]
    if action == "remux":
        command.extend(["-c:v", "copy"])
    else:
        if not 0 <= crf <= 51:
            raise InvalidInputError("CRF must be between 0 and 51.")
        if not 0 < scale_factor <= 1:
            raise InvalidInputError("Scale factor must be above 0 and no greater than 1.")
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                preset,
                "-crf",
                str(crf),
                "-pix_fmt",
                "yuv420p",
            ]
        )
        if scale_factor < 1:
            scale_text = f"{scale_factor:.6f}".rstrip("0").rstrip(".")
            command.extend(
                [
                    "-vf",
                    f"scale=trunc(iw*{scale_text}/2)*2:trunc(ih*{scale_text}/2)*2",
                ]
            )
        if target_video_bitrate is not None:
            if target_video_bitrate <= 0:
                raise InvalidInputError("Target video bitrate must be positive when supplied.")
            command.extend(
                [
                    "-maxrate",
                    str(target_video_bitrate),
                    "-bufsize",
                    str(target_video_bitrate * 2),
                ]
            )
        if encoder_threads is not None:
            if encoder_threads < 1:
                raise InvalidInputError("Encoder thread count must be positive.")
            command.extend(["-threads", str(encoder_threads)])
    if progress_pipe:
        command.extend(["-progress", "pipe:1", "-nostats"])
    command.extend(["-movflags", "+faststart", "-f", "mp4", str(candidate)])
    return tuple(command)


def _run_ffmpeg_with_progress(
    command: Sequence[str],
    timeout_seconds: float | None,
    duration_seconds: float,
    callback: Callable[[float], None],
) -> subprocess.CompletedProcess[str]:
    """Run FFmpeg while translating its machine progress stream to percentages."""

    process = subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **hidden_process_options(),
    )
    stdout_lines: list[str] = []
    started = time.monotonic()
    try:
        assert process.stdout is not None
        while True:
            line = process.stdout.readline()
            if line:
                stdout_lines.append(line)
                key, separator, value = line.strip().partition("=")
                if separator and key == "out_time_us" and duration_seconds > 0:
                    try:
                        percent = float(value) / (duration_seconds * 1_000_000) * 100
                    except ValueError:
                        pass
                    else:
                        callback(max(0.0, min(99.9, percent)))
                elif separator and key == "progress" and value == "end":
                    callback(100.0)
            if process.poll() is not None:
                break
            if timeout_seconds is not None and time.monotonic() - started > timeout_seconds:
                process.kill()
                raise subprocess.TimeoutExpired(command, timeout_seconds)
        stderr = process.stderr.read() if process.stderr is not None else ""
        return subprocess.CompletedProcess(
            list(command), process.returncode, "".join(stdout_lines), stderr
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def validate_candidate(
    *,
    media: MediaInfo,
    source_duration_seconds: float,
    maximum_bytes: int,
    strict_size: bool = False,
    quality_score: float | None = None,
    minimum_quality_score: float | None = None,
    source_width: int | None = None,
    source_height: int | None = None,
    source_frames_per_second: float | None = None,
    allow_dimension_reduction: bool = False,
) -> ValidationReport:
    duration_tolerance = max(1.0, source_duration_seconds * 0.02)
    checks = {
        "size_within_limit": 0 < media.size_bytes <= maximum_bytes,
        "one_video_stream": media.video_stream_count == 1,
        "audio_removed": media.audio_stream_count == 0,
        "browser_video_codec": media.primary_video.codec == "h264",
        "browser_pixel_format": media.primary_video.pixel_format == "yuv420p",
        "duration_preserved": abs(media.duration_seconds - source_duration_seconds)
        <= duration_tolerance,
        "dimensions_preserved": (
            source_width is None
            or source_height is None
            or (
                media.primary_video.width == source_width
                and media.primary_video.height == source_height
            )
            or (
                allow_dimension_reduction
                and 0 < media.primary_video.width <= source_width
                and 0 < media.primary_video.height <= source_height
                and abs(
                    media.primary_video.width / media.primary_video.height
                    - source_width / source_height
                )
                <= 0.02
            )
        ),
        "frame_rate_preserved": (
            source_frames_per_second is None
            or media.primary_video.frames_per_second is None
            or abs(media.primary_video.frames_per_second - source_frames_per_second)
            <= max(0.01, source_frames_per_second * 0.001)
        ),
        "quality_preserved": minimum_quality_score is None
        or (quality_score is not None and quality_score >= minimum_quality_score),
    }
    messages = {
        "size_within_limit": f"Output exceeds the {maximum_bytes}-byte limit.",
        "one_video_stream": "Output must contain exactly one video stream.",
        "audio_removed": "Output still contains an audio stream.",
        "browser_video_codec": "Output video codec is not H.264.",
        "browser_pixel_format": "Output pixel format is not yuv420p.",
        "duration_preserved": "Output duration differs materially from the source.",
        "dimensions_preserved": "Output dimensions differ from the source.",
        "frame_rate_preserved": "Output frame rate differs from the source.",
        "quality_preserved": (
            f"Output quality score is below the required {minimum_quality_score:.3f}."
            if minimum_quality_score is not None
            else "Output quality could not be validated."
        ),
    }
    required_checks = set(checks)
    if not strict_size:
        required_checks.remove("size_within_limit")
    errors = tuple(
        messages[name]
        for name, passed in checks.items()
        if name in required_checks and not passed
    )
    return ValidationReport(
        valid=not errors,
        checks=checks,
        errors=errors,
        media=media,
        quality_score=quality_score,
        minimum_quality_score=minimum_quality_score,
        size_limit_enforced=strict_size,
    )


_SSIM_PATTERN = re.compile(r"\bAll:([0-9]+(?:\.[0-9]+)?)")


def measure_ssim(
    ffmpeg: Path,
    source: Path,
    candidate: Path,
    timeout_seconds: float | None,
    candidate_width: int | None = None,
    candidate_height: int | None = None,
) -> float:
    """Compare every decoded output frame with the source using FFmpeg SSIM."""

    reference_filter = "[0:v:0]setpts=PTS-STARTPTS"
    if candidate_width is not None and candidate_height is not None:
        reference_filter += f",scale={candidate_width}:{candidate_height}"
    reference_filter += "[reference];"
    command = (
        str(ffmpeg),
        "-nostdin",
        "-hide_banner",
        "-y",
        "-i",
        str(source),
        "-i",
        str(candidate),
        "-filter_complex",
        reference_filter
        + "[1:v:0]setpts=PTS-STARTPTS[compressed];"
        + "[reference][compressed]ssim",
        "-an",
        "-sn",
        "-dn",
        "-f",
        "null",
        "-",
    )
    result = _run_ffmpeg(command, timeout_seconds)
    match = _SSIM_PATTERN.search("\n".join((result.stdout or "", result.stderr or "")))
    if result.returncode != 0 or match is None:
        detail = (result.stderr or result.stdout or "SSIM result was not reported").strip()[-1200:]
        raise ValidationFailedError(f"Could not validate visual quality: {detail}")
    return float(match.group(1))


def _publish_atomic(candidate: Path, output: Path, *, replace_existing: bool) -> None:
    if replace_existing:
        os.replace(candidate, output)
        return
    try:
        os.link(candidate, output)
    except FileExistsError as exc:
        raise UnsafePathError(f"Output already exists: '{output}'.") from exc
    except OSError as exc:
        raise EncodingFailedError(
            f"The destination filesystem cannot publish the output atomically: '{output}'."
        ) from exc
    candidate.unlink()


def compress_video(
    source: str | os.PathLike[str],
    output: str | os.PathLike[str],
    *,
    ffmpeg_path: str | os.PathLike[str] | None = None,
    probe_path: str | os.PathLike[str] | None = None,
    maximum_bytes: int = DEFAULT_MAXIMUM_BYTES,
    maximum_attempts: int = DEFAULT_MAXIMUM_ATTEMPTS,
    preset: str = DEFAULT_PRESET,
    crf: int = DEFAULT_CRF,
    minimum_ssim: float = DEFAULT_MINIMUM_SSIM,
    strict_size: bool = DEFAULT_STRICT_SIZE,
    adaptive_scale: bool = True,
    replace_existing: bool = False,
    timeout_seconds: float | None = None,
    encoder_threads: int | None = None,
    command_runner: CommandRunner = _run_ffmpeg,
    probe_function: ProbeFunction = probe_media,
    quality_function: QualityFunction = measure_ssim,
    progress_callback: EncodingProgressFunction | None = None,
) -> CompressionResult:
    """Create a validated silent MP4 while leaving the source immutable."""

    if not 1 <= maximum_attempts <= 8:
        raise InvalidInputError("Maximum attempts must be between 1 and 8.")
    if not 0 <= crf <= 51:
        raise InvalidInputError("CRF must be between 0 and 51.")
    if not 0.0 < minimum_ssim <= 1.0:
        raise InvalidInputError("Minimum SSIM must be above 0 and no greater than 1.")
    source_path, output_path = _validate_paths(source, output)
    if output_path.exists() and not replace_existing:
        raise UnsafePathError(f"Output already exists: '{output_path}'.")

    ffmpeg = resolve_ffmpeg(ffmpeg_path)
    effective_probe_path = probe_path or ffmpeg
    source_fingerprint = fingerprint_source(source_path)
    source_media = probe_function(source_path, probe_path=effective_probe_path)
    plan: EncodingPlan = calculate_encoding_plan(source_media, maximum_bytes=maximum_bytes)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    candidate = output_path.parent / f".{output_path.stem}.{uuid.uuid4().hex}.part.mp4"
    attempts: list[EncodingAttempt] = []
    action = plan.action
    target_bitrate = None
    current_crf = crf
    current_scale = 1.0

    try:
        for attempt_number in range(1, maximum_attempts + 1):
            command = build_ffmpeg_command(
                ffmpeg=ffmpeg,
                source=source_path,
                candidate=candidate,
                action=action,
                target_video_bitrate=target_bitrate,
                preset=preset,
                crf=current_crf,
                scale_factor=current_scale,
                encoder_threads=encoder_threads,
                progress_pipe=progress_callback is not None and command_runner is _run_ffmpeg,
            )
            if progress_callback is not None:
                progress_callback("encoding", attempt_number, 0.0)
            if progress_callback is not None and command_runner is _run_ffmpeg:
                result = _run_ffmpeg_with_progress(
                    command,
                    timeout_seconds,
                    source_media.duration_seconds,
                    lambda percent: progress_callback("encoding", attempt_number, percent),
                )
            else:
                result = command_runner(command, timeout_seconds)
            if result.returncode != 0 or not candidate.is_file() or candidate.stat().st_size <= 0:
                detail = (result.stderr or result.stdout or "unknown FFmpeg error").strip()[-1200:]
                raise EncodingFailedError(f"FFmpeg failed for '{source_path}': {detail}")

            candidate_media = probe_function(candidate, probe_path=effective_probe_path)
            candidate_is_oversized = candidate_media.size_bytes > maximum_bytes
            should_measure_quality = not (strict_size and candidate_is_oversized)
            quality_score = None
            if action == "remux":
                quality_score = 1.0
            elif should_measure_quality:
                if progress_callback is not None:
                    progress_callback("validating", attempt_number, 0.0)
                quality_score = quality_function(
                    ffmpeg,
                    source_path,
                    candidate,
                    timeout_seconds,
                    candidate_media.primary_video.width,
                    candidate_media.primary_video.height,
                )
                if progress_callback is not None:
                    progress_callback("validating", attempt_number, 100.0)
            validation = validate_candidate(
                media=candidate_media,
                source_duration_seconds=source_media.duration_seconds,
                maximum_bytes=maximum_bytes,
                strict_size=strict_size,
                quality_score=quality_score,
                minimum_quality_score=minimum_ssim if should_measure_quality else None,
                source_width=source_media.primary_video.width,
                source_height=source_media.primary_video.height,
                source_frames_per_second=source_media.primary_video.frames_per_second,
                allow_dimension_reduction=adaptive_scale,
            )
            attempts.append(
                EncodingAttempt(
                    number=attempt_number,
                    action=action,
                    target_video_bitrate=target_bitrate,
                    crf=None if action == "remux" else current_crf,
                    scale_factor=current_scale,
                    output_size_bytes=candidate.stat().st_size,
                    validation=validation,
                )
            )
            if validation.valid:
                break

            failed_checks = {
                name
                for name, passed in validation.checks.items()
                if not passed and (strict_size or name != "size_within_limit")
            }
            if attempt_number == maximum_attempts:
                raise ValidationFailedError(" ".join(validation.errors))
            if failed_checks == {"size_within_limit"} and adaptive_scale:
                size_ratio = maximum_bytes * SIZE_RETRY_SAFETY_RATIO / candidate.stat().st_size
                retry_factor = max(
                    MINIMUM_RETRY_SCALE_FACTOR,
                    min(MAXIMUM_RETRY_SCALE_FACTOR, size_ratio ** 0.5),
                )
                current_scale *= retry_factor
            elif "quality_preserved" in failed_checks:
                current_crf = max(18, current_crf - 2)
            else:
                raise ValidationFailedError(" ".join(validation.errors))
            action = "encode"
            candidate.unlink()
        else:  # pragma: no cover - loop always exits through break or an exception
            raise ValidationFailedError("No valid output was produced.")

        if fingerprint_source(source_path) != source_fingerprint:
            raise UnsafePathError(f"Source changed during compression: '{source_path}'.")
        _publish_atomic(candidate, output_path, replace_existing=replace_existing)
        final_attempt = attempts[-1]
        attempts[-1] = replace(
            final_attempt,
            validation=replace(
                final_attempt.validation,
                media=replace(final_attempt.validation.media, path=output_path),
            ),
        )
        output_size = output_path.stat().st_size
        return CompressionResult(
            source=source_path,
            output=output_path,
            source_size_bytes=source_fingerprint.size_bytes,
            output_size_bytes=output_size,
            reduction_percent=round(
                (1 - output_size / source_fingerprint.size_bytes) * 100,
                2,
            ),
            source_sha256=source_fingerprint.sha256,
            output_sha256=_sha256(output_path),
            attempts=tuple(attempts),
        )
    finally:
        if candidate.is_file():
            candidate.unlink()
