from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

from adt_video_publisher.errors import InvalidInputError, PublishFailedError
from adt_video_publisher.publishing import (
    ADLCP_NAMESPACE,
    IMS_NAMESPACE,
    build_deployment_package,
    declared_manifest_files,
    discover_page_videos,
    publish_adt,
    validate_adt_website,
    validate_deployment_package,
)


def hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((item for item in root.rglob("*") if item.is_file())):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def write_manifest(root: Path) -> None:
    ET.register_namespace("", IMS_NAMESPACE)
    ET.register_namespace("adlcp", ADLCP_NAMESPACE)
    manifest = ET.Element(f"{{{IMS_NAMESPACE}}}manifest", {"identifier": "TEST", "version": "1.0"})
    resources = ET.SubElement(manifest, f"{{{IMS_NAMESPACE}}}resources")
    resource = ET.SubElement(
        resources,
        f"{{{IMS_NAMESPACE}}}resource",
        {
            "identifier": "RESOURCE_1",
            "type": "webcontent",
            f"{{{ADLCP_NAMESPACE}}}scormtype": "sco",
            "href": "index.html",
        },
    )
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name == "imsmanifest.xml" or path.name == "AGENTS.md":
            continue
        ET.SubElement(
            resource,
            f"{{{IMS_NAMESPACE}}}file",
            {"href": path.relative_to(root).as_posix()},
        )
    tree = ET.ElementTree(manifest)
    ET.indent(tree, space="  ")
    tree.write(root / "imsmanifest.xml", encoding="utf-8", xml_declaration=True)


def make_book(root: Path, *, bundle_version: object = "7") -> Path:
    book = root / "book"
    (book / "assets").mkdir(parents=True)
    language = book / "content" / "i18n" / "en-GB"
    (language / "video").mkdir(parents=True)
    (book / "index.html").write_text("<title>Test</title>", encoding="utf-8")
    (book / "pg002.html").write_text("page two", encoding="utf-8")
    (book / "pg003.html").write_text("page three", encoding="utf-8")
    (book / "assets" / "config.json").write_text(
        json.dumps(
            {
                "title": "Test",
                "bundleVersion": bundle_version,
                "languages": {"available": ["en-GB"], "default": "en-GB"},
                "features": {"signLanguage": True},
            }
        ),
        encoding="utf-8",
    )
    (book / "content" / "pages.json").write_text(
        json.dumps(
            [
                {"section_id": "pg001", "href": "index.html"},
                {"section_id": "pg002", "href": "pg002.html"},
                {"section_id": "pg003", "href": "pg003.html"},
            ]
        ),
        encoding="utf-8",
    )
    (language / "videos.json").write_text(
        json.dumps({"video-2": "page_2.mp4"}), encoding="utf-8"
    )
    (language / "video" / "page_2.mp4").write_bytes(b"old-video")
    (book / "AGENTS.md").write_text("development only", encoding="utf-8")
    write_manifest(book)
    return book


class PublishingTests(unittest.TestCase):
    def test_publish_is_authoritative_transactional_and_packaged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"compressed-one")
            (videos / "page_3.mp4").write_bytes(b"compressed-three")
            original_book_hash = hash_tree(book)
            original_videos_hash = hash_tree(videos)
            output = root / "published"
            package = root / "deployment" / "book-v8.zip"
            events: list[str] = []

            result = publish_adt(
                videos,
                book=book,
                output=output,
                package=package,
                validate_media=False,
                progress_callback=lambda _job, event, _payload: events.append(event),
            )

            self.assertEqual(hash_tree(book), original_book_hash)
            self.assertEqual(hash_tree(videos), original_videos_hash)
            self.assertEqual(result.bundle_version, "8")
            self.assertEqual([item.page_index for item in result.videos], [1, 3])
            mappings = json.loads(
                (output / "content" / "i18n" / "en-GB" / "videos.json").read_text()
            )
            self.assertEqual(mappings, {"video-1": "page_1.mp4", "video-3": "page_3.mp4"})
            self.assertFalse((output / "content" / "i18n" / "en-GB" / "video" / "page_2.mp4").exists())
            self.assertFalse((output / "AGENTS.md").exists())
            self.assertEqual(validate_adt_website(output)["video_count"], 2)
            package_report = validate_deployment_package(package)
            self.assertEqual(package_report["entry_count"], len(declared_manifest_files(output / "imsmanifest.xml")) + 1)
            checksum = Path(str(package) + ".sha256").read_text(encoding="ascii")
            self.assertEqual(checksum, f"{result.package.sha256}  {package.name}\n")  # type: ignore[union-attr]
            self.assertEqual(events[0], "job_started")
            self.assertEqual(events[-1], "job_completed")
            self.assertEqual(events.count("item_completed"), 2)

    def test_packages_are_byte_for_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            first = root / "first.zip"
            second = root / "second.zip"
            first_count, first_hash = build_deployment_package(book, first)
            second_count, second_hash = build_deployment_package(book, second)
            self.assertEqual(first_count, second_count)
            self.assertEqual(first_hash, second_hash)
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_failure_removes_staging_and_leaves_every_source_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root, bundle_version="release-seven")
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"video")
            before = hash_tree(root)
            output = root / "published"
            package = root / "book.zip"

            with self.assertRaises(PublishFailedError):
                publish_adt(
                    videos,
                    book=book,
                    output=output,
                    package=package,
                    validate_media=False,
                )

            self.assertFalse(output.exists())
            self.assertFalse(package.exists())
            self.assertFalse(Path(str(package) + ".sha256").exists())
            self.assertEqual(hash_tree(root), before)
            self.assertEqual(list(root.glob(".*.adt-publish-*.tmp")), [])

    def test_video_names_allow_sparse_pages_but_reject_bad_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            videos = Path(temporary)
            (videos / "page_1.mp4").write_bytes(b"one")
            (videos / "page_3.mp4").write_bytes(b"three")
            items = discover_page_videos(videos, page_count=3)
            self.assertEqual([item.key for item in items], ["video-1", "video-3"])
            (videos / "page_4.mp4").write_bytes(b"four")
            with self.assertRaises(InvalidInputError):
                discover_page_videos(videos, page_count=3)

    def test_package_validation_rejects_undeclared_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            package = root / "book.zip"
            build_deployment_package(book, package)
            with zipfile.ZipFile(package, "a") as archive:
                archive.writestr("undeclared.txt", "bad")
            with self.assertRaises(PublishFailedError):
                validate_deployment_package(package)


if __name__ == "__main__":
    unittest.main()
