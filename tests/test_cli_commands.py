from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from adt_video_publisher.cli import ProgressEmitter, main
from adt_video_publisher.contracts import ExitCode


def fake_batch_result() -> SimpleNamespace:
    document = {
        "schema_version": "1.0",
        "job_id": "job-test",
        "ok": True,
        "exit_code": 0,
        "summary": {
            "total": 1,
            "completed": 1,
            "skipped": 0,
            "failed": 0,
            "original_bytes": 100,
            "output_bytes": 50,
        },
        "items": [],
    }
    return SimpleNamespace(
        exit_code=0,
        summary=SimpleNamespace(total=1, completed=1, skipped=0, failed=0),
        json_report_path=Path("compression-report.json"),
        csv_report_path=Path("compression-report.csv"),
        to_result_document=lambda: document,
    )


class CliCommandTests(unittest.TestCase):
    def test_plan_is_read_only_and_emits_machine_readable_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            output = root / "compressed"
            source.mkdir()
            (source / "page_1.mp4").write_bytes(b"video")
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = main(
                [
                    "plan",
                    "--input",
                    str(source),
                    "--output",
                    str(output),
                    "--json",
                    "--progress",
                    "ndjson",
                ],
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(exit_code, ExitCode.SUCCESS)
            document = json.loads(stdout.getvalue())
            self.assertEqual(document["schema_version"], "1.0")
            self.assertEqual(document["items"][0]["status"], "ready")
            self.assertFalse(output.exists())
            events = [json.loads(line) for line in stderr.getvalue().splitlines()]
            self.assertEqual([event["event"] for event in events], ["job_planned"])
            self.assertEqual(events[0]["sequence"], 0)

    def test_compress_dispatches_all_automation_options(self) -> None:
        stdout = io.StringIO()
        with patch("adt_video_publisher.cli.run_batch", return_value=fake_batch_result()) as run:
            exit_code = main(
                [
                    "compress",
                    "--input",
                    "source",
                    "--output",
                    "compressed",
                    "--workers",
                    "3",
                    "--ffmpeg",
                    "ffmpeg",
                    "--json",
                    "--progress=ndjson",
                ],
                stdout=stdout,
                stderr=io.StringIO(),
            )
        self.assertEqual(exit_code, ExitCode.SUCCESS)
        self.assertTrue(json.loads(stdout.getvalue())["ok"])
        keywords = run.call_args.kwargs
        self.assertEqual(keywords["requested_workers"], 3)
        self.assertEqual(keywords["ffmpeg_path"], "ffmpeg")
        self.assertIsNotNone(keywords["progress_callback"])

    def test_resume_derives_paths_and_settings_from_job_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / ".adt-video-job.json"
            state_path.write_text(
                json.dumps(
                    {
                        "source": "source-folder",
                        "output": "output-folder",
                        "settings": {
                            "maximum_bytes": 4_000_000,
                            "maximum_attempts": 3,
                            "preset": "fast",
                            "recursive": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch("adt_video_publisher.cli.run_batch", return_value=fake_batch_result()) as run:
                exit_code = main(
                    ["resume", "--job", str(state_path), "--workers", "2", "--json"],
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(exit_code, ExitCode.SUCCESS)
            keywords = run.call_args.kwargs
            self.assertTrue(keywords["resume"])
            self.assertEqual(keywords["maximum_bytes"], 4_000_000)
            self.assertEqual(keywords["maximum_attempts"], 3)
            self.assertEqual(keywords["preset"], "fast")
            self.assertTrue(keywords["recursive"])

    def test_domain_error_uses_stable_structured_exit(self) -> None:
        stdout = io.StringIO()
        exit_code = main(
            ["plan", "--input", "missing-path", "--json"],
            stdout=stdout,
            stderr=io.StringIO(),
        )
        document = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, ExitCode.INVALID_INPUT)
        self.assertEqual(document["error"]["name"], "INVALID_INPUT")

    def test_progress_emitter_is_thread_safe_and_monotonic(self) -> None:
        stream = io.StringIO()
        emitter = ProgressEmitter(stream, enabled=True)
        threads = [
            threading.Thread(
                target=emitter.emit,
                args=("job", "item_progress", {"value": number}),
            )
            for number in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        events = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual([event["sequence"] for event in events], list(range(20)))
        self.assertTrue(all(event["schema_version"] == "1.0" for event in events))

    def test_ui_command_is_lazy_and_does_not_affect_json_commands(self) -> None:
        with patch("adt_video_publisher.desktop.run", return_value=0) as desktop:
            exit_code = main(["ui"], stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(exit_code, 0)
        desktop.assert_called_once_with()

    def test_ui_smoke_test_is_available_without_opening_mainloop(self) -> None:
        with patch("adt_video_publisher.desktop.smoke_test", return_value=0) as smoke:
            exit_code = main(["ui", "--smoke-test"], stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(exit_code, 0)
        smoke.assert_called_once_with()

    def test_publish_dispatches_safe_book_copy_and_package_options(self) -> None:
        published = SimpleNamespace(
            videos=(object(), object()),
            output_book=Path("published-book"),
            language="en-GB",
            bundle_version="4",
            package=SimpleNamespace(path=Path("book.zip")),
            to_result_document=lambda: {
                "schema_version": "1.0",
                "job_id": "publish-job",
                "ok": True,
                "exit_code": 0,
                "summary": {"total": 2, "completed": 2, "failed": 0, "video_bytes": 10},
                "book": {
                    "source": "book",
                    "output": "published-book",
                    "language": "en-GB",
                    "bundle_version": "4",
                },
                "package": None,
                "items": [],
            },
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("adt_video_publisher.cli.publish_adt", return_value=published) as publish:
            exit_code = main(
                [
                    "publish",
                    "--input", "compressed",
                    "--book", "book",
                    "--output", "published-book",
                    "--package", "book.zip",
                    "--language", "en-GB",
                    "--json",
                    "--progress", "ndjson",
                ],
                stdout=stdout,
                stderr=stderr,
            )
        self.assertEqual(exit_code, ExitCode.SUCCESS)
        self.assertTrue(json.loads(stdout.getvalue())["ok"])
        keywords = publish.call_args.kwargs
        self.assertEqual(keywords["book"], "book")
        self.assertEqual(keywords["output"], "published-book")
        self.assertEqual(keywords["package"], "book.zip")
        self.assertFalse(keywords["in_place"])
        self.assertIsNotNone(keywords["progress_callback"])

    def test_publish_dispatches_explicit_in_place_mode_without_package(self) -> None:
        published = SimpleNamespace(
            videos=(object(),),
            output_book=Path("book"),
            language="en-GB",
            bundle_version="5",
            package=None,
            to_result_document=lambda: {"ok": True},
        )
        with patch("adt_video_publisher.cli.publish_adt", return_value=published) as publish:
            exit_code = main(
                [
                    "publish",
                    "--input", "compressed",
                    "--book", "book",
                    "--in-place",
                ],
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(exit_code, ExitCode.SUCCESS)
        keywords = publish.call_args.kwargs
        self.assertIsNone(keywords["output"])
        self.assertIsNone(keywords["package"])
        self.assertTrue(keywords["in_place"])


if __name__ == "__main__":
    unittest.main()
