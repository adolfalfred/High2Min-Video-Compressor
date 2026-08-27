"""Stable automation contracts shared by the CLI, UI, and future core engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import IntEnum
from importlib.resources import files
from typing import Final

from . import __version__

TOOL_NAME: Final = "high2min"
CONTRACT_SCHEMA_VERSION: Final = "1.0"


class ExitCode(IntEnum):
    """Stable process exit codes for agents, scripts, and CI systems."""

    SUCCESS = 0
    USAGE_ERROR = 2
    INVALID_INPUT = 3
    UNSAFE_PATH = 4
    PROBE_FAILED = 5
    RESOURCE_LIMIT = 6
    ENCODING_FAILED = 7
    VALIDATION_FAILED = 8
    PARTIAL_SUCCESS = 9
    PUBLISH_FAILED = 10
    INTERRUPTED = 11
    UNSUPPORTED = 69
    INTERNAL_ERROR = 70


@dataclass(frozen=True, slots=True)
class ExitCodeDescription:
    code: int
    name: str
    meaning: str
    retryable: bool


EXIT_CODE_DESCRIPTIONS: Final[tuple[ExitCodeDescription, ...]] = (
    ExitCodeDescription(0, "SUCCESS", "The requested operation completed successfully.", False),
    ExitCodeDescription(2, "USAGE_ERROR", "The command or its arguments are invalid.", False),
    ExitCodeDescription(3, "INVALID_INPUT", "An input path or media item is invalid.", False),
    ExitCodeDescription(4, "UNSAFE_PATH", "The requested paths could overwrite or endanger source data.", False),
    ExitCodeDescription(5, "PROBE_FAILED", "Media metadata could not be read.", True),
    ExitCodeDescription(6, "RESOURCE_LIMIT", "CPU, memory, disk, or encoder capacity is insufficient.", True),
    ExitCodeDescription(7, "ENCODING_FAILED", "One or more video encodes failed.", True),
    ExitCodeDescription(8, "VALIDATION_FAILED", "Generated output did not meet the release requirements.", True),
    ExitCodeDescription(9, "PARTIAL_SUCCESS", "Some items succeeded and some failed.", True),
    ExitCodeDescription(10, "PUBLISH_FAILED", "ADT publishing or packaging failed.", True),
    ExitCodeDescription(11, "INTERRUPTED", "The operation was cancelled or interrupted safely.", True),
    ExitCodeDescription(69, "UNSUPPORTED", "The requested operation or environment is unsupported.", False),
    ExitCodeDescription(70, "INTERNAL_ERROR", "The tool encountered an unexpected internal error.", True),
)

SCHEMA_FILES: Final[dict[str, str]] = {
    "job-plan": "job-plan-v1.schema.json",
    "progress-event": "progress-event-v1.schema.json",
    "publish-result": "publish-result-v1.schema.json",
    "publish-plan": "publish-plan-v1.schema.json",
    "result": "result-v1.schema.json",
}

COMMANDS: Final[tuple[dict[str, str], ...]] = (
    {"name": "contract", "status": "available", "purpose": "Print the stable CLI automation contract."},
    {"name": "schema", "status": "available", "purpose": "Print a bundled JSON schema."},
    {"name": "inspect", "status": "available", "purpose": "Inspect source media without changing files."},
    {"name": "plan", "status": "available", "purpose": "Calculate safe paths, settings, and concurrency."},
    {"name": "compress", "status": "available", "purpose": "Create validated compressed copies."},
    {"name": "verify", "status": "available", "purpose": "Validate compressed video outputs."},
    {"name": "publish", "status": "available", "purpose": "Publish videos into an ADT website in place or as a copy."},
    {"name": "publish-plan", "status": "available", "purpose": "Preview ADT compatibility, mappings, and file changes without writing."},
    {"name": "browser-test", "status": "available", "purpose": "Validate responsive sign-video and narration behavior in Chromium."},
    {"name": "resume", "status": "available", "purpose": "Resume an interrupted job."},
    {"name": "ui", "status": "available", "purpose": "Open the optional desktop interface."},
)


def schema_resource(name: str):
    """Return the traversable resource for a public schema name."""

    try:
        filename = SCHEMA_FILES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown schema: {name}") from exc
    return files("adt_video_publisher").joinpath("schemas", filename)


def contract_document() -> dict[str, object]:
    """Return the complete machine-readable CLI contract."""

    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "tool": {"name": TOOL_NAME, "version": __version__},
        "commands": list(COMMANDS),
        "exit_codes": [asdict(item) for item in EXIT_CODE_DESCRIPTIONS],
        "schemas": dict(SCHEMA_FILES),
        "guarantees": {
            "non_interactive_cli": True,
            "ui_optional": True,
            "originals_immutable": True,
            "separate_compression_output_required": True,
            "publish_preview_read_only": True,
            "in_place_publish_transactional": True,
            "adt_zip_files_immutable": True,
            "structured_output": True,
            "quality_first_encoding": True,
            "objective_quality_validation": "ssim",
        },
    }
