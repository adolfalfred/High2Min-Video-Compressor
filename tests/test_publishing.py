from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import xml.etree.ElementTree as ET

from adt_video_publisher.errors import InvalidInputError, PublishFailedError, ResourceLimitError
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
    (book / "assets" / "base.bundle.local.js").write_text(
        (
            'const hand="sign-language-label",feature="signLanguage";'
            'const voice="activate-tts-label",read="readAloud";'
            '(0,ui.useEffect)(()=>{n==="tts"&&d.current?.pause()},[n]);'
            'const video={onPlay:()=>a("sign-language")};'
            '(0,ui.useEffect)(()=>{l==="sign-language"&&(R(),r(!1),s(0),b(!1))},[l,R,r,s,b]);'
            'audio.play().then(()=>{u("tts"),r(!0)});'
            'audio.resume().then(()=>{u("tts"),r(!0),document.querySelectorAll("video").forEach(e=>{e.paused&&e.play().catch(()=>{})})});'
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


def read_offline_inline(preloader: Path) -> dict[str, object]:
    source = preloader.read_text(encoding="utf-8")
    marker = "var INLINE = "
    start = source.index(marker) + len(marker)
    end = source.index(";\n  var BASE_DIR", start)
    payload = json.loads(source[start:end])
    if not isinstance(payload, dict):
        raise AssertionError("offline preloader INLINE payload is not an object")
    return payload


class PublishingTests(unittest.TestCase):
    def test_publish_low_disk_error_uses_megabytes_without_changing_book(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"compressed-one")
            before = hash_tree(book)

            with patch(
                "adt_video_publisher.publishing.shutil.disk_usage",
                return_value=SimpleNamespace(free=1024),
            ):
                with self.assertRaises(ResourceLimitError) as raised:
                    publish_adt(
                        videos,
                        book=book,
                        in_place=True,
                        validate_media=False,
                    )

            message = str(raised.exception)
            self.assertIn("MB", message)
            self.assertNotIn("bytes", message)
            self.assertEqual(hash_tree(book), before)

    def test_publish_recovers_essential_files_omitted_from_legacy_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            manifest_path = book / "imsmanifest.xml"
            tree = ET.parse(manifest_path)
            manifest_root = tree.getroot()
            omitted = {"assets/base.bundle.local.js", "content/pages.json"}
            for element in list(manifest_root.iter(f"{{{IMS_NAMESPACE}}}file")):
                if element.get("href") in omitted:
                    parent = next(
                        candidate
                        for candidate in manifest_root.iter()
                        if element in list(candidate)
                    )
                    parent.remove(element)
            tree.write(manifest_path, encoding="utf-8", xml_declaration=True)
            self.assertTrue(omitted.isdisjoint(declared_manifest_files(manifest_path)))
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"compressed-one")

            publish_adt(
                videos,
                book=book,
                in_place=True,
                validate_media=False,
            )

            self.assertTrue((book / "assets" / "base.bundle.local.js").is_file())
            self.assertTrue(omitted.issubset(declared_manifest_files(manifest_path)))

    def test_publish_refreshes_offline_preloader_settings_mappings_and_cache_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            config_path = book / "assets" / "config.json"
            stale_config = json.loads(config_path.read_text(encoding="utf-8"))
            stale_config["features"] = {"signLanguage": False, "readAloud": False}
            config_path.write_text(json.dumps(stale_config), encoding="utf-8")
            index = book / "index.html"
            index.write_text(
                '<title>Test</title>'
                '<script src="./assets/offline-preloader.js?v=7"></script>'
                '<script src="./assets/base.bundle.local.js"></script>',
                encoding="utf-8",
            )
            preloader = book / "assets" / "offline-preloader.js"
            preloader.write_text(
                "// generated offline resources\n"
                "(function () {\n"
                "  var INLINE = "
                + json.dumps(
                    {
                        "./assets/config.json": stale_config,
                        "./content/i18n/en-GB/videos.json": {},
                        "./index.html": index.read_text(encoding="utf-8"),
                    },
                    separators=(",", ":"),
                )
                + ";\n  var BASE_DIR = \"\";\n})();\n",
                encoding="utf-8",
            )
            write_manifest(book)
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"compressed-one")
            (videos / "page_3.mp4").write_bytes(b"compressed-three")

            publish_adt(
                videos,
                book=book,
                in_place=True,
                validate_media=False,
            )

            updated_index = index.read_text(encoding="utf-8")
            self.assertIn("offline-preloader.js?v=8", updated_index)
            self.assertNotIn("offline-preloader.js?v=7", updated_index)
            self.assertIn("base.bundle.local.js?v=8", updated_index)
            inline = read_offline_inline(preloader)
            embedded_config = inline["./assets/config.json"]
            self.assertIsInstance(embedded_config, dict)
            self.assertEqual(embedded_config["bundleVersion"], "8")
            self.assertTrue(embedded_config["features"]["signLanguage"])
            self.assertTrue(embedded_config["features"]["readAloud"])
            self.assertEqual(
                inline["./content/i18n/en-GB/videos.json"],
                {"video-1": "page_1.mp4", "video-3": "page_3.mp4"},
            )
            self.assertEqual(inline["./index.html"], updated_index)

    def test_publish_stops_safely_when_offline_preloader_is_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            preloader = book / "assets" / "offline-preloader.js"
            preloader.write_text("var INLINE = broken;\n", encoding="utf-8")
            write_manifest(book)
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"replacement")
            before = hash_tree(book)

            with self.assertRaises(PublishFailedError):
                publish_adt(
                    videos,
                    book=book,
                    in_place=True,
                    validate_media=False,
                )

            self.assertEqual(hash_tree(book), before)
            self.assertEqual(list(root.glob(".*.adt-publish-*")), [])

    def test_in_place_publish_updates_repo_and_never_touches_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"compressed-one")
            (videos / "page_3.mp4").write_bytes(b"compressed-three")
            existing_zip = root / "deployment.zip"
            existing_zip.write_bytes(b"leave-this-zip-alone")

            result = publish_adt(
                videos,
                book=book,
                in_place=True,
                validate_media=False,
            )

            self.assertEqual(result.output_book, book.resolve())
            self.assertIsNone(result.package)
            self.assertEqual(existing_zip.read_bytes(), b"leave-this-zip-alone")
            self.assertTrue((book / "AGENTS.md").is_file())
            config = json.loads((book / "assets" / "config.json").read_text(encoding="utf-8"))
            self.assertTrue(config["features"]["signLanguage"])
            self.assertTrue(config["features"]["readAloud"])
            runtime = (book / "assets" / "base.bundle.local.js").read_text(encoding="utf-8")
            self.assertIn('onPlay:()=>{}', runtime)
            self.assertNotIn('a("sign-language")', runtime)
            self.assertNotIn('n==="tts"&&d.current?.pause()', runtime)
            self.assertNotIn('l==="sign-language"&&(R(),r(!1),s(0),b(!1))', runtime)
            self.assertIn(
                'u("tts"),r(!0),document.querySelectorAll("video[autoplay]").forEach',
                runtime,
            )
            self.assertNotIn('document.querySelectorAll("video").forEach', runtime)
            mappings = json.loads(
                (book / "content" / "i18n" / "en-GB" / "videos.json").read_text()
            )
            self.assertEqual(mappings, {"video-1": "page_1.mp4", "video-3": "page_3.mp4"})
            self.assertFalse((book / "content" / "i18n" / "en-GB" / "video" / "page_2.mp4").exists())
            self.assertEqual(
                validate_adt_website(book, allow_unmanifested=True)["video_count"],
                2,
            )
            self.assertEqual(list(root.glob(".*.adt-publish-*")), [])

    def test_publish_stops_safely_when_runtime_has_no_hand_control(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            (book / "assets" / "base.bundle.local.js").write_text(
                "const runtimeWithoutSignLanguage = true;",
                encoding="utf-8",
            )
            write_manifest(book)
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"replacement")
            before = hash_tree(book)

            with self.assertRaises(PublishFailedError):
                publish_adt(
                    videos,
                    book=book,
                    in_place=True,
                    validate_media=False,
                )

            self.assertEqual(hash_tree(book), before)
            self.assertEqual(list(root.glob(".*.adt-publish-*")), [])

    def test_in_place_publish_rolls_back_if_final_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"replacement")
            before = hash_tree(book)
            actual_validator = validate_adt_website
            validation_calls = 0

            def fail_final_validation(*args: object, **kwargs: object) -> dict[str, object]:
                nonlocal validation_calls
                validation_calls += 1
                if validation_calls == 2:
                    raise PublishFailedError("simulated final validation failure")
                return actual_validator(*args, **kwargs)  # type: ignore[arg-type]

            with patch(
                "adt_video_publisher.publishing.validate_adt_website",
                side_effect=fail_final_validation,
            ):
                with self.assertRaises(PublishFailedError):
                    publish_adt(
                        videos,
                        book=book,
                        in_place=True,
                        validate_media=False,
                    )

            self.assertEqual(hash_tree(book), before)
            self.assertEqual(list(root.glob(".*.adt-publish-*")), [])

    def test_in_place_mode_rejects_zip_without_modifying_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"replacement")
            package = root / "existing.zip"
            package.write_bytes(b"original-package")

            with self.assertRaises(InvalidInputError):
                publish_adt(
                    videos,
                    book=book,
                    package=package,
                    in_place=True,
                    validate_media=False,
                )

            self.assertEqual(package.read_bytes(), b"original-package")

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
            published_runtime = (output / "assets" / "base.bundle.local.js").read_text(
                encoding="utf-8"
            )
            self.assertIn("onPlay:()=>{}", published_runtime)
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
