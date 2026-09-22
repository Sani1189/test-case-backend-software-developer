"""Aggregation: robust statistics, honest counting, bounded memory."""

from __future__ import annotations

import pytest
from shapely.geometry import Polygon

from trackbox_pitch.aggregation import BoundaryAggregator
from trackbox_pitch.config import load_settings
from trackbox_pitch.models import FieldDetection, RejectionReason

FRAME = (100, 100)  # width, height


def agg_config(**overrides):
    base = load_settings("config/default.yaml", environ={}).aggregation
    return base.model_copy(update=overrides) if overrides else base


def detection(area_square: float, *, confidence: float = 0.9, frame_index: int = 0):
    side = area_square**0.5
    return FieldDetection(
        detector_id="test",
        frame_index=frame_index,
        polygon=Polygon([(0, 0), (side, 0), (side, side), (0, side)]),
        confidence=confidence,
    )


class TestCounting:
    def test_a_valid_detection_is_counted(self):
        aggregator = BoundaryAggregator(agg_config(), *FRAME)
        assert aggregator.observe(detection(100)) is True
        assert (aggregator.analyzed, aggregator.valid, aggregator.rejected) == (1, 1, 0)

    def test_a_low_confidence_detection_is_counted_as_a_rejection(self):
        """It is recorded with a reason, not silently discarded."""
        aggregator = BoundaryAggregator(agg_config(min_confidence=0.95), *FRAME)
        assert aggregator.observe(detection(100, confidence=0.1)) is False

        summary = aggregator.summary()
        assert (summary.valid, summary.rejected) == (0, 1)
        assert summary.rejections_by_reason == {"low_confidence": 1}

    def test_rejections_are_counted_by_reason(self):
        aggregator = BoundaryAggregator(agg_config(), *FRAME)
        aggregator.reject(RejectionReason.EMPTY_MASK)
        aggregator.reject(RejectionReason.EMPTY_MASK)
        aggregator.reject(RejectionReason.BELOW_MIN_AREA)

        summary = aggregator.summary()
        # Sorted and deterministic, so two runs of the same feed produce identical output.
        assert summary.rejections_by_reason == {"below_min_area": 1, "empty_mask": 2}
        assert summary.analyzed == 3
        assert summary.valid == 0

    def test_analyzed_always_equals_valid_plus_rejected(self):
        aggregator = BoundaryAggregator(agg_config(), *FRAME)
        aggregator.observe(detection(100))
        aggregator.reject(RejectionReason.NO_CONTOUR)
        summary = aggregator.summary()
        assert summary.analyzed == summary.valid + summary.rejected == 2


class TestMetric:
    def test_the_metric_is_the_area_intersected_with_the_frame(self):
        aggregator = BoundaryAggregator(agg_config(), *FRAME)
        aggregator.observe(detection(2_500))  # 50x50, wholly inside a 100x100 frame
        assert aggregator.summary().metric.median == pytest.approx(2_500)

    def test_geometry_outside_the_frame_is_clipped(self):
        aggregator = BoundaryAggregator(agg_config(), *FRAME)
        huge = FieldDetection(
            detector_id="test",
            frame_index=0,
            polygon=Polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)]),
            confidence=0.9,
        )
        aggregator.observe(huge)
        assert aggregator.summary().metric.median == pytest.approx(10_000)

    def test_median_resists_an_injected_outlier(self):
        """The feed contains deliberate false positives; a mean would be dragged."""
        aggregator = BoundaryAggregator(agg_config(), *FRAME)
        for _ in range(9):
            aggregator.observe(detection(2_500))
        aggregator.observe(detection(90_000))  # one absurd value

        metric = aggregator.summary().metric
        assert metric.median == pytest.approx(2_500)
        assert metric.mean > metric.median, "the mean is the statistic being guarded against"

    def test_a_single_sample_reports_zero_spread_rather_than_nan(self):
        aggregator = BoundaryAggregator(agg_config(), *FRAME)
        aggregator.observe(detection(2_500))
        metric = aggregator.summary().metric
        assert metric.count == 1
        assert metric.stdev == 0.0
        assert metric.iqr == 0.0

    def test_percentiles_are_ordered(self):
        aggregator = BoundaryAggregator(agg_config(), *FRAME)
        for side in (10, 20, 30, 40, 50):
            aggregator.observe(detection(side * side))

        metric = aggregator.summary().metric
        assert metric.minimum <= metric.p25 <= metric.median <= metric.p75 <= metric.maximum

    def test_no_detections_means_no_metric(self):
        aggregator = BoundaryAggregator(agg_config(), *FRAME)
        aggregator.reject(RejectionReason.EMPTY_MASK)
        summary = aggregator.summary()
        assert summary.metric is None
        assert summary.consensus is None
        assert summary.coverage == 0.0


class TestConsensus:
    def test_consensus_is_an_observed_boundary_not_a_computed_average(self):
        aggregator = BoundaryAggregator(agg_config(), *FRAME)
        observed = [detection(2_500, frame_index=i) for i in range(5)]
        for item in observed:
            aggregator.observe(item)

        consensus = aggregator.summary().consensus
        assert consensus is not None
        assert any(consensus is item for item in observed)

    def test_consensus_prefers_the_median_sized_boundary(self):
        aggregator = BoundaryAggregator(agg_config(), *FRAME)
        for side in (10, 30, 50):
            aggregator.observe(detection(side * side))
        # The median of {100, 900, 2500} is 900, so the 30x30 detection should be chosen.
        assert aggregator.summary().consensus.area == pytest.approx(900)


class TestBoundedMemory:
    def test_polygon_retention_is_bounded_by_the_window(self):
        """The prototype appended every polygon for the whole run."""
        aggregator = BoundaryAggregator(agg_config(consensus_polygon_window=5), *FRAME)
        for _ in range(500):
            aggregator.observe(detection(2_500))

        assert aggregator._valid == 500, "every detection is still counted"
        assert len(aggregator._recent) == 5, "but only a window of polygons is retained"


class TestConvergence:
    def test_disabled_by_default(self):
        aggregator = BoundaryAggregator(agg_config(early_exit=False), *FRAME)
        for _ in range(1000):
            aggregator.observe(detection(2_500))
        assert aggregator.converged() is False

    def test_stable_input_converges_when_enabled(self):
        aggregator = BoundaryAggregator(
            agg_config(early_exit=True, min_valid_samples=50, relative_stderr_threshold=0.02),
            *FRAME,
        )
        for _ in range(200):
            aggregator.observe(detection(2_500))
        assert aggregator.converged() is True

    def test_too_few_samples_never_converges(self):
        aggregator = BoundaryAggregator(agg_config(early_exit=True, min_valid_samples=50), *FRAME)
        for _ in range(10):
            aggregator.observe(detection(2_500))
        assert aggregator.converged() is False

    def test_an_unstable_metric_does_not_converge(self):
        aggregator = BoundaryAggregator(
            agg_config(early_exit=True, min_valid_samples=5, relative_stderr_threshold=0.001),
            *FRAME,
        )
        for side in (10, 90, 20, 80, 30, 70, 40, 60, 50, 55):
            aggregator.observe(detection(side * side))
        assert aggregator.converged() is False

    def test_a_zero_metric_has_no_scale_to_be_relative_to(self):
        """Convergence is undefined when the metric is zero, not trivially true.

        A boundary entirely outside the frame intersects it in zero area, so the running
        mean is zero and a relative standard error cannot be formed. Reporting "converged"
        there would stop a run on the strength of a metric that says nothing.
        """
        aggregator = BoundaryAggregator(agg_config(early_exit=True, min_valid_samples=2), *FRAME)
        outside = FieldDetection(
            detector_id="test",
            frame_index=0,
            polygon=Polygon([(500, 500), (600, 500), (600, 600), (500, 600)]),
            confidence=0.9,
        )
        for _ in range(10):
            aggregator.observe(outside)

        assert aggregator.summary().metric.median == 0.0
        assert aggregator.converged() is False
