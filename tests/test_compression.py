from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Sequence
from unittest.mock import patch

from adt_video_publisher.compression import (
    build_ffmpeg_command,
    compress_video,
    measure_ssim,
    validate_candidate,
)
from adt_video_publisher.errors import UnsafePathError, ValidationFailedError
from adt_video_publisher.media import MediaInfo, VideoStreamInfo


def media_for(
    path: Path,
    *,
    size_bytes: int,
    duration: float = 60.0,
    audio_streams: int = 0,
    codec: str = "h264",
    pixel_format: str = "yuv420p",
) -> MediaInfo:
    return MediaInfo(
        path=path,
        size_bytes=size_bytes,
        duration_seconds=duration,
        format_names=("mp4",),
        video_stream_count=1,
        audio_stream_count=audio_streams,
        primary_video=VideoStreamInfo(
            codec=codec,
            width=1280,
            height=720,
            pixel_format=pixel_format,
            frames_per_second=25.0,
            bitrate=1_000_000,
        ),
        container_bitrate=1_000_000,
        probe_kind="ffmpeg",
    )


class CompressionTests(unittest.TestCase):
    def test_encode_command_maps_video_only_and_removes_every_other_stream(self) -> None:
        command = build_ffmpeg_command(
            ffmpeg=Path("ffmpeg"),
            source=Path("source.mp4"),
            candidate=Path("candidate.mp4"),
            action="encode",
            target_video_bitrate=None,
            preset="medium",
            crf=21,
            encoder_threads=4,
        )
        self.assertIn("0:v:0", command)
        self.assertIn("-an", command)
        self.assertIn("-sn", command)
        self.assertIn("-dn", command)
        self.assertEqual(command[command.index("-c:v") + 1], "libx264")
        self.assertEqual(command[command.index("-crf") + 1], "21")
        self.assertNotIn("-b:v", command)
        self.assertEqual(command[command.index("-pix_fmt") + 1], "yuv420p")
        self.assertEqual(command[command.index("-threads") + 1], "4")

    def test_adaptive_scale_command_preserves_aspect_ratio_and_even_dimensions(self) -> None:
        command = build_ffmpeg_command(
            ffmpeg=Path("ffmpeg"),
            source=Path("source.mp4"),
            candidate=Path("candidate.mp4"),
            action="encode",
            target_video_bitrate=None,
            preset="medium",
            scale_factor=0.75,
        )
        self.assertEqual(
            command[command.index("-vf") + 1],
            "scale=trunc(iw*0.75/2)*2:trunc(ih*0.75/2)*2",
        )

    def test_progress_command_requests_ffmpeg_machine_percentages(self) -> None:
        command = build_ffmpeg_command(
            ffmpeg=Path("ffmpeg"),
            source=Path("source.mp4"),
            candidate=Path("candidate.mp4"),
            action="encode",
            target_video_bitrate=None,
            preset="medium",
            progress_pipe=True,
        )
        self.assertEqual(command[command.index("-progress") + 1], "pipe:1")
        self.assertIn("-nostats", command)

    def test_validation_rejects_audio_and_oversized_output(self) -> None:
        candidate = media_for(Path("candidate.mp4"), size_bytes=6 * 1024 * 1024, audio_streams=1)
        report = validate_candidate(
            media=candidate,
            source_duration_seconds=60.0,
            maximum_bytes=5 * 1024 * 1024,
        )
        self.assertFalse(report.valid)
        self.assertFalse(report.checks["size_within_limit"])
        self.assertFalse(report.checks["audio_removed"])

    def test_strict_validation_rejects_oversize_and_low_quality(self) -> None:
        candidate = media_for(Path("candidate.mp4"), size_bytes=6 * 1024 * 1024)
        report = validate_candidate(
            media=candidate,
            source_duration_seconds=60.0,
            maximum_bytes=5 * 1024 * 1024,
            strict_size=True,
            quality_score=0.90,
            minimum_quality_score=0.95,
        )
        self.assertFalse(report.valid)
        self.assertFalse(report.checks["size_within_limit"])
        self.assertFalse(report.checks["quality_preserved"])

    def test_ssim_measurement_parses_ffmpeg_summary(self) -> None:
        completed = subprocess.CompletedProcess(
            ["ffmpeg"],
            0,
            "",
            "SSIM Y:0.98 U:0.99 V:0.99 All:0.987654 (18.0)",
        )
        with patch("adt_video_publisher.compression._run_ffmpeg", return_value=completed) as run:
            score = measure_ssim(
                Path("ffmpeg"),
                Path("source.mp4"),
                Path("candidate.mp4"),
                None,
            )
        self.assertEqual(score, 0.987654)
        command = run.call_args.args[0]
        self.assertIn("ssim", command[command.index("-filter_complex") + 1])

    def test_low_quality_first_attempt_is_retried_at_higher_quality(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source" / "page_1.mp4"
            output = root / "compressed" / "page_1.mp4"
            ffmpeg = root / "ffmpeg.exe"
            source.parent.mkdir()
            source.write_bytes(b"source-video" * 600_000)
            ffmpeg.write_bytes(b"binary")
            source_media = media_for(source, size_bytes=source.stat().st_size)
            sizes = [6 * 1024 * 1024, 7 * 1024 * 1024]
            commands: list[Sequence[str]] = []
            quality_scores = iter((0.90, 0.98))

            def runner(command: Sequence[str], timeout: float | None) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                candidate = Path(command[-1])
                with candidate.open("wb") as stream:
                    stream.truncate(sizes[len(commands) - 1])
                return subprocess.CompletedProcess(command, 0, "", "")

            def probe(path: Path, **_: object) -> MediaInfo:
                if path == source:
                    return source_media
                return replace(source_media, path=path, size_bytes=path.stat().st_size, audio_stream_count=0)

            result = compress_video(
                source,
                output,
                ffmpeg_path=ffmpeg,
                command_runner=runner,
                probe_function=probe,
                quality_function=lambda *_args: next(quality_scores),
                strict_size=False,
                adaptive_scale=False,
            )
            self.assertEqual(len(result.attempts), 2)
            self.assertEqual(output.stat().st_size, sizes[1])
            self.assertEqual(result.attempts[-1].validation.media.path, output.resolve())
            first_crf = int(commands[0][commands[0].index("-crf") + 1])
            second_crf = int(commands[1][commands[1].index("-crf") + 1])
            self.assertLess(second_crf, first_crf)
            self.assertFalse(result.attempts[-1].validation.checks["size_within_limit"])
            self.assertTrue(result.attempts[-1].validation.valid)
            self.assertEqual(list(output.parent.glob("*.part.mp4")), [])

    def test_failed_validation_preserves_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source" / "page.mp4"
            output = root / "compressed" / "page.mp4"
            ffmpeg = root / "ffmpeg.exe"
            source.parent.mkdir()
            output.parent.mkdir()
            source.write_bytes(b"source-video" * 600_000)
            output.write_bytes(b"known-good-output")
            ffmpeg.write_bytes(b"binary")
            source_media = media_for(source, size_bytes=source.stat().st_size)

            def runner(command: Sequence[str], timeout: float | None) -> subprocess.CompletedProcess[str]:
                Path(command[-1]).write_bytes(b"invalid-candidate")
                return subprocess.CompletedProcess(command, 0, "", "")

            def probe(path: Path, **_: object) -> MediaInfo:
                if path == source:
                    return source_media
                return replace(
                    source_media,
                    path=path,
                    size_bytes=path.stat().st_size,
                    audio_stream_count=1,
                )

            with self.assertRaises(ValidationFailedError):
                compress_video(
                    source,
                    output,
                    ffmpeg_path=ffmpeg,
                    replace_existing=True,
                    command_runner=runner,
                    probe_function=probe,
                    quality_function=lambda *_args: 0.99,
                )
            self.assertEqual(output.read_bytes(), b"known-good-output")
            self.assertEqual(list(output.parent.glob("*.part.mp4")), [])

    def test_output_cannot_share_the_source_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "page.mp4"
            source.write_bytes(b"video")
            with self.assertRaises(UnsafePathError):
                compress_video(source, root / "compressed.mp4", ffmpeg_path=root / "ffmpeg.exe")


if __name__ == "__main__":
    unittest.main()
