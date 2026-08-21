from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from adt_video_publisher.batch import run_batch
from adt_video_publisher.compression import CompressionResult
from adt_video_publisher.errors import UnsafePathError
from adt_video_publisher.resources import ResourceSnapshot


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resources() -> ResourceSnapshot:
    return ResourceSnapshot(
        logical_cpu_count=8,
        physical_cpu_count=4,
        total_memory_bytes=8 * 1024**3,
        available_memory_bytes=6 * 1024**3,
        available_disk_bytes=10 * 1024**3,
        hardware_encoders=(),
        platform="test-x86_64",
    )


def successful_result(source: Path, output: Path) -> CompressionResult:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"compressed:" + source.read_bytes())
    source_size = source.stat().st_size
    output_size = output.stat().st_size
    return CompressionResult(
        source=source,
        output=output,
        source_size_bytes=source_size,
        output_size_bytes=output_size,
        reduction_percent=round((1 - output_size / source_size) * 100, 2),
        source_sha256=sha256(source),
        output_sha256=sha256(output),
        attempts=(),
    )


class BatchTests(unittest.TestCase):
    def create_sources(self, root: Path, count: int) -> Path:
        source = root / "source"
        source.mkdir()
        for number in range(1, count + 1):
            (source / f"page_{number}.mp4").write_bytes((f"video-{number}" * 100).encode())
        return source

    def test_batch_obeys_concurrency_and_writes_atomic_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.create_sources(root, 4)
            output = root / "compressed"
            lock = threading.Lock()
            active = 0
            maximum_active = 0
            progress_events: list[tuple[str, str, dict[str, object]]] = []

            def compressor(item_source: Path, item_output: Path, **kwargs: object) -> CompressionResult:
                nonlocal active, maximum_active
                with lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                callback = kwargs["progress_callback"]
                assert callable(callback)
                callback("encoding", 1, 50.0)
                time.sleep(0.04)
                result = successful_result(item_source, item_output)
                with lock:
                    active -= 1
                return result

            result = run_batch(
                source,
                output=output,
                requested_workers=2,
                resource_snapshot=resources(),
                compression_function=compressor,
                progress_callback=lambda job, event, payload: progress_events.append(
                    (job, event, payload)
                ),
            )
            self.assertEqual(result.summary.completed, 4)
            self.assertEqual(result.summary.failed, 0)
            self.assertEqual(maximum_active, 2)
            self.assertTrue(result.state_path.is_file())
            report = json.loads(result.json_report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["completed"], 4)
            with result.csv_report_path.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 4)
            self.assertEqual(list(output.glob("*.tmp")), [])
            event_names = [event for _job, event, _payload in progress_events]
            self.assertEqual(event_names.count("item_started"), 4)
            self.assertEqual(event_names.count("item_progress"), 4)
            self.assertEqual(event_names.count("item_completed"), 4)
            self.assertEqual(event_names[0], "job_started")
            self.assertEqual(event_names[-1], "job_completed")
            self.assertEqual(len({job for job, _event, _payload in progress_events}), 1)

    def test_resume_skips_verified_outputs_and_retries_only_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.create_sources(root, 3)
            output = root / "compressed"
            first_calls: list[str] = []

            def first_compressor(item_source: Path, item_output: Path, **_: object) -> CompressionResult:
                first_calls.append(item_source.name)
                if item_source.name == "page_2.mp4":
                    raise RuntimeError("controlled failure")
                return successful_result(item_source, item_output)

            first = run_batch(
                source,
                output=output,
                requested_workers=1,
                resource_snapshot=resources(),
                compression_function=first_compressor,
            )
            self.assertEqual(first.summary.completed, 2)
            self.assertEqual(first.summary.failed, 1)
            self.assertEqual(first.exit_code, 9)

            resumed_calls: list[str] = []

            def resumed_compressor(item_source: Path, item_output: Path, **_: object) -> CompressionResult:
                resumed_calls.append(item_source.name)
                return successful_result(item_source, item_output)

            resumed = run_batch(
                source,
                output=output,
                requested_workers=1,
                resource_snapshot=resources(),
                compression_function=resumed_compressor,
                resume=True,
            )
            self.assertEqual(resumed_calls, ["page_2.mp4"])
            self.assertEqual(resumed.summary.completed, 1)
            self.assertEqual(resumed.summary.skipped, 2)
            self.assertEqual(resumed.summary.failed, 0)
            state = json.loads(resumed.state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "completed")
            self.assertTrue(all(item["status"] == "completed" for item in state["items"]))

    def test_all_failed_without_output_removes_temporary_job_details(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.create_sources(root, 2)
            output = root / "compressed"

            def failing_compressor(*_args: object, **_kwargs: object) -> CompressionResult:
                raise RuntimeError("controlled failure before output")

            result = run_batch(
                source,
                output=output,
                requested_workers=1,
                resource_snapshot=resources(),
                compression_function=failing_compressor,
            )

            self.assertEqual(result.summary.completed, 0)
            self.assertEqual(result.summary.skipped, 0)
            self.assertEqual(result.summary.failed, 2)
            self.assertTrue(result.job_details_removed)
            self.assertFalse(result.state_path.exists())
            self.assertFalse(result.json_report_path.exists())
            self.assertFalse(result.csv_report_path.exists())
            self.assertEqual(list(output.glob("*.tmp")), [])

    def test_pre_cancelled_batch_keeps_items_pending_and_reports_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.create_sources(root, 2)
            cancellation = threading.Event()
            cancellation.set()
            calls = 0

            def compressor(item_source: Path, item_output: Path, **_: object) -> CompressionResult:
                nonlocal calls
                calls += 1
                return successful_result(item_source, item_output)

            result = run_batch(
                source,
                output=root / "compressed",
                resource_snapshot=resources(),
                cancel_event=cancellation,
                compression_function=compressor,
            )
            self.assertEqual(calls, 0)
            self.assertTrue(result.interrupted)
            self.assertEqual(result.exit_code, 11)
            self.assertEqual(result.summary.skipped, 2)
            state = json.loads(result.state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "interrupted")
            self.assertTrue(all(item["status"] == "pending" for item in state["items"]))

    def test_completed_resume_does_not_require_encoding_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.create_sources(root, 1)
            output = root / "compressed"
            run_batch(
                source,
                output=output,
                resource_snapshot=resources(),
                compression_function=lambda item_source, item_output, **_: successful_result(
                    item_source, item_output
                ),
            )
            low_memory = ResourceSnapshot(
                logical_cpu_count=2,
                physical_cpu_count=1,
                total_memory_bytes=512 * 1024**2,
                available_memory_bytes=64 * 1024**2,
                available_disk_bytes=1024,
                hardware_encoders=(),
                platform="test-low-resource",
            )
            resumed = run_batch(
                source,
                output=output,
                resume=True,
                resource_snapshot=low_memory,
                compression_function=lambda *_args, **_kwargs: self.fail("resume re-encoded output"),
            )
            self.assertEqual(resumed.summary.skipped, 1)
            self.assertEqual(resumed.worker_plan.limiting_factor, "resume")

    def test_state_file_cannot_be_written_into_the_source_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.create_sources(root, 1)
            with self.assertRaises(UnsafePathError):
                run_batch(
                    source,
                    output=root / "compressed",
                    state_path=source / "job-state.json",
                    resource_snapshot=resources(),
                    compression_function=lambda *_args, **_kwargs: self.fail("unsafe job ran"),
                )


if __name__ == "__main__":
    unittest.main()
