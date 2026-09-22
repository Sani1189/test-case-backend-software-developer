"""The data shapes that cross boundaries.

The important properties are the invariants: an outcome carries exactly one of a
detection or a reason, a detection cannot hold geometry nobody can use, and everything
destined for the wire is JSON-serialisable. That last one is what makes the Part 4
requirement enforceable rather than aspirational.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from shapely.geometry import Polygon

from trackbox_pitch.models import (
    BoundarySummary,
    DeliveryState,
    DetectionOutcome,
    FieldDetection,
    JobEvent,
    MetricStats,
    ProgressReport,
    RejectionReason,
    RunResult,
    RunStatus,
)

VALID_POLYGON = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])


def a_detection(**overrides) -> FieldDetection:
    payload = {
        "detector_id": "test",
        "frame_index": 0,
        "polygon": VALID_POLYGON,
        "confidence": 0.9,
    }
    payload.update(overrides)
    return FieldDetection(**payload)


class TestDetectionOutcome:
    def test_a_detection_is_a_valid_outcome(self):
        outcome = DetectionOutcome.found(a_detection())
        assert outcome.detection is not None
        assert outcome.rejection is None

    def test_a_reason_without_a_detection_is_a_valid_outcome(self):
        outcome = DetectionOutcome.rejected(RejectionReason.EMPTY_MASK)
        assert outcome.detection is None
        assert outcome.rejection is RejectionReason.EMPTY_MASK

    def test_both_set_is_rejected(self):
        with pytest.raises(ValidationError, match="exactly one"):
            DetectionOutcome(detection=a_detection(), rejection=RejectionReason.EMPTY_MASK)

    def test_neither_set_is_rejected(self):
        with pytest.raises(ValidationError, match="exactly one"):
            DetectionOutcome()


class TestFieldDetection:
    def test_confidence_is_bounded(self):
        with pytest.raises(ValidationError):
            a_detection(confidence=1.5)

    def test_frame_index_cannot_be_negative(self):
        with pytest.raises(ValidationError):
            a_detection(frame_index=-1)

    def test_empty_geometry_is_rejected(self):
        with pytest.raises(ValidationError, match="empty"):
            a_detection(polygon=Polygon())

    def test_invalid_geometry_is_rejected(self):
        # A bowtie self-intersection is not a usable boundary.
        bowtie = Polygon([(0, 0), (10, 10), (10, 0), (0, 10)])
        with pytest.raises(ValidationError, match="not valid"):
            a_detection(polygon=bowtie)

    def test_detections_are_immutable(self):
        with pytest.raises(ValidationError):
            a_detection().confidence = 0.1

    def test_area_is_exposed(self):
        assert a_detection().area == pytest.approx(100.0)


class TestBoundarySummary:
    def test_round_trips_a_polygon_to_json_safe_coordinates(self):
        summary = BoundarySummary.from_detection(a_detection(frame_index=7))

        # The closing point of the ring is dropped: it duplicates the first.
        assert summary.coordinates == [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        assert summary.frame_index == 7
        assert summary.area == pytest.approx(100.0)

    def test_is_json_serialisable(self):
        summary = BoundarySummary.from_detection(a_detection())
        assert json.loads(summary.model_dump_json())["coordinates"][0] == [0.0, 0.0]


class TestWireModelsAreSerialisable:
    """Every shape the reporter may send has to survive a JSON round trip."""

    def test_progress_report(self):
        report = ProgressReport(
            run_id="abc",
            status=RunStatus.RUNNING,
            frames_read=10,
            frames_analyzed=10,
            frames_skipped=0,
            valid_detections=8,
            rejected_detections=2,
            elapsed_seconds=1.5,
            processed_fraction=0.25,
        )
        dumped = report.model_dump(mode="json")
        assert json.loads(json.dumps(dumped))["status"] == "running"

    def test_job_event(self):
        event = JobEvent(
            run_id="abc",
            event="run.started",
            level="INFO",
            message="hello",
            timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
            detail={"k": "v"},
        )
        assert isinstance(json.loads(event.model_dump_json())["timestamp"], str)

    def test_run_result(self):
        result = RunResult(
            run_id="abc",
            status=RunStatus.SUCCEEDED,
            video_path="feed.mp4",
            frames_read=100,
            frames_analyzed=20,
            frames_skipped=80,
            valid_detections=19,
            rejected_detections=1,
            rejections_by_reason={"empty_mask": 1},
            coverage=0.95,
            metric=MetricStats(
                count=19,
                minimum=1.0,
                maximum=2.0,
                mean=1.5,
                stdev=0.1,
                median=1.5,
                p25=1.4,
                p75=1.6,
                iqr=0.2,
            ),
            consensus_boundary=BoundarySummary.from_detection(a_detection()),
            elapsed_seconds=3.0,
            reporting_degraded=True,
        )
        payload = json.loads(result.model_dump_json())
        assert payload["status"] == "succeeded"
        assert payload["reporting_degraded"] is True


class TestStrictness:
    def test_unknown_fields_are_rejected_everywhere(self):
        with pytest.raises(ValidationError):
            ProgressReport(
                run_id="abc",
                status=RunStatus.RUNNING,
                frames_read=1,
                frames_analyzed=1,
                frames_skipped=0,
                valid_detections=1,
                rejected_detections=0,
                elapsed_seconds=1.0,
                unexpected="nope",
            )

    def test_coverage_is_bounded(self):
        with pytest.raises(ValidationError):
            RunResult(
                run_id="abc",
                status=RunStatus.SUCCEEDED,
                video_path="feed.mp4",
                frames_read=1,
                frames_analyzed=1,
                frames_skipped=0,
                valid_detections=1,
                rejected_detections=0,
                rejections_by_reason={},
                coverage=1.5,
                elapsed_seconds=1.0,
            )

    def test_rejection_reasons_are_a_closed_set(self):
        # Every reason the pipeline can record is enumerated, so a histogram key can
        # never be free-form text.
        assert {r.value for r in RejectionReason} == {
            "empty_mask",
            "no_contour",
            "below_min_area",
            "too_few_points",
            "degenerate_polygon",
            "low_confidence",
            "detector_error",
        }

    def test_delivery_states_are_distinct(self):
        assert len({s.value for s in DeliveryState}) == 3
