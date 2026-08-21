from __future__ import annotations

import unittest

from adt_video_publisher.errors import ResourceLimitError
from adt_video_publisher.resources import ResourceSnapshot, select_worker_plan


def snapshot(*, available_memory: int = 8 * 1024**3, available_disk: int = 20 * 1024**3) -> ResourceSnapshot:
    return ResourceSnapshot(
        logical_cpu_count=16,
        physical_cpu_count=8,
        total_memory_bytes=16 * 1024**3,
        available_memory_bytes=available_memory,
        available_disk_bytes=available_disk,
        hardware_encoders=(),
        platform="test-x86_64",
    )


class ResourcePlanningTests(unittest.TestCase):
    def test_auto_workers_are_bounded_by_cpu_memory_and_items(self) -> None:
        plan = select_worker_plan(snapshot(), item_count=20)
        self.assertEqual(plan.workers, 4)
        self.assertEqual(plan.encoder_threads_per_worker, 4)
        self.assertEqual(plan.limiting_factor, "cpu")

    def test_explicit_worker_count_is_clamped_to_safe_limit(self) -> None:
        plan = select_worker_plan(snapshot(), item_count=20, requested_workers=2)
        self.assertEqual(plan.workers, 2)
        self.assertEqual(plan.encoder_threads_per_worker, 8)
        self.assertEqual(plan.limiting_factor, "requested")

    def test_low_memory_is_rejected_before_processing(self) -> None:
        with self.assertRaises(ResourceLimitError):
            select_worker_plan(snapshot(available_memory=300 * 1024**2), item_count=3)

    def test_low_disk_is_rejected_before_processing(self) -> None:
        with self.assertRaises(ResourceLimitError) as raised:
            select_worker_plan(snapshot(available_disk=1024), item_count=3)
        self.assertIn("MB required", str(raised.exception))
        self.assertIn("MB available", str(raised.exception))
        self.assertNotIn("bytes required", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
