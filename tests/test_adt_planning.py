from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from adt_video_publisher.adt_planning import analyze_adt_publish, plan_videos
from adt_video_publisher.errors import InvalidInputError

IMS = "http://www.imsproject.org/xsd/imscp_rootv1p1p2"


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def make_profile(root: Path) -> Path:
    book = root / "book"
    assets = book / "assets"
    language = book / "content" / "i18n" / "sw-TZ"
    (language / "video").mkdir(parents=True)
    assets.mkdir(parents=True)
    pages = ("index.html", "pg002_joined.html", "pg003.html")
    for page in pages:
        (book / page).write_text(
            '<link rel="stylesheet" href="./assets/book.css">'
            '<script src="./assets/base.bundle.local.js?v=4"></script>',
            encoding="utf-8",
        )
    (assets / "book.css").write_text("body{font-size:1rem}", encoding="utf-8")
    (assets / "base.bundle.local.js").write_text("window.reader=true", encoding="utf-8")
    (assets / "config.json").write_text(json.dumps({
        "bundleVersion": "4",
        "languages": {"available": ["sw-TZ"], "default": "sw-TZ"},
        "features": {"readAloud": True, "signLanguage": False},
    }), encoding="utf-8")
    (book / "content" / "pages.json").write_text(
        json.dumps([{"href": page} for page in pages]), encoding="utf-8"
    )
    (language / "videos.json").write_text(
        json.dumps({"video-3": "page_3.mp4"}), encoding="utf-8"
    )
    (language / "video" / "page_3.mp4").write_bytes(b"existing")
    (assets / "offline-preloader.js").write_text(
        '(function(){const INLINE={"./index.html":"old"}; const BASE_DIR="";}());',
        encoding="utf-8",
    )
    ET.register_namespace("", IMS)
    manifest = ET.Element(f"{{{IMS}}}manifest")
    resources = ET.SubElement(manifest, f"{{{IMS}}}resources")
    resource = ET.SubElement(resources, f"{{{IMS}}}resource", {"href": "index.html"})
    for path in sorted(item for item in book.rglob("*") if item.is_file()):
        ET.SubElement(resource, f"{{{IMS}}}file", {"href": path.relative_to(book).as_posix()})
    ET.ElementTree(manifest).write(book / "imsmanifest.xml", encoding="utf-8", xml_declaration=True)
    return book


class AdtPlanningTests(unittest.TestCase):
    def test_filename_mapping_uses_one_number_group_and_normalizes_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Signed lesson 0002.mp4").write_bytes(b"two")
            (root / "13.mp4").write_bytes(b"thirteen")
            hrefs = tuple(f"page-{number}.html" for number in range(1, 14))

            items = plan_videos(root, page_hrefs=hrefs)

            self.assertEqual([item.page_index for item in items], [2, 13])
            self.assertEqual([item.destination_filename for item in items], ["page_2.mp4", "page_13.mp4"])
            self.assertEqual(items[0].page_href, "page-2.html")

    def test_filename_mapping_rejects_zero_and_multiple_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "lesson_2_take_3.mp4").write_bytes(b"video")
            with self.assertRaisesRegex(InvalidInputError, "exactly one"):
                plan_videos(root, page_hrefs=("one.html", "two.html", "three.html"))
            (root / "lesson_2_take_3.mp4").unlink()
            (root / "lesson_000.mp4").write_bytes(b"video")
            with self.assertRaisesRegex(InvalidInputError, "page zero"):
                plan_videos(root, page_hrefs=("one.html",))

    def test_json_mapping_supports_source_names_without_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "introduction.mp4").write_bytes(b"video")
            mapping = root / "mapping.json"
            mapping.write_text(json.dumps({"introduction.mp4": 2}), encoding="utf-8")

            items = plan_videos(root, page_hrefs=("one.html", "two.html"), mapping_file=mapping)

            self.assertEqual(items[0].page_index, 2)
            self.assertEqual(items[0].mapping_key, "video-2")

    def test_analyzer_is_read_only_and_reports_exact_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_profile(root)
            videos = root / "videos"
            videos.mkdir()
            (videos / "sign 1.mp4").write_bytes(b"replacement")
            before = tree_hash(book)

            plan = analyze_adt_publish(videos, book=book, mode="replace")

            self.assertEqual(tree_hash(book), before)
            self.assertTrue(plan.ready)
            self.assertEqual(plan.active_runtime_files, ("assets/base.bundle.local.js",))
            self.assertEqual(plan.videos[0].page_href, "index.html")
            self.assertIn("content/i18n/sw-TZ/video/page_3.mp4", plan.removals)
            self.assertIn("assets/media-playback-independence.js", plan.mutations)
            self.assertIn("index.html", plan.mutations)


if __name__ == "__main__":
    unittest.main()
