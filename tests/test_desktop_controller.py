from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from adt_video_publisher.desktop_controller import (
    DesktopController,
    DesktopPublishSettings,
    DesktopSettings,
    mebibytes_to_bytes,
    parse_workers,
    suggested_output,
)
from adt_video_publisher.errors import InvalidInputError
from adt_video_publisher.resources import ResourceSnapshot


def resources() -> ResourceSnapshot:
    return ResourceSnapshot(
        logical_cpu_count=8,
        physical_cpu_count=4,
        total_memory_bytes=8 * 1024**3,
        available_memory_bytes=6 * 1024**3,
        available_disk_bytes=10 * 1024**3,
        hardware_encoders=("h264_qsv",),
        platform="test",
    )


class DesktopControllerTests(unittest.TestCase):
    def test_ui_value_parsing_is_strict(self) -> None:
        self.assertEqual(parse_workers(" auto "), "auto")
        self.assertEqual(parse_workers("3"), 3)
        self.assertEqual(mebibytes_to_bytes("5"), 5 * 1024 * 1024)
        with self.assertRaises(InvalidInputError):
            parse_workers("0")
        with self.assertRaises(InvalidInputError):
            mebibytes_to_bytes("0.5")

    def test_analysis_is_read_only_and_uses_safe_worker_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            output = root / "compressed"
            source.mkdir()
            (source / "page_1.mp4").write_bytes(b"video")
            controller = DesktopController(
                resource_detector=lambda *_args, **_kwargs: resources()
            )
            result = controller.analyze(DesktopSettings(source=str(source), output=str(output)))
            self.assertEqual(len(result.path_plan.items), 1)
            self.assertEqual(result.worker_plan.workers, 1)
            self.assertEqual(result.resources.hardware_encoders, ("h264_qsv",))
            self.assertFalse(output.exists())
            self.assertEqual(suggested_output(str(source)), str(source.parent / "source - Compressed"))

    def test_compress_forwards_settings_cancellation_and_progress(self) -> None:
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        sentinel = object()

        def runner(*args: object, **kwargs: object) -> object:
            calls.append((args, kwargs))
            return sentinel

        controller = DesktopController(batch_runner=runner)  # type: ignore[arg-type]
        cancellation = threading.Event()
        progress = lambda *_args: None
        result = controller.compress(
            DesktopSettings(
                source="source",
                output="output",
                workers=2,
                maximum_bytes=4_000_000,
                ffmpeg_path="ffmpeg",
            ),
            cancel_event=cancellation,
            progress_callback=progress,
        )
        self.assertIs(result, sentinel)
        arguments, keywords = calls[0]
        self.assertEqual(arguments, ("source",))
        self.assertEqual(keywords["output"], "output")
        self.assertEqual(keywords["requested_workers"], 2)
        self.assertEqual(keywords["maximum_bytes"], 4_000_000)
        self.assertIs(keywords["cancel_event"], cancellation)
        self.assertIs(keywords["progress_callback"], progress)

    def test_resume_uses_saved_paths_and_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / ".adt-video-job.json"
            state.write_text(
                json.dumps(
                    {
                        "source": "saved-source",
                        "output": "saved-output",
                        "settings": {
                            "maximum_bytes": 4_500_000,
                            "maximum_attempts": 3,
                            "preset": "fast",
                            "recursive": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

            def runner(*args: object, **kwargs: object) -> object:
                calls.append((args, kwargs))
                return object()

            controller = DesktopController(batch_runner=runner)  # type: ignore[arg-type]
            controller.resume(state, workers=2, ffmpeg_path="ffmpeg")
            arguments, keywords = calls[0]
            self.assertEqual(arguments, ("saved-source",))
            self.assertEqual(keywords["output"], "saved-output")
            self.assertEqual(keywords["maximum_attempts"], 3)
            self.assertEqual(keywords["preset"], "fast")
            self.assertTrue(keywords["resume"])
            self.assertEqual(keywords["state_path"], state.resolve())

    def test_publish_forwards_adt_copy_and_package_settings(self) -> None:
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        sentinel = object()

        def publisher(*args: object, **kwargs: object) -> object:
            calls.append((args, kwargs))
            return sentinel

        controller = DesktopController(publisher=publisher)  # type: ignore[arg-type]
        progress = lambda *_args: None
        result = controller.publish(
            DesktopPublishSettings(
                videos="compressed",
                book="source-book",
                output="published-book",
                package="book.zip",
                language="en-GB",
                recursive=True,
                maximum_bytes=4_000_000,
                probe_path="ffmpeg",
            ),
            progress_callback=progress,
        )
        self.assertIs(result, sentinel)
        arguments, keywords = calls[0]
        self.assertEqual(arguments, ("compressed",))
        self.assertEqual(keywords["book"], "source-book")
        self.assertEqual(keywords["output"], "published-book")
        self.assertEqual(keywords["package"], "book.zip")
        self.assertFalse(keywords["in_place"])
        self.assertEqual(keywords["language"], "en-GB")
        self.assertTrue(keywords["recursive"])
        self.assertIs(keywords["progress_callback"], progress)

    def test_publish_forwards_in_place_desktop_mode_without_zip(self) -> None:
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def publisher(*args: object, **kwargs: object) -> object:
            calls.append((args, kwargs))
            return object()

        controller = DesktopController(publisher=publisher)  # type: ignore[arg-type]
        controller.publish(
            DesktopPublishSettings(
                videos="compressed",
                book="source-book",
                in_place=True,
            )
        )
        _arguments, keywords = calls[0]
        self.assertIsNone(keywords["output"])
        self.assertIsNone(keywords["package"])
        self.assertTrue(keywords["in_place"])


if __name__ == "__main__":
    unittest.main()
