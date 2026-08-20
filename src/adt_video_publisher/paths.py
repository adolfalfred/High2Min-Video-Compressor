"""Safe, deterministic source discovery and output-path planning."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import InvalidInputError, UnsafePathError

SUPPORTED_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"})


@dataclass(frozen=True, slots=True)
class FilePathPlan:
    source: Path
    output: Path
    relative_source: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "source": str(self.source),
            "output": str(self.output),
            "relative_source": self.relative_source.as_posix(),
        }


@dataclass(frozen=True, slots=True)
class BatchPathPlan:
    source: Path
    output_root: Path
    items: tuple[FilePathPlan, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "source": str(self.source),
            "output_root": str(self.output_root),
            "items": [item.to_dict() for item in self.items],
        }


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True


def resolve_source(source: str | os.PathLike[str]) -> Path:
    path = Path(source).expanduser().resolve()
    if not path.exists():
        raise InvalidInputError(f"Input path does not exist: '{path}'.")
    if not path.is_file() and not path.is_dir():
        raise InvalidInputError(f"Input path is not a regular file or directory: '{path}'.")
    if path.is_file() and path.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
        raise InvalidInputError(f"Unsupported video extension: '{path.suffix}'.")
    return path


def default_output_root(source: Path) -> Path:
    if source.is_dir():
        return source.parent / f"{source.name} - Compressed"
    return source.parent / "Compressed"


def resolve_output_root(source: Path, output: str | os.PathLike[str] | None = None) -> Path:
    candidate = Path(output).expanduser() if output is not None else default_output_root(source)
    candidate = candidate.resolve(strict=False)
    if candidate.exists() and not candidate.is_dir():
        raise UnsafePathError(f"Output path is not a directory: '{candidate}'.")

    if source.is_dir():
        if _path_key(candidate) == _path_key(source) or _is_within(candidate, source):
            raise UnsafePathError("The output directory must be outside the source directory.")
    else:
        if _path_key(candidate) == _path_key(source) or _path_key(candidate) == _path_key(source.parent):
            raise UnsafePathError("The output directory must be separate from the source file location.")
    return candidate


def discover_videos(source: Path, *, recursive: bool = False) -> tuple[Path, ...]:
    if source.is_file():
        return (source,)

    iterator = source.rglob("*") if recursive else source.glob("*")
    videos = tuple(
        sorted(
            (item.resolve() for item in iterator if item.is_file() and item.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS),
            key=lambda item: item.as_posix().casefold(),
        )
    )
    if not videos:
        scope = "recursively" if recursive else "in the selected directory"
        raise InvalidInputError(f"No supported videos were found {scope}: '{source}'.")
    return videos


def build_path_plan(
    source: str | os.PathLike[str],
    *,
    output: str | os.PathLike[str] | None = None,
    recursive: bool = False,
) -> BatchPathPlan:
    """Create a read-only plan that guarantees outputs cannot replace sources."""

    source_path = resolve_source(source)
    output_root = resolve_output_root(source_path, output)
    videos = discover_videos(source_path, recursive=recursive)
    plans: list[FilePathPlan] = []
    output_keys: set[str] = set()

    for video in videos:
        relative = video.name if source_path.is_file() else video.relative_to(source_path)
        relative_path = Path(relative)
        output_path = (output_root / relative_path).with_suffix(".mp4").resolve(strict=False)
        key = _path_key(output_path)
        if key in output_keys:
            raise UnsafePathError(
                f"Multiple source videos would produce the same output path: '{output_path}'."
            )
        if key == _path_key(video):
            raise UnsafePathError(f"An output would replace its source: '{video}'.")
        output_keys.add(key)
        plans.append(FilePathPlan(source=video, output=output_path, relative_source=relative_path))

    return BatchPathPlan(source=source_path, output_root=output_root, items=tuple(plans))

