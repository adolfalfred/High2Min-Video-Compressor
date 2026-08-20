from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from adt_video_publisher.errors import InvalidInputError, UnsafePathError
from adt_video_publisher.paths import build_path_plan


class PathPlanningTests(unittest.TestCase):
    def test_directory_plan_preserves_structure_and_uses_sibling_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Original Videos"
            nested = source / "Chapter 1"
            nested.mkdir(parents=True)
            (source / "page_1.mp4").write_bytes(b"video")
            (nested / "page_2.mov").write_bytes(b"video")

            plan = build_path_plan(source, recursive=True)

            self.assertEqual(plan.output_root, root / "Original Videos - Compressed")
            self.assertEqual(len(plan.items), 2)
            self.assertEqual(plan.items[1].output, plan.output_root / "page_1.mp4")
            self.assertEqual(plan.items[0].output, plan.output_root / "Chapter 1" / "page_2.mp4")

    def test_rejects_output_inside_source_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            (source / "page_1.mp4").write_bytes(b"video")
            with self.assertRaises(UnsafePathError):
                build_path_plan(source, output=source / "compressed")

    def test_rejects_single_file_output_in_source_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "page_1.mp4"
            source.write_bytes(b"video")
            with self.assertRaises(UnsafePathError):
                build_path_plan(source, output=source.parent)

    def test_rejects_colliding_output_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            (source / "page_1.mov").write_bytes(b"video")
            (source / "page_1.avi").write_bytes(b"video")
            with self.assertRaises(UnsafePathError):
                build_path_plan(source)

    def test_rejects_empty_video_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            with self.assertRaises(InvalidInputError):
                build_path_plan(source)


if __name__ == "__main__":
    unittest.main()

