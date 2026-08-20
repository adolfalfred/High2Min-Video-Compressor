from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from adt_video_publisher.errors import ProbeFailedError
from adt_video_publisher.media import parse_ffmpeg_description, parse_ffprobe_document


class MediaParsingTests(unittest.TestCase):
    def test_parses_ffprobe_json(self) -> None:
        document = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1280,
                    "height": 720,
                    "pix_fmt": "yuv420p",
                    "avg_frame_rate": "25/1",
                    "bit_rate": "900000",
                },
                {"codec_type": "audio", "codec_name": "aac"},
            ],
            "format": {
                "duration": "12.5",
                "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                "bit_rate": "1024000",
            },
        }

        info = parse_ffprobe_document(Path("lesson.mp4"), 1_600_000, document)

        self.assertEqual(info.primary_video.codec, "h264")
        self.assertEqual((info.primary_video.width, info.primary_video.height), (1280, 720))
        self.assertEqual(info.primary_video.frames_per_second, 25.0)
        self.assertEqual(info.audio_stream_count, 1)
        self.assertTrue(info.has_audio)
        self.assertEqual(info.duration_seconds, 12.5)

    def test_parses_ffmpeg_fallback_description(self) -> None:
        description = """
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'lesson.mp4':
  Duration: 00:01:02.50, start: 0.000000, bitrate: 1200 kb/s
  Stream #0:0: Video: h264 (High), yuv420p(progressive), 1920x1080, 25 fps
  Stream #0:1: Audio: aac, 48000 Hz, stereo
"""
        info = parse_ffmpeg_description(Path("lesson.mp4"), 9_000_000, description)
        self.assertAlmostEqual(info.duration_seconds, 62.5)
        self.assertEqual(info.primary_video.pixel_format, "yuv420p")
        self.assertEqual(info.primary_video.frames_per_second, 25.0)
        self.assertEqual(info.container_bitrate, 1_200_000)
        self.assertEqual(info.audio_stream_count, 1)

    def test_rejects_descriptions_without_video(self) -> None:
        with self.assertRaises(ProbeFailedError):
            parse_ffmpeg_description(Path("broken.mp4"), 100, "Duration: 00:00:01.00")


if __name__ == "__main__":
    unittest.main()

