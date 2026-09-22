"""Reporting progress and outcome to the platform, over the network.

The rule this module exists to enforce:

    a failure to reach the platform is not a failure of the video pipeline, and a
    failure of the video pipeline must not be hidden by a failure to report it.

Both directions are handled explicitly rather than by hoping:

* **Transport failures never propagate.** ``requests`` exceptions are caught here and
  returned as a :class:`DeliveryState`, so an unreachable endpoint cannot fail an
  otherwise healthy run.
* **A repeated failure opens a circuit breaker.** Past that point reports are dropped
  without attempting a connection, because otherwise a dead platform makes a long run
  progressively slower as timeouts accumulate.
* **The pipeline decides its own exit code first and reports afterwards.** A failure to
  report a failure therefore cannot mask the original.

One deliberate exception to "never raises": only ``requests.RequestException`` is
caught. A programming error -- a payload that will not serialise, say -- propagates,
because absorbing it would be exactly the "real failure silently absorbed" defect this
project is meant to remove. Payloads are validated models, so this should not happen;
if it does, it is a bug and it should be loud.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol

import requests
from pydantic import BaseModel

from .config import ReportingConfig
from .models import DeliveryState, JobEvent, ProgressReport

logger = logging.getLogger(__name__)


class Reporter(Protocol):
    """What the pipeline needs from a reporter. Keeps the pipeline testable."""

    def send_progress(self, report: ProgressReport) -> DeliveryState: ...

    def send_event(self, event: JobEvent) -> DeliveryState: ...

    @property
    def degraded(self) -> bool:
        """Whether anything has been lost. Surfaced in the final result."""
        ...

    @property
    def dropped(self) -> int:
        """How many reports were lost."""
        ...


class NullReporter:
    """Used when reporting is switched off. Keeps the pipeline free of branches."""

    def send_progress(self, report: ProgressReport) -> DeliveryState:
        return DeliveryState.DISABLED

    def send_event(self, event: JobEvent) -> DeliveryState:
        return DeliveryState.DISABLED

    @property
    def degraded(self) -> bool:
        return False

    @property
    def dropped(self) -> int:
        return 0


class HttpJobReporter:
    """Posts validated payloads to the platform's job endpoints."""

    PROGRESS_PATH = "/api/v1/jobs/progress"
    EVENTS_PATH = "/api/v1/jobs/events"

    def __init__(
        self,
        config: ReportingConfig,
        *,
        session: requests.Session | Any | None = None,
        clock: Any = time.monotonic,
        sleep: Any = time.sleep,
        breaker_threshold: int = 3,
        breaker_cooldown_seconds: float = 30.0,
        initial_backoff_seconds: float = 0.25,
    ) -> None:
        self._enabled = config.enabled
        self._api_url = config.api_url.rstrip("/")
        self._timeout = config.timeout_seconds
        self._max_attempts = config.max_attempts

        self._session = session if session is not None else requests.Session()
        # Injected so tests do not have to sleep or read the wall clock.
        self._clock = clock
        self._sleep = sleep

        self._breaker_threshold = breaker_threshold
        self._breaker_cooldown = breaker_cooldown_seconds
        self._initial_backoff = initial_backoff_seconds

        self._consecutive_failures = 0
        self._breaker_open_until = 0.0
        self._dropped = 0
        self._degradation_reported = False

    # -- public API --------------------------------------------------------------------

    def send_progress(self, report: ProgressReport) -> DeliveryState:
        return self._post(self.PROGRESS_PATH, report)

    def send_event(self, event: JobEvent) -> DeliveryState:
        return self._post(self.EVENTS_PATH, event)

    @property
    def degraded(self) -> bool:
        return self._dropped > 0

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def breaker_open(self) -> bool:
        return self._breaker_open_until > self._clock()

    # -- internals ---------------------------------------------------------------------

    def _post(self, path: str, payload: BaseModel) -> DeliveryState:
        if not self._enabled:
            return DeliveryState.DISABLED

        if self.breaker_open:
            self._dropped += 1
            return DeliveryState.DROPPED

        url = f"{self._api_url}{path}"
        body = payload.model_dump(mode="json")
        backoff = self._initial_backoff
        last_error = "no attempt made"

        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._session.post(url, json=body, timeout=self._timeout)
                if response.status_code < 400:
                    self._consecutive_failures = 0
                    self._breaker_open_until = 0.0
                    self._degradation_reported = False
                    return DeliveryState.DELIVERED

                last_error = f"HTTP {response.status_code}"
                if response.status_code < 500:
                    # A 4xx will not fix itself by being retried.
                    break
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"

            if attempt < self._max_attempts:
                self._sleep(backoff)
                backoff *= 2

        self._record_failure(path, last_error)
        return DeliveryState.DROPPED

    def _record_failure(self, path: str, error: str) -> None:
        self._dropped += 1
        self._consecutive_failures += 1

        if self._consecutive_failures >= self._breaker_threshold:
            self._breaker_open_until = self._clock() + self._breaker_cooldown

        # Logged once per degradation episode, not once per dropped report: a thousand
        # identical warnings would bury whatever else the run had to say.
        if not self._degradation_reported:
            self._degradation_reported = True
            logger.warning(
                "could not reach the reporting service at %s%s (%s); "
                "the pipeline will continue and the run will be marked degraded",
                self._api_url,
                path,
                error,
                extra={
                    "event": "reporting.degraded",
                    "api_url": self._api_url,
                    "error": error,
                },
            )


def build_reporter(config: ReportingConfig) -> Reporter:
    """Reporter for this configuration: a real one, or a do-nothing stand-in."""
    if not config.enabled:
        logger.info(
            "reporting disabled by configuration",
            extra={"event": "reporting.disabled"},
        )
        return NullReporter()
    return HttpJobReporter(config)
