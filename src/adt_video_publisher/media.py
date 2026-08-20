"""Cross-platform video metadata probing through FFprobe or FFmpeg."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

from .errors import InvalidInputError, ProbeFailedError, ProbeUnavailableError

ProbeKind = Literal["ffprobe", "ffmpeg"]


@dataclass(frozen=True, slots=True)
class ProbeTool:
    path: Path
    kind: ProbeKind
    source: str


@dataclass(frozen=True, slots=True)
class VideoStreamInfo:
    codec: str
    width: int
    height: int
    pixel_format: str | None
    frames_per_second: float | None
    bitrate: int | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MediaInfo:
    path: Path
    size_bytes: int
    duration_seconds: float
    format_names: tuple[str, ...]
    video_stream_count: int
    audio_stream_count: int
    primary_video: VideoStreamInfo
    container_bitrate: int | None
    probe_kind: ProbeKind

    @property
    def has_audio(self) -> bool:
        return self.audio_stream_count > 0

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "size_bytes": self.size_bytes,
            "duration_seconds": self.duration_seconds,
            "format_names": list(self.format_names),
            "video_stream_count": self.video_stream_count,
            "audio_stream_count": self.audio_stream_count,
            "has_audio": self.has_audio,
            "primary_video": self.primary_video.to_dict(),
            "container_bitrate": self.container_bitrate,
            "probe_kind": self.probe_kind,
        }


def _platform_tag() -> str:
    system = platform.system().lower()
    system = {"darwin": "macos", "windows": "windows", "linux": "linux"}.get(system, system)
    machine = platform.machine().lower()
    machine = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "aarch64": "arm64",
    }.get(machine, machine)
    return f"{system}-{machine}"


def _tool_kind(path: Path) -> ProbeKind:
    return "ffprobe" if "ffprobe" in path.name.lower() else "ffmpeg"


def resolve_probe_tool(explicit_path: str | os.PathLike[str] | None = None) -> ProbeTool:
    """Resolve a probe binary without assuming any operating-system installation."""

    candidates: list[tuple[str | os.PathLike[str] | None, str]] = [
        (explicit_path, "explicit"),
        (os.environ.get("ADT_VIDEO_FFPROBE"), "ADT_VIDEO_FFPROBE"),
    ]

    executable_suffix = ".exe" if os.name == "nt" else ""
    package_bin = Path(__file__).resolve().parent / "bin" / _platform_tag()
    candidates.extend(
        [
            (package_bin / f"ffprobe{executable_suffix}", "bundled"),
            (shutil.which("ffprobe"), "PATH"),
            (os.environ.get("ADT_VIDEO_FFMPEG"), "ADT_VIDEO_FFMPEG"),
            (package_bin / f"ffmpeg{executable_suffix}", "bundled-fallback"),
            (shutil.which("ffmpeg"), "PATH-fallback"),
        ]
    )

    for candidate, source in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser().resolve()
        if path.is_file():
            return ProbeTool(path=path, kind=_tool_kind(path), source=source)

    raise ProbeUnavailableError(
        "FFprobe or FFmpeg was not found. Supply an explicit path, set "
        "ADT_VIDEO_FFPROBE/ADT_VIDEO_FFMPEG, or use a packaged release."
    )


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _frame_rate(value: Any) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        rate = float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def parse_ffprobe_document(path: Path, size_bytes: int, document: dict[str, Any]) -> MediaInfo:
    """Convert FFprobe JSON into the stable internal media model."""

    streams = document.get("streams")
    if not isinstance(streams, list):
        raise ProbeFailedError("FFprobe returned no stream collection.")

    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if not video_streams:
        raise ProbeFailedError(f"No video stream was found in '{path}'.")

    primary = video_streams[0]
    width = _positive_int(primary.get("width"))
    height = _positive_int(primary.get("height"))
    if width is None or height is None:
        raise ProbeFailedError(f"The primary video dimensions are missing for '{path}'.")

    format_document = document.get("format") if isinstance(document.get("format"), dict) else {}
    duration = _positive_float(format_document.get("duration"))
    if duration is None:
        duration = max(
            (_positive_float(stream.get("duration")) or 0.0 for stream in streams),
            default=0.0,
        )
    if duration <= 0:
        raise ProbeFailedError(f"The video duration is missing or invalid for '{path}'.")

    format_names = tuple(
        name.strip()
        for name in str(format_document.get("format_name") or path.suffix.lstrip(".")).split(",")
        if name.strip()
    )
    video = VideoStreamInfo(
        codec=str(primary.get("codec_name") or "unknown").lower(),
        width=width,
        height=height,
        pixel_format=str(primary["pix_fmt"]).lower() if primary.get("pix_fmt") else None,
        frames_per_second=_frame_rate(primary.get("avg_frame_rate") or primary.get("r_frame_rate")),
        bitrate=_positive_int(primary.get("bit_rate")),
    )
    return MediaInfo(
        path=path,
        size_bytes=size_bytes,
        duration_seconds=duration,
        format_names=format_names,
        video_stream_count=len(video_streams),
        audio_stream_count=len(audio_streams),
        primary_video=video,
        container_bitrate=_positive_int(format_document.get("bit_rate")),
        probe_kind="ffprobe",
    )


_DURATION_PATTERN = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_BITRATE_PATTERN = re.compile(r"bitrate:\s*(\d+)\s*kb/s", re.IGNORECASE)
_RESOLUTION_PATTERN = re.compile(r"(?<!\d)(\d{2,5})x(\d{2,5})(?!\d)")
_FPS_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*fps\b", re.IGNORECASE)
_PIXEL_FORMAT_PATTERN = re.compile(r"\b(yuv[0-9a-z_]+|nv12|p010le)\b", re.IGNORECASE)


def parse_ffmpeg_description(path: Path, size_bytes: int, description: str) -> MediaInfo:
    """Parse FFmpeg's metadata description when FFprobe is unavailable."""

    duration_match = _DURATION_PATTERN.search(description)
    video_lines = [line for line in description.splitlines() if "Stream #" in line and "Video:" in line]
    audio_lines = [line for line in description.splitlines() if "Stream #" in line and "Audio:" in line]
    if not duration_match or not video_lines:
        raise ProbeFailedError(f"FFmpeg could not describe a valid video at '{path}'.")

    hours, minutes, seconds = duration_match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    if duration <= 0:
        raise ProbeFailedError(f"The video duration is missing or invalid for '{path}'.")

    primary_line = video_lines[0]
    resolution = _RESOLUTION_PATTERN.search(primary_line)
    if not resolution:
        raise ProbeFailedError(f"FFmpeg did not report video dimensions for '{path}'.")

    codec_text = primary_line.split("Video:", 1)[1].split(",", 1)[0].strip().split()[0]
    pixel_format_match = _PIXEL_FORMAT_PATTERN.search(primary_line)
    fps_match = _FPS_PATTERN.search(primary_line)
    bitrate_match = _BITRATE_PATTERN.search(description)
    container_bitrate = int(bitrate_match.group(1)) * 1000 if bitrate_match else None
    width, height = (int(value) for value in resolution.groups())

    return MediaInfo(
        path=path,
        size_bytes=size_bytes,
        duration_seconds=duration,
        format_names=(path.suffix.lstrip(".").lower(),),
        video_stream_count=len(video_lines),
        audio_stream_count=len(audio_lines),
        primary_video=VideoStreamInfo(
            codec=codec_text.lower(),
            width=width,
            height=height,
            pixel_format=pixel_format_match.group(1).lower() if pixel_format_match else None,
            frames_per_second=float(fps_match.group(1)) if fps_match else None,
            bitrate=None,
        ),
        container_bitrate=container_bitrate,
        probe_kind="ffmpeg",
    )


def probe_media(
    source: str | os.PathLike[str],
    *,
    probe_path: str | os.PathLike[str] | None = None,
    timeout_seconds: float = 30.0,
) -> MediaInfo:
    """Probe a local video without modifying it."""

    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise InvalidInputError(f"Video file does not exist: '{path}'.")
    size_bytes = path.stat().st_size
    if size_bytes <= 0:
        raise InvalidInputError(f"Video file is empty: '{path}'.")

    tool = resolve_probe_tool(probe_path)
    if tool.kind == "ffprobe":
        command = [
            str(tool.path),
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or "unknown FFprobe error"
            raise ProbeFailedError(f"FFprobe failed for '{path}': {detail}")
        try:
            document = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ProbeFailedError(f"FFprobe returned invalid JSON for '{path}'.") from exc
        return parse_ffprobe_document(path, size_bytes, document)

    command = [str(tool.path), "-hide_banner", "-i", str(path)]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )
    description = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return parse_ffmpeg_description(path, size_bytes, description)

