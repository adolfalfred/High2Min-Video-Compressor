"""Verify a portable High2Min Video Compressor release archive and checksum."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from release.support import ReleaseValidationError, sha256_file, verify_release_archive  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    arguments = parser.parse_args()
    archive = Path(arguments.archive).expanduser().resolve()
    checksum = Path(str(archive) + ".sha256")
    try:
        document = verify_release_archive(archive)
        digest = sha256_file(archive)
        expected = checksum.read_text(encoding="ascii").split()[0]
        if digest != expected:
            raise ReleaseValidationError("Archive SHA-256 does not match its sidecar.")
    except (OSError, IndexError, ReleaseValidationError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "archive": str(archive),
                "sha256": digest,
                "product_version": document["product_version"],
                "target": document["target"],
                "file_count": len(document["files"]),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
