from __future__ import annotations

import unittest
from pathlib import Path

from adt_video_publisher.media import MediaInfo, VideoStreamInfo
from adt_video_publisher.planning import calculate_encoding_plan


def media_info(
    *,
    size_bytes: int,
    duration_seconds: float = 60.0,
    codec: str = "h264",
    pixel_format: str = "yuv420p",
    audio_streams: int = 0,
) -> MediaInfo:
    return MediaInfo(
        path=Path("page_1.mp4"),
        size_bytes=size_bytes,
        duration_seconds=duration_seconds,
        format_names=("mp4",),
        video_stream_count=1,
        audio_stream_count=audio_streams,
        primary_video=VideoStreamInfo(
            codec=codec,
            width=1920,
            height=1080,
            pixel_format=pixel_format,
            frames_per_second=25.0,
            bitrate=2_000_000,
        ),
        container_bitrate=2_100_000,
        probe_kind="ffprobe",
    )


class BitratePlanningTests(unittest.TestCase):
    def test_small_compatible_video_uses_fast_remux(self) -> None:
        plan = calculate_encoding_plan(media_info(size_bytes=3 * 1024 * 1024))
        self.assertEqual(plan.action, "remux")
        self.assertIsNone(plan.target_video_bitrate)

    def test_audio_is_removed_during_fast_remux(self) -> None:
        plan = calculate_encoding_plan(
            media_info(size_bytes=4 * 1024 * 1024, audio_streams=1)
        )
        self.assertEqual(plan.action, "remux")
        self.assertIn("Remove non-video streams", plan.reason)

    def test_large_video_gets_duration_aware_target_bitrate(self) -> None:
        plan = calculate_encoding_plan(
            media_info(size_bytes=80 * 1024 * 1024, duration_seconds=120.0)
        )
        self.assertEqual(plan.action, "encode")
        self.assertLess(plan.target_bytes, plan.maximum_bytes)
        self.assertIsNotNone(plan.target_video_bitrate)
        projected_bytes = plan.target_video_bitrate * 120.0 / 8 + plan.reserved_overhead_bytes
        self.assertLessEqual(projected_bytes, plan.target_bytes)

    def test_non_h264_video_is_encoded_even_when_small(self) -> None:
        plan = calculate_encoding_plan(
            media_info(size_bytes=2 * 1024 * 1024, codec="vp9")
        )
        self.assertEqual(plan.action, "encode")


if __name__ == "__main__":
    unittest.main()
