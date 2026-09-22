"""Reporting: the two failure directions must never convert into each other."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import requests

from trackbox_pitch.config import load_settings
from trackbox_pitch.models import DeliveryState, JobEvent, ProgressReport, RunStatus
from trackbox_pitch.reporting import HttpJobReporter, NullReporter, build_reporter


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class FakeSession:
    """Returns scripted outcomes in order. An ``Exception`` outcome is raised."""

    def __init__(self, outcomes: list[object] | None = None) -> None:
        self._outcomes = list(outcomes or [])
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, json: dict | None = None, timeout: float | None = None):
        self.calls.append((url, json or {}))
        outcome = self._outcomes.pop(0) if self._outcomes else FakeResponse(200)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def reporting_config(**overrides):
    base = load_settings("config/default.yaml", environ={}).reporting
    return base.model_copy(update=overrides) if overrides else base


def a_progress() -> ProgressReport:
    return ProgressReport(
        run_id="run-1",
        status=RunStatus.RUNNING,
        frames_read=10,
        frames_analyzed=10,
        frames_skipped=0,
        valid_detections=9,
        rejected_detections=1,
        elapsed_seconds=1.0,
    )


def an_event() -> JobEvent:
    return JobEvent(
        run_id="run-1",
        event="run.started",
        level="INFO",
        message="started",
        timestamp=datetime.now(UTC),
    )


def build(session: FakeSession, clock: FakeClock | None = None, **overrides) -> HttpJobReporter:
    return HttpJobReporter(
        reporting_config(**overrides),
        session=session,
        clock=clock or FakeClock(),
        sleep=lambda _seconds: None,
        initial_backoff_seconds=0.0,
    )


class TestDelivery:
    def test_a_successful_post_is_delivered(self):
        session = FakeSession([FakeResponse(200)])
        assert build(session).send_progress(a_progress()) is DeliveryState.DELIVERED
        assert len(session.calls) == 1

    def test_progress_and_events_go_to_their_own_endpoints(self):
        session = FakeSession([FakeResponse(200), FakeResponse(200)])
        reporter = build(session)
        reporter.send_progress(a_progress())
        reporter.send_event(an_event())

        urls = [url for url, _ in session.calls]
        assert urls[0].endswith("/api/v1/jobs/progress")
        assert urls[1].endswith("/api/v1/jobs/events")

    def test_the_payload_is_the_validated_model(self):
        session = FakeSession([FakeResponse(200)])
        build(session).send_progress(a_progress())
        _, body = session.calls[0]
        # Enum members are serialised as their values, not as reprs.
        assert body["status"] == "running"
        assert body["run_id"] == "run-1"


class TestRetries:
    def test_a_server_error_is_retried(self):
        session = FakeSession([FakeResponse(500), FakeResponse(500), FakeResponse(200)])
        assert build(session, max_attempts=3).send_progress(a_progress()) is DeliveryState.DELIVERED
        assert len(session.calls) == 3

    def test_a_client_error_is_not_retried(self):
        """A 4xx will not fix itself, so retrying only wastes the run's time."""
        session = FakeSession([FakeResponse(404), FakeResponse(200)])
        result = build(session, max_attempts=3).send_progress(a_progress())
        assert result is DeliveryState.DROPPED
        assert len(session.calls) == 1

    def test_exhausting_attempts_drops_rather_than_raises(self):
        session = FakeSession([FakeResponse(503)] * 10)
        reporter = build(session, max_attempts=3)
        assert reporter.send_progress(a_progress()) is DeliveryState.DROPPED
        assert len(session.calls) == 3


class TestTransportFailureIsNeverFatal:
    def test_an_unreachable_platform_does_not_raise(self):
        session = FakeSession([requests.ConnectionError("refused")] * 10)
        result = build(session, max_attempts=2).send_progress(a_progress())
        assert result is DeliveryState.DROPPED

    def test_a_timeout_does_not_raise(self):
        session = FakeSession([requests.Timeout("too slow")] * 10)
        assert build(session, max_attempts=2).send_event(an_event()) is DeliveryState.DROPPED

    def test_degradation_is_visible_in_the_health_flags(self):
        session = FakeSession([requests.ConnectionError("refused")] * 10)
        reporter = build(session, max_attempts=1)
        assert reporter.degraded is False

        reporter.send_progress(a_progress())
        assert reporter.degraded is True
        assert reporter.dropped == 1

    def test_a_later_success_clears_the_breaker(self):
        session = FakeSession([requests.ConnectionError("x"), FakeResponse(200)])
        reporter = build(session, max_attempts=1)
        reporter.send_progress(a_progress())
        assert reporter.degraded is True

        reporter.send_progress(a_progress())
        assert reporter.breaker_open is False


class TestCircuitBreaker:
    def test_repeated_failure_opens_the_breaker(self):
        session = FakeSession([requests.ConnectionError("x")] * 100)
        reporter = build(session, max_attempts=1)

        for _ in range(3):
            reporter.send_progress(a_progress())
        calls_before = len(session.calls)

        assert reporter.breaker_open is True
        # Past the threshold, reports are dropped without touching the network, so a
        # dead platform cannot make a long run slower and slower.
        assert reporter.send_progress(a_progress()) is DeliveryState.DROPPED
        assert len(session.calls) == calls_before

    def test_the_breaker_closes_after_the_cooldown(self):
        clock = FakeClock()
        session = FakeSession([requests.ConnectionError("x")] * 100)
        reporter = build(session, clock=clock, max_attempts=1)

        for _ in range(3):
            reporter.send_progress(a_progress())
        assert reporter.breaker_open is True

        clock.now += 3600.0
        assert reporter.breaker_open is False
        reporter.send_progress(a_progress())
        assert len(session.calls) == 4


class TestDisabledReporting:
    def test_disabled_yields_a_null_reporter(self):
        config = reporting_config(enabled=False)
        assert isinstance(build_reporter(config), NullReporter)

    def test_the_null_reporter_reports_nothing_and_is_never_degraded(self):
        reporter = NullReporter()
        assert reporter.send_progress(a_progress()) is DeliveryState.DISABLED
        assert reporter.send_event(an_event()) is DeliveryState.DISABLED
        assert reporter.degraded is False
        assert reporter.dropped == 0

    def test_enabled_yields_the_http_reporter(self):
        assert isinstance(build_reporter(reporting_config()), HttpJobReporter)

    def test_a_disabled_http_reporter_makes_no_calls(self):
        session = FakeSession([FakeResponse(200)])
        reporter = build(session, enabled=False)
        assert reporter.send_progress(a_progress()) is DeliveryState.DISABLED
        assert session.calls == []


class TestUrlHandling:
    def test_a_trailing_slash_does_not_produce_a_double_slash(self):
        session = FakeSession([FakeResponse(200)])
        reporter = HttpJobReporter(
            reporting_config(api_url="http://host:5000/"),
            session=session,
            sleep=lambda _s: None,
        )
        reporter.send_progress(a_progress())
        url, _ = session.calls[0]
        assert url == "http://host:5000/api/v1/jobs/progress"

    @pytest.mark.parametrize("status", [200, 201, 204, 302])
    def test_anything_below_400_counts_as_delivered(self, status):
        session = FakeSession([FakeResponse(status)])
        assert build(session).send_progress(a_progress()) is DeliveryState.DELIVERED
