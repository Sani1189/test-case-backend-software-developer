"""Data shapes that cross a boundary in the pipeline.

Two families, deliberately kept apart:

* **Internal shapes** (:class:`FieldDetection`, :class:`DetectionOutcome`) may hold
  non-JSON types such as a shapely polygon. They never leave the process.
* **Wire shapes** (:class:`ProgressReport`, :class:`JobEvent`, :class:`RunResult`) are
  JSON-serialisable by construction. Part 4 requires that what is sent to the platform
  is built from validated models rather than a dict assembled on the spot, so these are
  the only things :mod:`trackbox_pitch.reporting` may send.

Everything here is frozen and forbids unknown keys, matching the configuration models.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from shapely.geometry.base import BaseGeometry

# Shared by every model here, mirroring the configuration models' strictness.
_STRICT = ConfigDict(extra="forbid", frozen=True)

# Internal shapes additionally permit non-JSON field types.
_INTERNAL = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)


class RejectionReason(StrEnum):
    """Why a frame produced no usable boundary.

    Rejections are ordinary outcomes, not errors, and every one is counted. The
    prototype dropped invalid polygons silently and uncounted, which made "there was
    no pitch in this frame" indistinguishable from "the detector is broken".
    """

    EMPTY_MASK = "empty_mask"
    """The mask was empty -- a blank frame or a camera cut."""

    NO_CONTOUR = "no_contour"
    """The mask was non-empty but contained no external contour."""

    BELOW_MIN_AREA = "below_min_area"
    """The largest contour was smaller than the configured minimum."""

    TOO_FEW_POINTS = "too_few_points"
    """Fewer than three points, so no polygon could exist."""

    DEGENERATE_POLYGON = "degenerate_polygon"
    """Shapely rejected the polygon as invalid or empty."""

    LOW_CONFIDENCE = "low_confidence"
    """A detection was produced but fell below the aggregation confidence floor."""

    DETECTOR_ERROR = "detector_error"
    """The detector raised on this frame. Counted, surfaced, and not fatal."""


class RunStatus(StrEnum):
    """How a run finished."""

    SUCCEEDED = "succeeded"
    NO_VALID_DETECTIONS = "no_valid_detections"
    """The run completed but found nothing usable. Not a crash -- a real outcome."""

    FAILED = "failed"


class DeliveryState(StrEnum):
    """Outcome of one attempt to report to the platform."""

    DELIVERED = "delivered"
    DROPPED = "dropped"
    """Not delivered, and not retried further. The run carries on regardless."""

    DISABLED = "disabled"
    """Reporting switched off by configuration."""


class FieldDetection(BaseModel):
    """One detected playing-field boundary.

    Internal: holds a shapely polygon, so it is not JSON-serialisable and must never
    be handed to the reporter directly.
    """

    model_config = _INTERNAL

    detector_id: str = Field(description="Which detector produced this.")
    frame_index: int = Field(ge=0)
    polygon: BaseGeometry
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _polygon_must_be_usable(self) -> FieldDetection:
        if self.polygon.is_empty:
            raise ValueError("polygon is empty")
        if not self.polygon.is_valid:
            raise ValueError("polygon is not valid")
        return self

    @property
    def area(self) -> float:
        return float(self.polygon.area)


class DetectionOutcome(BaseModel):
    """What one detector call produced: either a detection or a reason it did not.

    Modelled as one type rather than ``FieldDetection | None`` so the *reason* for a
    miss survives. That reason is the difference between a quiet frame and a broken
    detector, and it is what the rejection histogram reports.
    """

    model_config = _INTERNAL

    detection: FieldDetection | None = None
    rejection: RejectionReason | None = None

    @model_validator(mode="after")
    def _exactly_one_of(self) -> DetectionOutcome:
        if (self.detection is None) == (self.rejection is None):
            raise ValueError("exactly one of detection or rejection must be set")
        return self

    @classmethod
    def found(cls, detection: FieldDetection) -> DetectionOutcome:
        return cls(detection=detection)

    @classmethod
    def rejected(cls, reason: RejectionReason) -> DetectionOutcome:
        return cls(rejection=reason)


# --------------------------------------------------------------------------------------
# Wire shapes. JSON-serialisable by construction.
# --------------------------------------------------------------------------------------


class ProgressReport(BaseModel):
    """Periodic progress, sent while a run is in flight."""

    model_config = _STRICT

    run_id: str
    status: RunStatus
    frames_read: int = Field(ge=0)
    frames_analyzed: int = Field(ge=0)
    frames_skipped: int = Field(ge=0)
    valid_detections: int = Field(ge=0)
    rejected_detections: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)
    processed_fraction: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Frames read as a fraction of the stream, when the stream reports a length.",
    )


class JobEvent(BaseModel):
    """A discrete thing that happened during a run."""

    model_config = _STRICT

    run_id: str
    event: str = Field(description="Dotted event name, e.g. 'run.started'.")
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]
    message: str
    timestamp: datetime
    detail: dict[str, Any] | None = None


class BoundarySummary(BaseModel):
    """A boundary in JSON-safe form, for the wire and for the result file."""

    model_config = _STRICT

    coordinates: list[tuple[float, float]]
    area: float = Field(ge=0)
    frame_index: int = Field(ge=0)

    @classmethod
    def from_detection(cls, detection: FieldDetection) -> BoundarySummary:
        return cls(
            # Polygon.exterior is a closed ring: first point repeats last. Drop it.
            coordinates=[(float(x), float(y)) for x, y, *_ in detection.polygon.exterior.coords][
                :-1
            ],
            area=detection.area,
            frame_index=detection.frame_index,
        )


class MetricStats(BaseModel):
    """Summary of the per-frame spatial metric across valid frames.

    Median rather than mean, with the interquartile range alongside it. The feed
    contains deliberately injected false positives, and a mean lets those drag the
    headline number in a way a median does not.
    """

    model_config = _STRICT

    count: int = Field(ge=0)
    minimum: float
    maximum: float
    mean: float
    stdev: float = Field(ge=0)
    median: float
    p25: float
    p75: float
    iqr: float = Field(ge=0)


class AggregationSummary(BaseModel):
    """Everything the aggregator knows at the end of a run. Internal."""

    model_config = _INTERNAL

    analyzed: int = Field(ge=0)
    valid: int = Field(ge=0)
    rejected: int = Field(ge=0)
    rejections_by_reason: dict[str, int]
    metric: MetricStats | None = None
    consensus: FieldDetection | None = None

    @property
    def coverage(self) -> float | None:
        """Fraction of analysed frames that yielded a usable boundary."""
        if self.analyzed == 0:
            return None
        return self.valid / self.analyzed


class RunResult(BaseModel):
    """The outcome of one run. Wire-safe: this is what the platform receives.

    Carries ``reporting_degraded`` so that "the pipeline succeeded but the platform
    never heard about it" is a visible, queryable fact rather than something an
    operator has to infer from silence.
    """

    model_config = _STRICT

    run_id: str
    status: RunStatus
    video_path: str

    frames_read: int = Field(ge=0)
    frames_analyzed: int = Field(ge=0)
    frames_skipped: int = Field(ge=0)

    valid_detections: int = Field(ge=0)
    rejected_detections: int = Field(ge=0)
    rejections_by_reason: dict[str, int]

    coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    metric: MetricStats | None = None
    consensus_boundary: BoundarySummary | None = None

    elapsed_seconds: float = Field(ge=0)
    reporting_degraded: bool = False
    failure: str | None = Field(
        default=None, description="Set when status is FAILED. Never swallowed into a success."
    )
