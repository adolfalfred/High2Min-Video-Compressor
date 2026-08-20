"""Locate and verify the one release archive produced by a native CI job."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from release.support import (  # noqa: E402
    ReleaseValidationError,
    sha256_file,
    verify_release_archive,
)


def _archives(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and (path.suffix.lower() == ".zip" or path.name.endswith(".tar.gz"))
    )


def locate(directory: Path, target: str) -> tuple[Path, Path, dict[str, object]]:
    matches: list[tuple[Path, dict[str, object]]] = []
    for archive in _archives(directory):
        document = verify_release_archive(archive)
        if document.get("target") == target:
            matches.append((archive, document))
    if len(matches) != 1:
        raise ReleaseValidationError(
            f"Expected one verified {target} archive in {directory}, found {len(matches)}."
        )
    archive, document = matches[0]
    if document.get("product") != "High2Min Video Compressor":
        raise ReleaseValidationError("Release manifest contains the wrong product name.")
    checksum = Path(str(archive) + ".sha256")
    if not checksum.is_file():
        raise ReleaseValidationError(f"Release checksum is missing: {checksum}")
    values = checksum.read_text(encoding="ascii").split()
    if len(values) < 2 or values[0] != sha256_file(archive) or values[1] != archive.name:
        raise ReleaseValidationError(f"Release checksum is invalid: {checksum}")
    return archive.resolve(), checksum.resolve(), document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default="releases")
    parser.add_argument("--target", required=True)
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    arguments = parser.parse_args()
    try:
        archive, checksum, document = locate(Path(arguments.directory).resolve(), arguments.target)
    except (OSError, ReleaseValidationError) as exc:
        print(f"release verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"Verified {archive.name} ({document['product_version']}, {document['target']})")
    if arguments.github_output:
        with Path(arguments.github_output).open("a", encoding="utf-8", newline="\n") as output:
            output.write(f"archive={archive}\n")
            output.write(f"checksum={checksum}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
