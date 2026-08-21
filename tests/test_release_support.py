from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from release.support import (
    ReleaseValidationError,
    build_release_archive,
    platform_tag,
    verify_release_archive,
    verify_release_directory,
    write_release_manifest,
)
from release.build_release import GUI_NAME, MACOS_APP_NAME, _windows_pe_subsystem, _write_launchers


def make_release(parent: Path) -> Path:
    root = parent / "High2Min-Video-Compressor-test"
    (root / "_internal").mkdir(parents=True)
    (root / "high2min").write_bytes(b"executable")
    (root / "_internal" / "schema.json").write_text("{}\n", encoding="utf-8")
    write_release_manifest(
        root,
        product_version="1.2.3",
        target="linux-x86_64",
        dependencies={"python": "3.12.0", "ffmpeg": "test"},
    )
    return root


class ReleaseSupportTests(unittest.TestCase):
    def test_windows_release_no_longer_uses_a_vbs_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_launchers(root, "windows-x86_64")
            self.assertEqual(list(root.iterdir()), [])

    def test_macos_launchers_use_executables_inside_the_native_app(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_launchers(root, "macos-arm64")
            ui = (root / "high2min-ui").read_text(encoding="utf-8")
            cli = (root / "high2min").read_text(encoding="utf-8")
            self.assertIn(f"{MACOS_APP_NAME}/Contents/MacOS/{GUI_NAME}", ui)
            self.assertIn(f"{MACOS_APP_NAME}/Contents/MacOS/high2min", cli)
            self.assertNotIn("python", ui.casefold())
            self.assertNotIn("python", cli.casefold())

    def test_windows_pe_subsystem_is_read_from_the_optional_header(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "app.exe"
            value = bytearray(256)
            value[:2] = b"MZ"
            value[0x3C:0x40] = (128).to_bytes(4, "little")
            value[128:132] = b"PE\0\0"
            value[128 + 24 + 68:128 + 24 + 70] = (2).to_bytes(2, "little")
            executable.write_bytes(value)
            self.assertEqual(_windows_pe_subsystem(executable), 2)

    def test_platform_tags_are_stable(self) -> None:
        with patch("platform.system", return_value="Windows"), patch(
            "platform.machine", return_value="AMD64"
        ):
            self.assertEqual(platform_tag(), "windows-x86_64")
        with patch("platform.system", return_value="Darwin"), patch(
            "platform.machine", return_value="arm64"
        ):
            self.assertEqual(platform_tag(), "macos-arm64")

    def test_manifest_detects_changed_or_extra_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_release(Path(temporary))
            self.assertEqual(verify_release_directory(root)["product_version"], "1.2.3")
            (root / "high2min").write_bytes(b"changed")
            with self.assertRaises(ReleaseValidationError):
                verify_release_directory(root)

    def test_windows_zip_is_deterministic_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = make_release(parent)
            first = parent / "first.zip"
            second = parent / "second.zip"
            build_release_archive(root, first, target="windows-x86_64")
            build_release_archive(root, second, target="windows-x86_64")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(verify_release_archive(first)["target"], "linux-x86_64")

    def test_posix_tar_is_deterministic_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = make_release(parent)
            first = parent / "first.tar.gz"
            second = parent / "second.tar.gz"
            build_release_archive(root, first, target="linux-x86_64")
            build_release_archive(root, second, target="linux-x86_64")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(verify_release_archive(first)["product_version"], "1.2.3")

    def test_archive_rejects_an_undeclared_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = make_release(parent)
            archive = parent / "release.zip"
            build_release_archive(root, archive, target="windows-x86_64")
            with zipfile.ZipFile(archive, "a") as output:
                output.writestr(f"{root.name}/undeclared.txt", "bad")
            with self.assertRaises(ReleaseValidationError):
                verify_release_archive(archive)


if __name__ == "__main__":
    unittest.main()
