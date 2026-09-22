"""Command-line entry point.

Thin by design: parse arguments, load and validate configuration, run the pipeline, and
map the outcome onto an exit code. Everything interesting lives in the library, so the
same run can be driven from a test, a notebook, or an orchestrator without going
through a subprocess.

Command-line flags are applied as configuration overrides and validated by exactly the
same models the file goes through. A flag cannot set a value the file would have been
rejected for.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from typing import Any

import trackbox_pitch

from .config import load_settings
from .errors import ConfigError, ExitCode, PipelineError
from .logging_setup import configure_logging
from .models import RunStatus
from .pipeline import run_pipeline

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = "config/default.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pitch-pipeline",
        description=(
            "Detect the playing-field boundary across a video feed and report the "
            "outcome to the platform."
        ),
    )
    parser.add_argument(
        "-c",
        "--config",
        default=_DEFAULT_CONFIG,
        help=f"Path to the configuration file (default: {_DEFAULT_CONFIG}).",
    )
    parser.add_argument("--video", help="Override the input video path.")
    parser.add_argument(
        "--target-fps",
        type=float,
        help="Override how many frames per second of source are analysed.",
    )
    parser.add_argument(
        "--api-url",
        help="Override the reporting service base URL.",
    )
    parser.add_argument(
        "--no-reporting",
        action="store_true",
        help="Skip reporting entirely (useful for local runs with no platform running).",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Override the log level.",
    )
    parser.add_argument(
        "--log-format",
        choices=["json", "console"],
        help="Override the log format.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {trackbox_pitch.__version__}",
    )
    return parser


def _overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """Translate parsed flags into dotted-path configuration overrides."""
    overrides: dict[str, Any] = {}

    if args.video:
        overrides["video.path"] = args.video
    if args.target_fps is not None:
        overrides["video.target_fps"] = args.target_fps
    if args.api_url:
        overrides["reporting.api_url"] = args.api_url
    if args.no_reporting:
        overrides["reporting.enabled"] = False
    if args.log_level:
        overrides["logging.level"] = args.log_level
    if args.log_format:
        overrides["logging.format"] = args.log_format

    return overrides


def main(argv: Sequence[str] | None = None) -> int:
    """Run one job. Returns the process exit code."""
    args = build_parser().parse_args(argv)

    try:
        settings = load_settings(args.config, overrides=_overrides_from_args(args))
    except ConfigError as exc:
        # Logging has not been configured yet -- the configuration that says where logs
        # go is the thing that just failed -- so this goes to stderr, where it cannot be
        # swallowed by a broken log setup.
        print(f"error: {exc}", file=sys.stderr)
        return int(ExitCode.CONFIG)

    configure_logging(settings.logging)

    try:
        result = run_pipeline(settings)
    except PipelineError as exc:
        logger.error(
            "run failed: %s",
            exc.message,
            extra={"event": "run.failed", "exit_code": int(exc.exit_code)},
        )
        return int(exc.exit_code)
    except Exception:
        # Unclassified. Never absorbed: this is a bug and it ends the run.
        logger.exception("run failed with an unclassified error")
        return int(ExitCode.UNEXPECTED)

    if result.status is RunStatus.SUCCEEDED:
        return int(ExitCode.OK)

    # Completed, but found nothing usable. A distinct code so an orchestrator can tell
    # it apart from a crash without reading logs.
    return int(ExitCode.NO_DETECTIONS)


if __name__ == "__main__":
    sys.exit(main())
