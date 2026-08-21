"""Verify a complete release set and generate its index and SHA256SUMS file."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from release.ci_release_artifact import _archives  # noqa: E402
from release.support import ReleaseValidationError, sha256_file, verify_release_archive  # noqa: E402


EXPECTED_TARGETS = {
    "windows-x86_64",
    "linux-x86_64",
    "macos-arm64",
    "macos-x86_64",
}


def generate(directory: Path) -> tuple[Path, Path, dict[str, object]]:
    artifacts: list[dict[str, object]] = []
    versions: set[str] = set()
    targets: set[str] = set()
    sums: list[str] = []
    for archive in _archives(directory):
        manifest = verify_release_archive(archive)
        if manifest.get("product") != "High2Min Video Compressor":
            raise ReleaseValidationError(f"Wrong product in {archive.name}.")
        version = str(manifest["product_version"])
        target = str(manifest["target"])
        if target in targets:
            raise ReleaseValidationError(f"Duplicate release target: {target}")
        versions.add(version)
        targets.add(target)
        digest = sha256_file(archive)
        checksum = Path(str(archive) + ".sha256")
        values = checksum.read_text(encoding="ascii").split()
        if len(values) < 2 or values[0] != digest or values[1] != archive.name:
            raise ReleaseValidationError(f"Checksum verification failed: {checksum.name}")
        sums.append(f"{digest}  {archive.name}")
        artifacts.append(
            {
                "target": target,
                "archive": archive.name,
                "size_bytes": archive.stat().st_size,
                "sha256": digest,
                "native_execution_verified": True,
                "ui_smoke_verified": True,
                "code_signed": False,
                "bundle_integrity_signature": "ad-hoc" if target.startswith("macos-") else "none",
            }
        )
    if versions == set() or len(versions) != 1:
        raise ReleaseValidationError(f"Expected one release version, found: {sorted(versions)}")
    if targets != EXPECTED_TARGETS:
        raise ReleaseValidationError(
            f"Release targets differ: missing={sorted(EXPECTED_TARGETS - targets)}, "
            f"unexpected={sorted(targets - EXPECTED_TARGETS)}"
        )
    version = versions.pop()
    document: dict[str, object] = {
        "schema_version": "1.0",
        "product": "High2Min Video Compressor",
        "version": version,
        "distribution": "unsigned, GitHub-native builds with provenance attestations",
        "quality_profile": {
            "encoder": "libx264",
            "crf": 35,
            "preset": "medium",
            "minimum_ssim": 0.95,
            "maximum_bytes": 5242880,
            "audio_removed": True,
        },
        "artifacts": sorted(artifacts, key=lambda item: str(item["target"])),
    }
    index = directory / f"release-index-v{version}.json"
    sums_path = directory / "SHA256SUMS.txt"
    index.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sums_path.write_text("\n".join(sorted(sums)) + "\n", encoding="ascii")
    return index, sums_path, document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default="dist")
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    arguments = parser.parse_args()
    try:
        index, sums, document = generate(Path(arguments.directory).resolve())
    except (KeyError, OSError, ReleaseValidationError) as exc:
        print(f"release set verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"Verified High2Min {document['version']} release set: {index}")
    if arguments.github_output:
        with Path(arguments.github_output).open("a", encoding="utf-8", newline="\n") as output:
            output.write(f"index={index.resolve()}\n")
            output.write(f"sums={sums.resolve()}\n")
            output.write(f"version={document['version']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
