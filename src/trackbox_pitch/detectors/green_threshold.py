"""Colour-threshold field detector.

A faithful port of the prototype's mask-and-contour approach, with the parts that were
wrong for a long-running process fixed and the parts that were merely naive kept
visible rather than quietly corrected.

Ported as-is from ``legacy/synthetic_field_prototype.py``:

* the HSV band defaults to the same green range,
* the largest external contour is still the boundary,
* the minimum-area gate is still applied.

Fixed:

* the HSV bound arrays, previously rebuilt on every frame, are built once in
  ``__init__``,
* contour areas are computed once rather than twice per contour,
* the minimum-area check happens *before* a polygon is constructed, so the injected
  noise frames cost almost nothing,
* ``except Exception: pass`` is gone. A detector that silently absorbs a real error is
  the defect, so failures are counted and reported as ``DETECTOR_ERROR`` -- and only a
  narrow set of exceptions is treated that way.

Known limitation, deliberately not papered over: because the synthetic background is
solid green and the pitch is drawn as white lines *on top* of it, the green mask covers
the whole frame, so with ``RETR_EXTERNAL`` the "boundary" is the frame edge rather than
the pitch. See ``DECISIONS.md``. Fixing that is a modelling change, not a plumbing one,
and doing it silently here would hide the flaw rather than document it.
"""

from __future__ import annotations

import cv2
import numpy as np
from shapely.errors import GEOSException, ShapelyError
from shapely.geometry import Polygon

from ..config import GreenThresholdConfig
from ..models import DetectionOutcome, FieldDetection, RejectionReason
from .base import register

# Exceptions that mean "this frame could not be turned into geometry", as opposed to a
# bug. Anything outside this set propagates: a TypeError is not a data gap.
_GEOMETRY_ERRORS = (GEOSException, ShapelyError, ValueError, cv2.error)


class GreenThresholdDetector:
    """Derives a boundary polygon from a green colour mask."""

    def __init__(self, config: GreenThresholdConfig) -> None:
        self.detector_id = config.type
        self._lower = np.array(config.hsv_lower, dtype=np.uint8)
        self._upper = np.array(config.hsv_upper, dtype=np.uint8)
        self._min_area = config.min_area

    def detect(self, frame: np.ndarray) -> DetectionOutcome:
        if frame is None or frame.size == 0:
            return DetectionOutcome.rejected(RejectionReason.EMPTY_MASK)

        try:
            mask = self._mask(frame)
        except cv2.error:
            # Colour conversion failed on a frame we cannot interpret at all.
            return DetectionOutcome.rejected(RejectionReason.DETECTOR_ERROR)

        frame_area = float(frame.shape[0] * frame.shape[1])
        if frame_area == 0:
            return DetectionOutcome.rejected(RejectionReason.EMPTY_MASK)

        # One pass instead of countNonZero plus findContours both reading the mask.
        mask_pixels = int(cv2.countNonZero(mask))
        if mask_pixels == 0:
            return DetectionOutcome.rejected(RejectionReason.EMPTY_MASK)

        try:
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        except cv2.error:
            return DetectionOutcome.rejected(RejectionReason.DETECTOR_ERROR)

        if not contours:
            return DetectionOutcome.rejected(RejectionReason.NO_CONTOUR)

        # Areas computed once. The prototype's `max(contours, key=cv2.contourArea)`
        # evaluated every contour's area and then evaluated the winner's again.
        largest_index = -1
        largest_area = 0.0
        for index, contour in enumerate(contours):
            area = cv2.contourArea(contour)
            if area > largest_area:
                largest_area = area
                largest_index = index

        # Cheap reject before any geometry work: the noise frames the generator injects
        # never reach the expensive path.
        if largest_area < self._min_area:
            return DetectionOutcome.rejected(RejectionReason.BELOW_MIN_AREA)

        points = contours[largest_index].reshape(-1, 2)
        if len(points) < 3:
            return DetectionOutcome.rejected(RejectionReason.TOO_FEW_POINTS)

        try:
            polygon = Polygon(points)
            if polygon.is_empty or not polygon.is_valid or polygon.area <= 0:
                return DetectionOutcome.rejected(RejectionReason.DEGENERATE_POLYGON)
        except _GEOMETRY_ERRORS:
            return DetectionOutcome.rejected(RejectionReason.DEGENERATE_POLYGON)

        return DetectionOutcome.found(
            FieldDetection(
                detector_id=self.detector_id,
                frame_index=0,  # the pipeline stamps the real index
                polygon=polygon,
                # A threshold detector has no calibrated score. Coverage of the mask is
                # an honest proxy: how much of the frame matched the target colour.
                # Documented as a proxy rather than presented as a model confidence.
                confidence=min(1.0, mask_pixels / frame_area),
            )
        )

    def _mask(self, frame: np.ndarray) -> np.ndarray:
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        return cv2.inRange(hsv, self._lower, self._upper)


@register("green_threshold")
def _build(config: GreenThresholdConfig) -> GreenThresholdDetector:
    return GreenThresholdDetector(config)
