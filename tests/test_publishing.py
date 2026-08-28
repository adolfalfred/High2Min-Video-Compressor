from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from adt_video_publisher import publishing
from adt_video_publisher.errors import (
    InvalidInputError,
    PublishFailedError,
    PublishingInterruptedError,
    ResourceLimitError,
)
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
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
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


def omit_manifest_files(root: Path, omitted: set[str]) -> None:
    manifest = root / "imsmanifest.xml"
    tree = ET.parse(manifest)
    manifest_root = tree.getroot()
    for element in list(manifest_root.iter(f"{{{IMS_NAMESPACE}}}file")):
        if element.get("href") not in omitted:
            continue
        parent = next(
            candidate for candidate in manifest_root.iter() if element in list(candidate)
        )
        parent.remove(element)
    tree.write(manifest, encoding="utf-8", xml_declaration=True)


def add_manifest_files(root: Path, additions: set[str]) -> None:
    manifest = root / "imsmanifest.xml"
    tree = ET.parse(manifest)
    manifest_root = tree.getroot()
    resource = next(manifest_root.iter(f"{{{IMS_NAMESPACE}}}resource"))
    for relative in sorted(additions):
        ET.SubElement(resource, f"{{{IMS_NAMESPACE}}}file", {"href": relative})
    tree.write(manifest, encoding="utf-8", xml_declaration=True)


def make_book(root: Path, *, bundle_version: object = "7", existing_video: bool = False) -> Path:
    book = root / "book"
    (book / "assets").mkdir(parents=True)
    language = book / "content" / "i18n" / "en-GB"
    (language / "video").mkdir(parents=True)
    runtime_tag = '<script src="./assets/base.bundle.local.js?v=7"></script>'
    (book / "index.html").write_text(f"<head><title>Test</title></head>{runtime_tag}", encoding="utf-8")
    (book / "pg002.html").write_text(f"<head></head>page two{runtime_tag}", encoding="utf-8")
    (book / "pg003.html").write_text(f"<head></head>page three{runtime_tag}", encoding="utf-8")
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
    existing_mappings = {"video-2": "page_2.mp4"} if existing_video else {}
    (language / "videos.json").write_text(json.dumps(existing_mappings), encoding="utf-8")
    if existing_video:
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
        raise TypeError("offline preloader INLINE payload is not an object")
    return payload


class PublishingTests(unittest.TestCase):
    def test_inline_scanner_handles_const_spacing_and_braces_inside_strings(self) -> None:
        source = 'const INLINE =  {"./index.html":"a } brace and \\\"quote\\\""};\nlet BASE_DIR="";'
        start, end = publishing._inline_json_span(source)
        self.assertEqual(
            json.loads(source[start:end]),
            {"./index.html": 'a } brace and "quote"'},
        )

    def test_manifest_patch_preserves_comments_order_and_unrelated_attributes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            manifest = book / "imsmanifest.xml"
            source = manifest.read_text(encoding="utf-8")
            source = source.replace(
                "<resources>",
                '<resources data-preserve="yes">\n    <!-- authored manifest comment -->',
            )
            manifest.write_text(source, encoding="utf-8", newline="\r\n")
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"replacement")

            publish_adt(videos, book=book, in_place=True, validate_media=False)

            updated = manifest.read_text(encoding="utf-8")
            self.assertIn('data-preserve="yes"', updated)
            self.assertIn("<!-- authored manifest comment -->", updated)
            self.assertLess(updated.index('href="index.html"'), updated.index('href="pg002.html"'))

    def test_cache_query_preserves_other_parameters_and_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            index = book / "index.html"
            index.write_text(
                '<head></head><script src="./assets/offline-preloader.js?theme=dark&v=7#boot"></script>'
                '<script src="./assets/base.bundle.local.js?theme=dark#reader"></script>',
                encoding="utf-8",
            )
            (book / "assets" / "offline-preloader.js").write_text(
                'const INLINE={"./index.html":"old"}; const BASE_DIR="";',
                encoding="utf-8",
            )
            write_manifest(book)
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"replacement")

            publish_adt(videos, book=book, in_place=True, validate_media=False)

            updated = index.read_text(encoding="utf-8")
            self.assertIn("offline-preloader.js?theme=dark&v=8#boot", updated)
            self.assertIn("base.bundle.local.js?theme=dark&v=8#reader", updated)

    def test_merge_mode_preserves_existing_mappings_and_unrelated_video_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root, existing_video=True)
            marker = book / "content" / "i18n" / "en-GB" / "video" / "notes.txt"
            marker.write_text("preserve me", encoding="utf-8")
            videos = root / "compressed"
            videos.mkdir()
            (videos / "lesson 1.mp4").write_bytes(b"replacement")

            publish_adt(videos, book=book, in_place=True, validate_media=False)

            mappings = json.loads((book / "content" / "i18n" / "en-GB" / "videos.json").read_text())
            self.assertEqual(mappings, {"video-1": "page_1.mp4", "video-2": "page_2.mp4"})
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve me")

    def test_replace_mode_requires_explicit_confirmation_for_removals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root, existing_video=True)
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"replacement")
            before = hash_tree(book)

            with self.assertRaisesRegex(InvalidInputError, "confirm_removals"):
                publish_adt(
                    videos,
                    book=book,
                    in_place=True,
                    mode="replace",
                    validate_media=False,
                )

            self.assertEqual(hash_tree(book), before)

    def test_concurrent_target_edit_aborts_before_overwriting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"replacement")
            actual_sync = publishing._synchronize_offline_preloader

            def introduce_concurrent_edit(*args: object, **kwargs: object) -> tuple[Path, ...]:
                result = actual_sync(*args, **kwargs)  # type: ignore[arg-type]
                (book / "assets" / "config.json").write_text("concurrent edit", encoding="utf-8")
                return result

            with patch(
                "adt_video_publisher.publishing._synchronize_offline_preloader",
                side_effect=introduce_concurrent_edit,
            ), self.assertRaisesRegex(PublishFailedError, "Concurrent edit"):
                publish_adt(videos, book=book, in_place=True, validate_media=False)

            self.assertEqual((book / "assets" / "config.json").read_text(), "concurrent edit")
            self.assertFalse((book / "content" / "i18n" / "en-GB" / "video" / "page_1.mp4").exists())
            self.assertEqual(list(root.glob(".*.adt-publish-*")), [])

    def test_inactive_runtime_and_repository_zip_are_byte_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            inactive = book / "assets" / "base.bundle.min.js"
            inactive.write_bytes(b"unknown inactive runtime")
            inactive_page = book / "inactive-section.html"
            inactive_page.write_bytes(
                b'<script src="./assets/base.bundle.local.js?v=7"></script>inactive'
            )
            archive = book / "legacy-scrum.zip"
            archive.write_bytes(b"never touch this package")
            write_manifest(book)
            before_runtime = inactive.read_bytes()
            before_page = inactive_page.read_bytes()
            before_zip = archive.read_bytes()
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"replacement")

            publish_adt(videos, book=book, in_place=True, validate_media=False)

            self.assertEqual(inactive.read_bytes(), before_runtime)
            self.assertEqual(inactive_page.read_bytes(), before_page)
            self.assertEqual(archive.read_bytes(), before_zip)

    def test_staged_video_copy_does_not_force_per_file_disk_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            destination = root / "stage" / "page_1.mp4"
            source.write_bytes(b"video-data" * 200_000)
            reporter = publishing._PublishReporter("job", None, None, None)

            with patch("adt_video_publisher.publishing.os.fsync") as forced_sync:
                digest = publishing._copy_and_hash_video(
                    source,
                    destination,
                    reporter=reporter,
                    completed_bytes=0,
                    total_bytes=source.stat().st_size,
                )

            forced_sync.assert_not_called()
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertEqual(digest, hashlib.sha256(source.read_bytes()).hexdigest())

    def test_transaction_journal_keeps_durable_disk_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = Path(temporary) / "transaction.json"

            with patch("adt_video_publisher.publishing.os.fsync") as durable_sync:
                publishing._write_json_atomic(journal, {"status": "committing"})

            durable_sync.assert_called_once()
            self.assertEqual(
                json.loads(journal.read_text(encoding="utf-8")),
                {"status": "committing"},
            )

    def test_in_place_publish_stages_only_files_that_can_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            static = book / "assets" / "large-static-data.bin"
            static.write_bytes(b"unchanged" * 1000)
            for index in range(300):
                path = book / "assets" / "static" / f"item-{index:04d}.bin"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"item-{index}".encode("ascii"))
            write_manifest(book)
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"replacement")
            copied_sources: list[Path] = []
            actual_copy = publishing._copy_file

            def record_copy(source: Path, destination: Path) -> None:
                copied_sources.append(source)
                actual_copy(source, destination)

            with patch("adt_video_publisher.publishing._copy_file", side_effect=record_copy):
                publish_adt(videos, book=book, in_place=True, validate_media=False)

            self.assertNotIn(static, copied_sources)
            self.assertFalse(any("static" in path.parts for path in copied_sources))
            self.assertEqual(static.read_bytes(), b"unchanged" * 1000)
            self.assertIn("assets/large-static-data.bin", declared_manifest_files(book / "imsmanifest.xml"))

    def test_publish_progress_is_monotonic_and_reports_real_final_phases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"replacement" * 200_000)
            phases: list[tuple[str, float]] = []

            def progress(_job: str, event: str, payload: dict[str, object]) -> None:
                if event == "item_progress" and payload.get("kind") == "publishing":
                    phases.append((str(payload["phase"]), float(payload["percent"])))

            publish_adt(
                videos,
                book=book,
                in_place=True,
                validate_media=False,
                progress_callback=progress,
            )

            percentages = [percent for _phase, percent in phases]
            self.assertEqual(percentages, sorted(percentages))
            self.assertEqual(percentages[-1], 100)
            names = {phase for phase, _percent in phases}
            self.assertTrue({"preflight", "staging", "commit", "final_validation", "cleanup", "completed"}.issubset(names))

    def test_publish_can_be_cancelled_before_repository_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"replacement" * 200_000)
            before = hash_tree(book)
            cancellation = threading.Event()

            def progress(_job: str, event: str, payload: dict[str, object]) -> None:
                if (
                    event == "item_progress"
                    and payload.get("kind") == "publishing"
                    and float(payload.get("percent", 0)) >= 25
                ):
                    cancellation.set()

            with self.assertRaises(PublishingInterruptedError):
                publish_adt(
                    videos,
                    book=book,
                    in_place=True,
                    validate_media=False,
                    cancel_event=cancellation,
                    progress_callback=progress,
                )

            self.assertEqual(hash_tree(book), before)
            self.assertEqual(list(root.glob(".*.adt-publish-*")), [])

    def test_publish_recovers_an_interrupted_rename_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            original_config = (book / "assets" / "config.json").read_bytes()
            stage = root / f".{book.name}.adt-publish-recovery.tmp"
            backup = root / f".{book.name}.adt-publish-recovery.backup"
            (backup / "assets").mkdir(parents=True)
            stage.mkdir()
            (book / "assets" / "config.json").replace(backup / "assets" / "config.json")
            (backup / "transaction.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "transaction_id": "recovery",
                        "status": "committing",
                        "book": str(book.resolve()),
                        "stage": str(stage.resolve()),
                        "language": "en-GB",
                        "targets": [
                            {"relative": "assets/config.json", "had_original": True}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"replacement")

            publish_adt(videos, book=book, in_place=True, validate_media=False)

            self.assertNotEqual((book / "assets" / "config.json").read_bytes(), original_config)
            self.assertFalse(stage.exists())
            self.assertFalse(backup.exists())
            self.assertEqual(validate_adt_website(book, allow_unmanifested=True)["video_count"], 1)

    def test_publish_writes_durable_phase_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"replacement")
            log = root / "logs" / "publish.jsonl"

            result = publish_adt(
                videos,
                book=book,
                in_place=True,
                validate_media=False,
                diagnostic_log=log,
            )

            self.assertEqual(result.diagnostic_log, log.resolve())
            events = [json.loads(line)["event"] for line in log.read_text(encoding="utf-8").splitlines()]
            self.assertIn("phase", events)
            self.assertEqual(events[-1], "job_completed")

    def test_permission_preflight_fails_before_repository_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"replacement")
            before = hash_tree(book)

            with patch(
                "adt_video_publisher.publishing._atomic_write_probe",
                side_effect=PublishFailedError("simulated read-only repository"),
            ), self.assertRaisesRegex(PublishFailedError, "read-only"):
                publish_adt(videos, book=book, in_place=True, validate_media=False)

            self.assertEqual(hash_tree(book), before)

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
            ), self.assertRaises(ResourceLimitError) as raised:
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

    def test_in_place_publish_recovers_active_pages_omitted_from_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            navigation = book / "content" / "navigation" / "nav.html"
            navigation.parent.mkdir(parents=True)
            navigation.write_text("<nav>Mathematics navigation</nav>", encoding="utf-8")
            preloader = book / "assets" / "offline-preloader.js"
            preloader.write_text(
                "var INLINE = "
                + json.dumps(
                    {
                        "./assets/config.json": json.loads(
                            (book / "assets" / "config.json").read_text(encoding="utf-8")
                        ),
                        "./content/i18n/en-GB/videos.json": {},
                        "./content/navigation/nav.html": "stale navigation",
                    },
                    separators=(",", ":"),
                )
                + ";\n  var BASE_DIR = \"\";\n",
                encoding="utf-8",
            )
            write_manifest(book)
            omitted = {
                "pg002.html",
                "pg003.html",
                "content/navigation/nav.html",
            }
            pages_path = book / "content" / "pages.json"
            pages = json.loads(pages_path.read_text(encoding="utf-8"))
            pages[1]["href"] = "pg002.html?reader=32#page"
            pages_path.write_text(json.dumps(pages), encoding="utf-8")
            omit_manifest_files(book, omitted)
            add_manifest_files(book, {"removed-legacy-page.html"})
            self.assertTrue(
                omitted.isdisjoint(declared_manifest_files(book / "imsmanifest.xml"))
            )
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"replacement")

            publish_adt(videos, book=book, in_place=True, validate_media=False)

            self.assertTrue(
                omitted.issubset(declared_manifest_files(book / "imsmanifest.xml"))
            )
            self.assertNotIn(
                "removed-legacy-page.html",
                declared_manifest_files(book / "imsmanifest.xml"),
            )
            for relative in {"pg002.html", "pg003.html"}:
                source = (book / relative).read_text(encoding="utf-8")
                self.assertIn("media-playback-independence.js", source)
                self.assertIn("sign-language-video.js", source)
            self.assertEqual(
                read_offline_inline(preloader)["./content/navigation/nav.html"],
                navigation.read_text(encoding="utf-8"),
            )

    def test_copy_publish_recovers_active_pages_without_changing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            omit_manifest_files(book, {"pg002.html"})
            add_manifest_files(book, {"removed-legacy-page.html"})
            source_before = hash_tree(book)
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"replacement")
            output = root / "published"

            publish_adt(
                videos,
                book=book,
                output=output,
                validate_media=False,
            )

            self.assertEqual(hash_tree(book), source_before)
            self.assertTrue((output / "pg002.html").is_file())
            self.assertIn(
                "pg002.html",
                declared_manifest_files(output / "imsmanifest.xml"),
            )
            self.assertNotIn(
                "removed-legacy-page.html",
                declared_manifest_files(output / "imsmanifest.xml"),
            )
            self.assertEqual(
                validate_adt_website(output)["page_count"],
                3,
            )

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

    def test_publish_preserves_crlf_and_bom_in_offline_embedded_html(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            navigation = book / "content" / "navigation" / "nav.html"
            navigation.parent.mkdir(parents=True)
            navigation_source = "<!doctype html>\r\n<nav>\r\n  Utamaduni\r\n</nav>\r\n"
            navigation.write_bytes(b"\xef\xbb\xbf" + navigation_source.encode("utf-8"))
            preloader = book / "assets" / "offline-preloader.js"
            preloader.write_text(
                "var INLINE = "
                + json.dumps(
                    {
                        "./assets/config.json": json.loads(
                            (book / "assets" / "config.json").read_text(encoding="utf-8")
                        ),
                        "./content/i18n/en-GB/videos.json": {},
                        "./content/navigation/nav.html": "stale",
                    },
                    separators=(",", ":"),
                )
                + ";\n  var BASE_DIR = \"\";\n",
                encoding="utf-8",
            )
            write_manifest(book)
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"replacement")

            publish_adt(videos, book=book, in_place=True, validate_media=False)

            inline = read_offline_inline(preloader)
            self.assertEqual(
                inline["./content/navigation/nav.html"],
                navigation_source,
            )
            self.assertTrue(navigation.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertIn(b"\r\n", navigation.read_bytes())

    def test_offline_mismatch_is_rejected_before_repository_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root)
            index = book / "index.html"
            preloader = book / "assets" / "offline-preloader.js"
            preloader.write_text(
                "var INLINE = "
                + json.dumps(
                    {
                        "./assets/config.json": json.loads(
                            (book / "assets" / "config.json").read_text(encoding="utf-8")
                        ),
                        "./content/i18n/en-GB/videos.json": {},
                        "./index.html": index.read_text(encoding="utf-8"),
                    },
                    separators=(",", ":"),
                )
                + ";\n  var BASE_DIR = \"\";\n",
                encoding="utf-8",
            )
            write_manifest(book)
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"replacement")
            before = hash_tree(book)
            actual_sync = publishing._synchronize_offline_preloader

            def corrupt_staged_inline(*args: object, **kwargs: object) -> tuple[Path, ...]:
                changed = actual_sync(*args, **kwargs)  # type: ignore[arg-type]
                staged_book = args[0]
                staged_preloader = staged_book / "assets" / "offline-preloader.js"  # type: ignore[operator]
                source = staged_preloader.read_text(encoding="utf-8")
                staged_preloader.write_text(
                    source.replace("<title>Test</title>", "<title>Stale</title>"),
                    encoding="utf-8",
                )
                return changed

            with patch(
                "adt_video_publisher.publishing._synchronize_offline_preloader",
                side_effect=corrupt_staged_inline,
            ), patch(
                "adt_video_publisher.publishing._commit_in_place"
            ) as commit, self.assertRaisesRegex(PublishFailedError, "stale embedded HTML"):
                publish_adt(videos, book=book, in_place=True, validate_media=False)

            commit.assert_not_called()
            self.assertEqual(hash_tree(book), before)
            self.assertEqual(list(root.glob(".*.adt-publish-*")), [])

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
            book = make_book(root, existing_video=True)
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
                mode="replace",
                confirm_removals=True,
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
            self.assertIn('onPlay:()=>a("sign-language")', runtime)
            self.assertTrue((book / "assets" / "media-playback-independence.js").is_file())
            self.assertTrue((book / "assets" / "sign-language-video.js").is_file())
            self.assertTrue((book / "assets" / "sign-language-video.css").is_file())
            updated_index = (book / "index.html").read_text(encoding="utf-8")
            self.assertLess(updated_index.index("media-playback-independence.js"), updated_index.index("base.bundle.local.js"))
            self.assertLess(updated_index.index("base.bundle.local.js"), updated_index.index("sign-language-video.js"))
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
                if validation_calls == 1:
                    raise PublishFailedError("simulated final validation failure")
                return actual_validator(*args, **kwargs)  # type: ignore[arg-type]

            with patch(
                "adt_video_publisher.publishing.validate_adt_website",
                side_effect=fail_final_validation,
            ), self.assertRaises(PublishFailedError):
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
            book = make_book(root, existing_video=True)
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
                mode="replace",
                confirm_removals=True,
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
            self.assertIn('onPlay:()=>a("sign-language")', published_runtime)
            self.assertTrue((output / "assets" / "media-playback-independence.js").is_file())
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

    def test_nonstandard_bundle_version_uses_dedicated_cache_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = make_book(root, bundle_version="release-seven")
            videos = root / "compressed"
            videos.mkdir()
            (videos / "page_1.mp4").write_bytes(b"video")
            before = hash_tree(book)
            output = root / "published"
            package = root / "book.zip"

            result = publish_adt(
                videos,
                book=book,
                output=output,
                package=package,
                validate_media=False,
            )

            self.assertEqual(result.bundle_version, "h2m-1")
            config = json.loads((output / "assets" / "config.json").read_text())
            self.assertEqual(config["bundleVersion"], "release-seven")
            self.assertEqual(config["high2minCacheVersion"], 1)
            self.assertTrue(package.exists())
            self.assertTrue(Path(str(package) + ".sha256").exists())
            self.assertEqual(hash_tree(book), before)
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
