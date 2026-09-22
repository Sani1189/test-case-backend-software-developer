"""Failure taxonomy.

Every failure the pipeline can hit is classified here, once. Nothing is caught
without being classified, and nothing is absorbed silently.

The split that matters:

* :class:`PipelineError` and its subclasses **stop the run**. Each carries the exit
  code the process should terminate with.
* Recoverable conditions — a frame with no boundary in it, a report that could not be
  delivered — are deliberately **not** exceptions in this module. They are ordinary
  values (a rejection reason, a delivery status) so that a caller cannot accidentally
  treat them as fatal or, worse, swallow them with a bare ``except Exception``.

The prototype had no taxonomy at all, which is why a failure to open the video exited
0 and an unexpected error inside contour extraction was indistinguishable from "no
boundary found in this frame".
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """Process exit codes.

    Deliberately distinct from one another so an orchestrator can tell "bad config"
    from "bad video" from "ran fine but saw no pitch" without parsing log text.
    """

    OK = 0
    """Completed successfully."""

    UNEXPECTED = 1
    """An unclassified internal error. A bug."""

    CONFIG = 2
    """Configuration missing, unreadable, unparsable, or invalid."""

    INPUT = 3
    """The input video is absent, unopenable, or contains no frames."""

    STREAM = 4
    """The feed stopped early or could not be decoded part-way through."""

    NO_DETECTIONS = 5
    """The run completed but found no valid boundary at all.

    Not an exception: the run genuinely finished. The pipeline reports this as a
    result status and the entry point maps it to this code.
    """


class PipelineError(Exception):
    """Base class for failures that must stop the run."""

    exit_code: ExitCode = ExitCode.UNEXPECTED

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConfigError(PipelineError):
    """Configuration could not be loaded or failed validation.

    Raised before any video is opened, so a misconfigured run costs nothing.
    """

    exit_code = ExitCode.CONFIG


class InputError(PipelineError):
    """The input video cannot be opened, or yields no frames.

    Deliberately separate from :class:`ConfigError`: a path that is syntactically
    fine but points at a missing file is an input problem (exit 3), not a
    configuration problem (exit 2).
    """

    exit_code = ExitCode.INPUT


class StreamError(PipelineError):
    """The feed ended early, or a decode failed part-way through.

    The prototype treated any ``ret=False`` as a clean end-of-video, so a truncated
    or corrupt feed was reported as a successful run.
    """

    exit_code = ExitCode.STREAM
