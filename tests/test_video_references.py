from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from adt_video_publisher.video_references import (
    parse_video_reference,
    resolve_book_video_reference,
    versioned_video_reference,
)


class VideoReferenceTests(unittest.TestCase):
    def test_parser_separates_browser_suffix_from_physical_filename(self) -> None:
        reference = parse_video_reference("signed%20page.mp4?v=17#start")

        self.assertEqual(reference.filename, "signed page.mp4")
        self.assertEqual(reference.query, "v=17")
        self.assertEqual(reference.fragment, "start")
        self.assertTrue(reference.has_cache_version)

    def test_versioning_preserves_query_and_fragment(self) -> None:
        self.assertEqual(
            versioned_video_reference(
                "page_1.mp4",
                cache_version="18",
                existing_reference="old-name.mp4?quality=high&v=17#start",
            ),
            "page_1.mp4?quality=high&v=18#start",
        )
        self.assertEqual(
            versioned_video_reference(
                "page_2.mp4",
                cache_version="18",
                inherit_cache_version=True,
            ),
            "page_2.mp4?v=18",
        )

    def test_parser_rejects_nonlocal_or_traversing_references(self) -> None:
        invalid = (
            "../page_1.mp4?v=17",
            "/page_1.mp4?v=17",
            "https://example.invalid/page_1.mp4",
            "folder/page_1.mp4?v=17",
            "..%2Fpage_1.mp4?v=17",
            "page%00_1.mp4?v=17",
            "page%3F_1.mp4?v=17",
            "page_1.webm?v=17",
            "page_1.mp4%ZZ",
        )

        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_video_reference(value)

    def test_book_resolver_accepts_legacy_paths_only_when_they_stay_in_book(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            book = Path(temporary)
            resolved = resolve_book_video_reference(
                "../../../../Videos_web/page_1.mp4?v=17",
                book=book,
                language="en",
            )

            self.assertEqual(resolved.filename, "page_1.mp4")
            self.assertEqual(resolved.root_relative, "Videos_web/page_1.mp4")
            self.assertTrue(resolved.has_cache_version)
            with self.assertRaises(ValueError):
                resolve_book_video_reference(
                    "../../../../../outside/page_1.mp4",
                    book=book,
                    language="en",
                )


if __name__ == "__main__":
    unittest.main()
