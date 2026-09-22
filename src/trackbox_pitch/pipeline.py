"""Orchestration: one job, from opening the video to reporting its outcome.

Sequencing, counters, and policy live here. Concrete detection, concrete transport, and
argument parsing do not -- the pipeline depends on a :class:`FieldDetector` and a
:class:`Reporter`, both injected, which is what makes it testable without a video file
or a network.

The failure policy is the point of this module:

* A frame with no boundary is an ordinary outcome, counted with its reason.
* A truncated feed, an unreadable input, or a bug stops the run.
* Whatever happens, the platform is told -- and if it cannot be told, the run still ends
  with an exit code that reflects the *pipeline's* outcome.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from .aggregation import BoundaryAggregator
from .config import Settings
from .detectors import FieldDetector, build_detector
from .errors import PipelineError
from .logging_setup import set_run_id
from .models import (
    BoundarySummary,
    JobEvent,
    ProgressReport,
    RunResult,
    RunStatus,
)
from .reporting import Reporter, build_reporter
from .video import FrameScheduler, StreamInfo

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def run_pipeline(
    settings: Settings,
    *,
    detector: FieldDetector | None = None,
    reporter: Reporter | None = None,
    run_id: str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> RunResult:
    """Analyse one feed and report the outcome.

    Args:
        settings: Validated configuration.
        detector: Override for the configured detector. Used by tests.
        reporter: Override for the configured reporter. Used by tests.
        run_id: Correlation id. Generated when not supplied.
        clock: Monotonic clock, injectable so tests need not wait.

    Returns:
        The :class:`RunResult` for the run. A run that found nothing usable still
        returns normally -- that is a result, not an error.

    Raises:
        PipelineError: An input, stream, or configuration failure. The platform is told
            first, on a best-effort basis, then the error propagates so the entry point
            can map it to the documented exit code.
        Exception: Any unclassified error, after the same best-effort notification.
            Never absorbed: an unknown failure is a bug and must be visible.
    """
    run_id = run_id or uuid4().hex[:12]
    set_run_id(run_id)

    reporter = reporter if reporter is not None else build_reporter(settings.reporting)
    detector = detector if detector is not None else build_detector(settings.field_detector)

    started = clock()

    logger.info(
        "starting analysis of %s using the %s detector",
        settings.video.path,
        detector.detector_id,
        extra={
            "event": "run.started",
            "video": str(settings.video.path),
            "detector": detector.detector_id,
            "target_fps": settings.video.target_fps,
        },
    )
    reporter.send_event(
        JobEvent(
            run_id=run_id,
            event="run.started",
            level="INFO",
            message=f"analysis started for {settings.video.path}",
            timestamp=_now(),
            detail={
                "detector": detector.detector_id,
                "target_fps": settings.video.target_fps,
                "video": str(settings.video.path),
            },
        )
    )

    aggregator: BoundaryAggregator | None = None
    stream_info: StreamInfo | None = None
    last_index = -1

    try:
        with FrameScheduler(settings.video.path, settings.video.target_fps) as schedule:
            stream_info = schedule.info
            last_progress_at = started

            for sampled in schedule.frames():
                last_index = sampled.index

                if aggregator is None:
                    aggregator = BoundaryAggregator(
                        settings.aggregation,
                        sampled.frame.shape[1],
                        sampled.frame.shape[0],
                    )

                outcome = detector.detect(sampled.frame)
                if outcome.detection is not None:
                    # The detector does not know where it is in the stream, so the real
                    # index is stamped here.
                    aggregator.observe(
                        outcome.detection.model_copy(update={"frame_index": sampled.index})
                    )
                else:
                    aggregator.reject(outcome.rejection)

                now = clock()
                due_by_count = aggregator.analyzed % settings.reporting.progress_every_frames == 0
                due_by_time = (now - last_progress_at) >= settings.reporting.progress_every_seconds

                if due_by_count or due_by_time:
                    _report_progress(reporter, run_id, aggregator, now - started, stream_info)
                    last_progress_at = now

                if aggregator.converged():
                    logger.info(
                        "metric converged after %d samples; stopping early",
                        aggregator.analyzed,
                        extra={"event": "run.converged", "samples": aggregator.analyzed},
                    )
                    break

            # Set by the scheduler when it exhausts the stream. Left at zero when the
            # loop broke early, in which case the last frame actually seen is the truth.
            frames_read = schedule.frames_read or (last_index + 1)

    except PipelineError as exc:
        _report_failure(reporter, run_id, exc, clock() - started)
        raise
    except Exception as exc:
        logger.exception("run failed with an unclassified error")
        _report_failure(reporter, run_id, exc, clock() - started)
        raise

    if aggregator is None:
        # The schedule yielded nothing at all. Still produce a well-formed result rather
        # than returning None and making every caller handle a special case.
        aggregator = BoundaryAggregator(
            settings.aggregation,
            stream_info.width if stream_info else 0,
            stream_info.height if stream_info else 0,
        )

    summary = aggregator.summary()
    elapsed = clock() - started

    status = RunStatus.SUCCEEDED if summary.valid > 0 else RunStatus.NO_VALID_DETECTIONS

    result = RunResult(
        run_id=run_id,
        status=status,
        video_path=str(settings.video.path),
        frames_read=frames_read,
        frames_analyzed=summary.analyzed,
        frames_skipped=max(0, frames_read - summary.analyzed),
        valid_detections=summary.valid,
        rejected_detections=summary.rejected,
        rejections_by_reason=summary.rejections_by_reason,
        coverage=summary.coverage,
        metric=summary.metric,
        consensus_boundary=(
            BoundarySummary.from_detection(summary.consensus) if summary.consensus else None
        ),
        elapsed_seconds=elapsed,
        # A success that the platform never heard about is a visible fact, not silence.
        reporting_degraded=reporter.degraded,
    )

    log = logger.info if status is RunStatus.SUCCEEDED else logger.warning
    log(
        "run finished: %s",
        status.value,
        extra={
            "event": "run.finished",
            "status": status.value,
            "valid_detections": summary.valid,
            "rejected_detections": summary.rejected,
            "coverage": summary.coverage,
            "elapsed_seconds": round(elapsed, 3),
            "reporting_degraded": reporter.degraded,
        },
    )

    reporter.send_event(
        JobEvent(
            run_id=run_id,
            event="run.completed",
            level="INFO" if status is RunStatus.SUCCEEDED else "WARNING",
            message=f"run finished: {status.value}",
            timestamp=_now(),
            detail=result.model_dump(mode="json"),
        )
    )

    return result


def _report_progress(
    reporter: Reporter,
    run_id: str,
    aggregator: BoundaryAggregator,
    elapsed: float,
    stream_info: StreamInfo | None,
) -> None:
    expected = stream_info.expected_samples if stream_info else 0
    fraction = min(1.0, aggregator.analyzed / expected) if expected else None

    reporter.send_progress(
        ProgressReport(
            run_id=run_id,
            status=RunStatus.RUNNING,
            frames_read=aggregator.analyzed,
            frames_analyzed=aggregator.analyzed,
            frames_skipped=0,
            valid_detections=aggregator.valid,
            rejected_detections=aggregator.rejected,
            elapsed_seconds=elapsed,
            processed_fraction=fraction,
        )
    )


def _report_failure(reporter: Reporter, run_id: str, error: BaseException, elapsed: float) -> None:
    """Best-effort notification that the run failed.

    Deliberately tolerant: this is called while unwinding from the real failure, and a
    transport problem here must not replace the original exception with a reporting one.
    The reporter already reduces transport failures to a status value rather than
    raising, so nothing is caught here beyond the guarantee that a failure to report a
    failure never becomes the failure.
    """
    message = f"{type(error).__name__}: {error}"
    exit_code = int(error.exit_code) if isinstance(error, PipelineError) else None

    try:
        reporter.send_event(
            JobEvent(
                run_id=run_id,
                event="run.failed",
                level="ERROR",
                message=message,
                timestamp=_now(),
                detail={"exit_code": exit_code, "elapsed_seconds": round(elapsed, 3)},
            )
        )
    except Exception:  # noqa: BLE001 -- see docstring
        logger.warning("could not report the failure to the platform", exc_info=True)
