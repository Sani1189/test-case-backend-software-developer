"""Turning per-frame detections into one final, robust summary.

Two separate jobs, both of which the prototype got wrong by accumulating everything:

**Aggregate without keeping everything.** The prototype appended ``(frame, polygon,
area)`` to a list for the whole run, so memory grew with feed length and a long run
would eventually die. Here, per-frame metric values (plain floats) are retained so the
median is exact, and the *polygons* -- the actually heavy objects -- are bounded by a
ring buffer. For a ten-hour feed at 5fps the metric buffer is about 1.4 MB, which is
not worth approximating away.

**Aggregate robustly.** The feed contains deliberately injected false positives. A mean
would let those drag the headline number; a median and interquartile range do not.

The spatial metric is the area of the detected boundary intersected with the frame
rectangle -- the same metric the prototype computed, except the frame rectangle is
built once here from the real frame dimensions instead of being rebuilt every frame
from a hardcoded 1280x720.
"""

from __future__ import annotations

from collections import Counter, deque

import numpy as np
from shapely.geometry import box

from .config import AggregationConfig
from .models import (
    AggregationSummary,
    FieldDetection,
    MetricStats,
    RejectionReason,
)


class BoundaryAggregator:
    """Accumulates detections and rejections and summarises them at the end."""

    def __init__(self, config: AggregationConfig, frame_width: int, frame_height: int) -> None:
        self._config = config
        self._min_confidence = config.min_confidence

        # Built once, not per frame, and from the stream's real dimensions rather than
        # the prototype's hardcoded 1280x720.
        self._frame_bounds = box(0.0, 0.0, float(frame_width), float(frame_height))

        self._metrics: list[float] = []
        self._recent: deque[FieldDetection] = deque(maxlen=config.consensus_polygon_window)
        self._rejections: Counter[str] = Counter()

        self._analyzed = 0
        self._valid = 0
        self._rejected = 0

    # -- recording ---------------------------------------------------------------------

    def observe(self, detection: FieldDetection) -> bool:
        """Record a detection. Returns whether it counted as valid.

        A detection below the confidence floor is recorded as a rejection rather than
        dropped, so it shows up in the histogram instead of vanishing.
        """
        self._analyzed += 1

        if detection.confidence < self._min_confidence:
            self._rejected += 1
            self._rejections[RejectionReason.LOW_CONFIDENCE.value] += 1
            return False

        self._metrics.append(float(detection.polygon.intersection(self._frame_bounds).area))
        self._recent.append(detection)
        self._valid += 1
        return True

    def reject(self, reason: RejectionReason) -> None:
        """Record that a frame produced no usable boundary, and why."""
        self._analyzed += 1
        self._rejected += 1
        self._rejections[reason.value] += 1

    # -- reading -----------------------------------------------------------------------

    @property
    def analyzed(self) -> int:
        return self._analyzed

    @property
    def valid(self) -> int:
        return self._valid

    @property
    def rejected(self) -> int:
        return self._rejected

    def summary(self) -> AggregationSummary:
        """The final result. Safe to call at any point, including mid-run."""
        return AggregationSummary(
            analyzed=self._analyzed,
            valid=self._valid,
            rejected=self._rejected,
            rejections_by_reason=dict(sorted(self._rejections.items())),
            metric=self._metric_stats(),
            consensus=self._consensus(),
        )

    def converged(self) -> bool:
        """Whether the running metric is stable enough to stop early.

        Only consulted when ``aggregation.early_exit`` is enabled. Returns False while
        there are too few samples, and uses the relative standard error rather than the
        standard deviation so that the test does not depend on the metric's scale.
        """
        if not self._config.early_exit:
            return False
        if len(self._metrics) < self._config.min_valid_samples:
            return False

        values = np.asarray(self._metrics, dtype=float)
        mean = float(values.mean())
        if mean == 0.0:
            # A zero metric carries no scale to be relative to; convergence is
            # undefined rather than trivially true.
            return False

        relative_stderr = float(values.std(ddof=1) / np.sqrt(values.size)) / abs(mean)
        return relative_stderr <= self._config.relative_stderr_threshold

    # -- internals ---------------------------------------------------------------------

    def _metric_stats(self) -> MetricStats | None:
        if not self._metrics:
            return None

        values = np.asarray(self._metrics, dtype=float)
        # ddof=1 is the sample standard deviation, but it is undefined for a single
        # observation, so a lone sample reports zero spread rather than NaN.
        stdev = float(values.std(ddof=1)) if values.size > 1 else 0.0
        p25, median, p75 = (float(v) for v in np.percentile(values, [25, 50, 75]))

        return MetricStats(
            count=int(values.size),
            minimum=float(values.min()),
            maximum=float(values.max()),
            mean=float(values.mean()),
            stdev=stdev,
            median=median,
            p25=p25,
            p75=p75,
            iqr=p75 - p25,
        )

    def _consensus(self) -> FieldDetection | None:
        """The retained detection whose metric sits closest to the median.

        A representative boundary drawn from real observations, rather than a computed
        average polygon -- averaging arbitrary polygon vertices does not produce a
        meaningful shape.
        """
        if not self._recent or not self._metrics:
            return None

        median = float(np.median(self._metrics))
        return min(
            self._recent,
            key=lambda detection: abs(
                float(detection.polygon.intersection(self._frame_bounds).area) - median
            ),
        )
