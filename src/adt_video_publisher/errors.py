"""Domain errors used by the core engine and mapped to stable CLI exit codes."""

from __future__ import annotations


class AdtVideoError(Exception):
    """Base class for expected application errors."""


class InvalidInputError(AdtVideoError):
    """Raised when an input path or media item is invalid."""


class UnsafePathError(AdtVideoError):
    """Raised when output planning could modify or overlap source data."""


class ProbeUnavailableError(AdtVideoError):
    """Raised when neither FFprobe nor FFmpeg can be located."""


class ProbeFailedError(AdtVideoError):
    """Raised when media metadata cannot be read or interpreted."""


class EncodingFailedError(AdtVideoError):
    """Raised when FFmpeg cannot create a candidate output."""


class ValidationFailedError(AdtVideoError):
    """Raised when a candidate output does not meet release requirements."""


class ResourceLimitError(AdtVideoError):
    """Raised when safe processing exceeds detected CPU, memory, or disk capacity."""


class PublishFailedError(AdtVideoError):
    """Raised when an ADT website or deployment package cannot be published safely."""


class PublishingInterruptedError(AdtVideoError):
    """Raised when ADT publishing is cancelled before the commit phase."""
