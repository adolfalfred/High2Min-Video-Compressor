"""Duration-aware compression planning without writing output files."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Literal

from .errors import InvalidInputError
from .media import MediaInfo

PlanAction = Literal["remux", "encode"]

DEFAULT_MAXIMUM_BYTES = 5 * 1024 * 1024
DEFAULT_SAFETY_RATIO = 0.94
MINIMUM_CONTAINER_OVERHEAD = 64 * 1024
OVERHEAD_BYTES_PER_SECOND = 2048
LOW_BITRATE_WARNING_THRESHOLD = 250_000


@dataclass(frozen=True, slots=True)
class EncodingPlan:
    action: PlanAction
    reason: str
    maximum_bytes: int
    target_bytes: int
    reserved_overhead_bytes: int
    target_video_bitrate: int | None
    requires_quality_review: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def calculate_encoding_plan(
    media: MediaInfo,
    *,
    maximum_bytes: int = DEFAULT_MAXIMUM_BYTES,
    safety_ratio: float = DEFAULT_SAFETY_RATIO,
) -> EncodingPlan:
    """Choose remuxing or encoding and calculate a size-aware video bitrate."""

    if maximum_bytes < 1024 * 1024:
        raise InvalidInputError("The maximum output size must be at least 1 MiB.")
    if not 0.5 <= safety_ratio < 1.0:
        raise InvalidInputError("The safety ratio must be at least 0.5 and below 1.0.")
    if media.duration_seconds <= 0:
        raise InvalidInputError("A positive media duration is required for bitrate planning.")

    video = media.primary_video
    stream_copy_compatible = video.codec == "h264" and video.pixel_format == "yuv420p"
    if media.size_bytes <= maximum_bytes and stream_copy_compatible:
        reason = "Remove non-video streams and enable fast-start without re-encoding."
        if not media.has_audio:
            reason = "Create a separate fast-start copy without re-encoding the video stream."
        return EncodingPlan(
            action="remux",
            reason=reason,
            maximum_bytes=maximum_bytes,
            target_bytes=media.size_bytes,
            reserved_overhead_bytes=0,
            target_video_bitrate=None,
            requires_quality_review=False,
        )

    target_bytes = math.floor(maximum_bytes * safety_ratio)
    reserved_overhead = max(
        MINIMUM_CONTAINER_OVERHEAD,
        math.ceil(media.duration_seconds * OVERHEAD_BYTES_PER_SECOND),
    )
    reserved_overhead = min(reserved_overhead, target_bytes // 4)
    video_budget_bytes = max(1, target_bytes - reserved_overhead)
    target_video_bitrate = max(1, math.floor(video_budget_bytes * 8 / media.duration_seconds))
    return EncodingPlan(
        action="encode",
        reason="The source requires transcoding to meet format or size requirements.",
        maximum_bytes=maximum_bytes,
        target_bytes=target_bytes,
        reserved_overhead_bytes=reserved_overhead,
        target_video_bitrate=target_video_bitrate,
        requires_quality_review=target_video_bitrate < LOW_BITRATE_WARNING_THRESHOLD,
    )

