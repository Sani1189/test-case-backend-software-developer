"""End to end, with the video, detector, and reporter all under control.

These are the tests that enforce the failure policy as a whole: what stops a run, what
does not, and whether the platform is told either way.
"""

from __future__ import annotations

import json

import pytest
from shapely.geometry import Polygon

from trackbox_pitch.config import load_settings
from trackbox_pitch.errors import ExitCode, InputError
from trackbox_pitch.models import (
    DeliveryState,
    DetectionOutcome,
    FieldDetection,
    JobEvent,
    ProgressReport,
    RejectionReason,
    RunStatus,
)
from trackbox_pitch.pipeline import run_pipeline


class StubDetector:
    """Finds a boundary on every ``every``-th frame, or never, or explodes."""

    detector_id = "stub"

    def __init__(self, *, every: int = 1, confidence: float = 0.9, explode_at: int | None = None):
        self.calls = 0
        self._every = every
        self._confidence = confidence
        self._explode_at = explode_at

    def detect(self, frame):
        self.calls += 1
        if self._explode_at is not None and self.calls == self._explode_at:
            raise RuntimeError("detector exploded")

        if self.calls % self._every:
            return DetectionOutcome.rejected(RejectionReason.EMPTY_MASK)

        height, width = frame.shape[:2]
        polygon = Polygon(
            [(0, 0), (width * 0.5, 0), (width * 0.5, height * 0.5), (0, height * 0.5)]
        )
        return DetectionOutcome.found(
            FieldDetection(
                detector_id=self.detector_id,
                frame_index=0,
                polygon=polygon,
                confidence=self._confidence,
            )
        )


class RecordingReporter:
    def __init__(self, *, degraded: bool = False) -> None:
        self.progress: list[ProgressReport] = []
        self.events: list[JobEvent] = []
        self._degraded = degraded

    def send_progress(self, report: ProgressReport) -> DeliveryState:
        self.progress.append(report)
        return DeliveryState.DROPPED if self._degraded else DeliveryState.DELIVERED

    def send_event(self, event: JobEvent) -> DeliveryState:
        self.events.append(event)
        return DeliveryState.DROPPED if self._degraded else DeliveryState.DELIVERED

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def dropped(self) -> int:
        return 1 if self._degraded else 0

    def event_names(self) -> list[str]:
        return [event.event for event in self.events]


@pytest.fixture
def settings(sample_video):
    return load_settings(
        "config/default.yaml",
        environ={},
        overrides={"video.path": str(sample_video), "reporting.enabled": False},
    )


class TestSuccessfulRun:
    def test_a_normal_run_succeeds(self, settings):
        result = run_pipeline(settings, detector=StubDetector(), reporter=RecordingReporter())

        assert result.status is RunStatus.SUCCEEDED
        assert result.valid_detections > 0
        assert result.failure is None

    def test_frame_accounting_is_consistent(self, settings):
        result = run_pipeline(settings, detector=StubDetector(), reporter=RecordingReporter())

        # The fixture is 180 frames at 30fps, sampled at 5fps => stride 6 => 30 samples.
        assert result.frames_read == 180
        assert result.frames_analyzed == 30
        assert result.frames_skipped == 150
        assert result.frames_read == result.frames_analyzed + result.frames_skipped

    def test_counts_add_up(self, settings):
        result = run_pipeline(
            settings, detector=StubDetector(every=2), reporter=RecordingReporter()
        )
        assert result.valid_detections + result.rejected_detections == result.frames_analyzed
        assert result.coverage == pytest.approx(result.valid_detections / result.frames_analyzed)

    def test_the_result_is_json_serialisable(self, settings):
        result = run_pipeline(settings, detector=StubDetector(), reporter=RecordingReporter())
        payload = json.loads(result.model_dump_json())
        assert payload["status"] == "succeeded"
        assert payload["consensus_boundary"]["coordinates"]

    def test_it_reports_start_and_completion(self, settings):
        reporter = RecordingReporter()
        run_pipeline(settings, detector=StubDetector(), reporter=reporter)

        assert reporter.event_names()[0] == "run.started"
        assert reporter.event_names()[-1] == "run.completed"

    def test_it_reports_progress_along_the_way(self, sample_video):
        """A run must be monitorable while it is in flight, not only once it ends.

        The cadence is tightened here because the fixture is only 30 scheduled frames;
        the shipped default reports every 250 frames or 10 seconds.
        """
        settings = load_settings(
            "config/default.yaml",
            environ={},
            overrides={
                "video.path": str(sample_video),
                "reporting.enabled": False,
                "reporting.progress_every_frames": 5,
            },
        )
        reporter = RecordingReporter()
        run_pipeline(settings, detector=StubDetector(), reporter=reporter)

        assert reporter.progress, "a run must be monitorable while it is in flight"
        assert len(reporter.progress) >= 5
        assert all(report.status is RunStatus.RUNNING for report in reporter.progress)
        assert all(report.run_id for report in reporter.progress)

    def test_progress_fractions_increase(self, sample_video):
        settings = load_settings(
            "config/default.yaml",
            environ={},
            overrides={
                "video.path": str(sample_video),
                "reporting.enabled": False,
                "reporting.progress_every_frames": 5,
            },
        )
        reporter = RecordingReporter()
        run_pipeline(settings, detector=StubDetector(), reporter=reporter)

        fractions = [report.processed_fraction for report in reporter.progress]
        assert fractions == sorted(fractions), "progress should not go backwards"
        assert fractions[-1] <= 1.0

    def test_the_detector_does_not_own_the_run_id(self, settings):
        reporter = RecordingReporter()
        result = run_pipeline(settings, detector=StubDetector(), reporter=reporter)
        assert all(report.run_id == result.run_id for report in reporter.progress)


class TestNothingFound:
    def test_finding_nothing_is_a_result_not_a_crash(self, settings):
        result = run_pipeline(
            settings, detector=StubDetector(every=10_000), reporter=RecordingReporter()
        )

        assert result.status is RunStatus.NO_VALID_DETECTIONS
        assert result.valid_detections == 0
        assert result.metric is None
        assert result.rejections_by_reason == {"empty_mask": 30}
        assert result.failure is None, "not finding anything is not a failure"

    def test_rejections_are_attributed_not_merely_counted(self, settings):
        result = run_pipeline(
            settings, detector=StubDetector(every=10_000), reporter=RecordingReporter()
        )
        assert sum(result.rejections_by_reason.values()) == result.rejected_detections


class TestFailurePolicy:
    def test_a_missing_video_is_an_input_error(self, sample_video):
        settings = load_settings(
            "config/default.yaml",
            environ={},
            overrides={"video.path": "definitely-not-here.mp4", "reporting.enabled": False},
        )
        with pytest.raises(InputError) as exc:
            run_pipeline(settings, detector=StubDetector(), reporter=RecordingReporter())
        assert exc.value.exit_code is ExitCode.INPUT

    def test_a_failure_is_reported_to_the_platform_before_it_propagates(self, sample_video):
        settings = load_settings(
            "config/default.yaml",
            environ={},
            overrides={"video.path": "definitely-not-here.mp4", "reporting.enabled": False},
        )
        reporter = RecordingReporter()

        with pytest.raises(InputError):
            run_pipeline(settings, detector=StubDetector(), reporter=reporter)

        assert "run.started" in reporter.event_names()
        assert "run.failed" in reporter.event_names()

        failure = next(e for e in reporter.events if e.event == "run.failed")
        assert failure.level == "ERROR"
        assert failure.detail["exit_code"] == int(ExitCode.INPUT)

    def test_an_unexpected_detector_error_is_never_absorbed(self, settings):
        """A bug is not a data gap. This is the prototype's `except Exception: pass`."""
        with pytest.raises(RuntimeError, match="detector exploded"):
            run_pipeline(
                settings,
                detector=StubDetector(explode_at=5),
                reporter=RecordingReporter(),
            )

    def test_an_unexpected_error_is_still_reported(self, settings):
        reporter = RecordingReporter()
        with pytest.raises(RuntimeError):
            run_pipeline(settings, detector=StubDetector(explode_at=5), reporter=reporter)

        failure = next(e for e in reporter.events if e.event == "run.failed")
        assert failure.detail["exit_code"] is None, "an unclassified error has no exit code"
        assert "detector exploded" in failure.message

    def test_a_failed_run_is_never_reported_as_completed(self, settings):
        reporter = RecordingReporter()
        with pytest.raises(RuntimeError):
            run_pipeline(settings, detector=StubDetector(explode_at=5), reporter=reporter)
        assert "run.completed" not in reporter.event_names()


class TestReportingDegradation:
    def test_a_healthy_run_survives_an_unreachable_platform(self, settings):
        result = run_pipeline(
            settings, detector=StubDetector(), reporter=RecordingReporter(degraded=True)
        )
        assert result.status is RunStatus.SUCCEEDED

    def test_degradation_is_recorded_in_the_result(self, settings):
        result = run_pipeline(
            settings, detector=StubDetector(), reporter=RecordingReporter(degraded=True)
        )
        assert result.reporting_degraded is True

    def test_a_healthy_reporter_is_not_marked_degraded(self, settings):
        result = run_pipeline(settings, detector=StubDetector(), reporter=RecordingReporter())
        assert result.reporting_degraded is False


class TestEarlyExit:
    def test_a_converging_run_stops_before_the_end(self, sample_video):
        settings = load_settings(
            "config/default.yaml",
            environ={},
            overrides={
                "video.path": str(sample_video),
                "reporting.enabled": False,
                "aggregation.early_exit": True,
                "aggregation.min_valid_samples": 2,
                "aggregation.relative_stderr_threshold": 1.0,
            },
        )
        detector = StubDetector()
        result = run_pipeline(settings, detector=detector, reporter=RecordingReporter())

        assert result.status is RunStatus.SUCCEEDED
        assert detector.calls < 30, "it should not have looked at every scheduled frame"
