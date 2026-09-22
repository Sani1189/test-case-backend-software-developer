"""Structured logging for a process nobody is watching.

The prototype's entire observability was ``print()``. That is readable by a human
staring at a console, which is precisely what an unattended batch job does not have.
Diagnosing a failure afterwards means the output has to be greppable and machine
readable, so every record here is one JSON object on one line.

Each record carries a run id so that records from successive or concurrent runs can be
told apart, plus whatever structured fields the call site supplied via ``extra=``.

Named ``logging_setup`` rather than ``logging`` so that a reader (and an import) never
has to wonder whether ``import logging`` inside this package means the standard library
or this module. It is the standard library, but not having to check is better.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, TextIO

from .config import LoggingConfig

_run_id: ContextVar[str] = ContextVar("run_id", default="-")


def set_run_id(run_id: str) -> None:
    """Bind the run id that subsequent log records from this context will carry."""
    _run_id.set(run_id)


def get_run_id() -> str:
    return _run_id.get()


#: Attributes log records always carry. Anything else present came from the call site's
#: ``extra=`` and is worth emitting rather than discarding.
_STANDARD_ATTRIBUTES = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "stacklevel",
        "taskName",
        "thread",
        "threadName",
    }
)


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_ATTRIBUTES and not key.startswith("_")
    }


class JsonFormatter(logging.Formatter):
    """Renders each record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "run_id": _run_id.get(),
            "message": record.getMessage(),
        }
        payload.update(_extra_fields(record))

        if record.exc_info:
            # The traceback is what makes a failure diagnosable after the fact, so it
            # is kept rather than reduced to the exception's message.
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Readable single-line output for a human at a terminal."""

    def format(self, record: logging.LogRecord) -> str:
        line = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {record.getMessage()}"

        extra = _extra_fields(record)
        if extra:
            line += "  " + " ".join(f"{key}={value}" for key, value in extra.items())

        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)

        return line


def configure_logging(config: LoggingConfig, *, stream: TextIO | None = None) -> None:
    """Install the configured handler on the root logger.

    Replaces any existing handlers rather than adding to them, so calling this twice
    (which the tests and the CLI both do) cannot produce duplicate lines.
    """
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter() if config.format == "json" else ConsoleFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(config.level)
