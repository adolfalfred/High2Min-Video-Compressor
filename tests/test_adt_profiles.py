from __future__ import annotations

import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from importlib.resources import files
from pathlib import Path

from adt_video_publisher.adt_planning import analyze_adt_publish
from adt_video_publisher.publishing import (
    ADLCP_NAMESPACE,
    IMS_NAMESPACE,
    publish_adt,
    validate_adt_website,
)

PROFILES = json.loads(
    (Path(__file__).parent / "fixtures" / "adt_profiles.json").read_text(encoding="utf-8")
)


def make_profile(root: Path, profile: dict[str, object]) -> Path:
    book = root / str(profile["name"])
    assets = book / "assets"
    language_code = str(profile["language"])
    language = book / "content" / "i18n" / language_code
    (language / "video").mkdir(parents=True)
    assets.mkdir(parents=True)
    hrefs = ["index.html", "pg002_sec001_pg003_sec001.html" if profile["joined_href"] else "pg002.html", "pg003.html"]
    runtime_tag = '<script src="./assets/base.bundle.local.js?v=1"></script>'
    helper_tags = ""
    if profile["helpers"] == "all":
        helper_tags = (
            '<link rel="stylesheet" href="./assets/sign-language-video.css">'
            '<script src="./assets/media-playback-independence.js"></script>'
            + runtime_tag
            + '<script src="./assets/sign-language-video.js"></script>'
        )
    elif profile["helpers"] == "partial":
        helper_tags = '<script src="./assets/media-playback-independence.js"></script>' + runtime_tag
    else:
        helper_tags = runtime_tag
    for position, href in enumerate(hrefs, start=1):
        (book / href).write_text(
            f"<!doctype html><html><head><title>Page {position}</title></head><body><main>Profile text {position}</main>{helper_tags}</body></html>",
            encoding="utf-8",
        )
    for helper in ("media-playback-independence.js", "sign-language-video.js", "sign-language-video.css"):
        if profile["helpers"] == "all" or (profile["helpers"] == "partial" and helper == "media-playback-independence.js"):
            (assets / helper).write_bytes(files("adt_video_publisher").joinpath("assets", helper).read_bytes())
    (assets / "base.bundle.local.js").write_text(
        'const signLanguage=true,readAloud=true,hand="sign-language-label",voice="activate-tts-label";',
        encoding="utf-8",
    )
    if profile["inactive_bundle"]:
        (assets / "base.bundle.min.js").write_bytes(b"inactive unknown bundle")
    config: dict[str, object] = {
        "title": profile["name"],
        "languages": {"available": [language_code], "default": language_code},
        "features": {"signLanguage": bool(profile["existing_video"]), "readAloud": True},
    }
    if profile["bundle_version"] is not None:
        config["bundleVersion"] = profile["bundle_version"]
    (assets / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    (book / "content" / "pages.json").write_text(
        json.dumps([{"section_id": f"p{index}", "href": href} for index, href in enumerate(hrefs, start=1)]),
        encoding="utf-8",
    )
    mappings = {"video-2": "page_2.mp4"} if profile["existing_video"] else {}
    (language / "videos.json").write_text(json.dumps(mappings), encoding="utf-8")
    if profile["existing_video"]:
        (language / "video" / "page_2.mp4").write_bytes(b"existing")
    inline = {
        "./assets/config.json": config,
        f"./content/i18n/{language_code}/videos.json": mappings,
        "./index.html": (book / "index.html").read_text(encoding="utf-8"),
    }
    declaration = str(profile["offline"])
    (assets / "offline-preloader.js").write_text(
        f'(function(){{ {declaration} INLINE = {json.dumps(inline, separators=(",", ":"))};\nconst BASE_DIR=""; }}());\n',
        encoding="utf-8",
    )
    for index in range(int(profile["extra_files"])):
        path = assets / "static" / f"fixture-{index:04d}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(index), encoding="ascii")

    ET.register_namespace("", IMS_NAMESPACE)
    ET.register_namespace("adlcp", ADLCP_NAMESPACE)
    manifest = ET.Element(f"{{{IMS_NAMESPACE}}}manifest", {"identifier": str(profile["name"])})
    resources = ET.SubElement(manifest, f"{{{IMS_NAMESPACE}}}resources")
    if profile["manifest_comment"]:
        resources.append(ET.Comment(" preserve authored profile metadata "))
    resource = ET.SubElement(resources, f"{{{IMS_NAMESPACE}}}resource", {"href": "index.html"})
    for path in sorted(item for item in book.rglob("*") if item.is_file()):
        ET.SubElement(resource, f"{{{IMS_NAMESPACE}}}file", {"href": path.relative_to(book).as_posix()})
    ET.ElementTree(manifest).write(book / "imsmanifest.xml", encoding="utf-8", xml_declaration=True)
    return book


class AdtProfileRegressionTests(unittest.TestCase):
    def test_all_ten_adt_profiles_analyze_publish_and_validate(self) -> None:
        self.assertEqual(len(PROFILES), 10)
        for profile in PROFILES:
            with self.subTest(profile=profile["name"]), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                book = make_profile(root, profile)
                inactive = book / "assets" / "base.bundle.min.js"
                inactive_before = inactive.read_bytes() if inactive.is_file() else None
                videos = root / "compressed"
                videos.mkdir()
                (videos / "signed lesson 001.mp4").write_bytes(b"replacement")

                plan = analyze_adt_publish(videos, book=book)
                self.assertTrue(plan.ready, plan.blockers)
                self.assertEqual(plan.videos[0].page_href, "index.html")
                publish_adt(videos, book=book, in_place=True, validate_media=False)

                validation = validate_adt_website(book, language=str(profile["language"]), allow_unmanifested=True)
                self.assertEqual(validation["page_count"], 3)
                expected_videos = 2 if profile["existing_video"] else 1
                self.assertEqual(validation["video_count"], expected_videos)
                for helper in ("media-playback-independence.js", "sign-language-video.js", "sign-language-video.css"):
                    self.assertTrue((book / "assets" / helper).is_file())
                if inactive_before is not None:
                    self.assertEqual(inactive.read_bytes(), inactive_before)
                self.assertEqual(list(root.glob(".*.adt-publish-*")), [])


if __name__ == "__main__":
    unittest.main()
