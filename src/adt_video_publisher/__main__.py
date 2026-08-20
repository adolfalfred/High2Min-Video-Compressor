"""Module entry point for ``python -m adt_video_publisher``."""

from __future__ import annotations

from .cli import run


if __name__ == "__main__":
    raise SystemExit(run())

