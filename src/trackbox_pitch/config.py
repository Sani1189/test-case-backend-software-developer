"""Validated configuration.

Configuration is read once, at startup, and validated into immutable models. A bad
file stops the process before any video is opened -- never part-way through a run, and
never by quietly falling back to a default.

That last point is the whole reason this module exists. The prototype read its
configuration with ``dict.get(key, default)``, and the defaults had already drifted
away from the shipped values: ``sport`` fell back to ``"soccer"`` while the file said
``"football"``, and ``min_area`` fell back to ``500`` while the file said ``1000``.
Nobody would have noticed, because nothing failed.

Design notes:

* No field has an implicit default. If it is not in the file and not in the
  environment, loading fails and says so.
* ``extra="forbid"`` on every model. A misspelled key is a mistake worth failing on;
  silently ignoring ``min_are`` would run the pipeline with a value the operator never
  chose. The cost is that adding a field is a breaking change for older configs --
  a deliberate trade, recorded in ``DECISIONS.md``.
* Environment overrides are validated through the same models. An override is not a
  way to bypass validation.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .errors import ConfigError

# Applied to every model: unknown keys are rejected, and instances are immutable once
# built so nothing downstream can mutate shared configuration.
_STRICT = ConfigDict(extra="forbid", frozen=True)

# OpenCV's HSV ranges for an 8-bit image. Not the conventional 0-360/0-100/0-100
# ranges, which is a classic source of silently-wrong thresholds.
_HSV_HUE_RANGE = (0, 179)
_HSV_CHANNEL_RANGE = (0, 255)


class VideoConfig(BaseModel):
    """The input feed, and how much of it to look at."""

    model_config = _STRICT

    path: Path = Field(description="Input video to consume.")

    target_fps: float = Field(
        gt=0,
        le=240,
        description=(
            "Upper bound on how many frames per second of source are analysed. The "
            "scheduler derives a stride from this against the stream's real frame "
            "rate, so processing cost tracks the work actually needed rather than "
            "the length of the file. In the prototype this setting existed but was "
            "never read, and every frame was decoded."
        ),
    )


class GreenThresholdConfig(BaseModel):
    """Parameters for the colour-threshold field detector.

    ``type`` doubles as the registry key, so an unknown detector name fails at load
    time with a clear message instead of at the first frame. The prototype carried a
    ``type`` field that no code ever read.
    """

    model_config = _STRICT

    type: Literal["green_threshold"] = Field(
        description="Detector id. Must match a registered detector implementation."
    )
    sport: str = Field(min_length=1, description="Sport this tuned detector targets.")
    min_area: int = Field(
        gt=0,
        description="Contours smaller than this are rejected before any geometry work.",
    )
    hsv_lower: tuple[int, int, int] = Field(description="Lower HSV bound (h, s, v).")
    hsv_upper: tuple[int, int, int] = Field(description="Upper HSV bound (h, s, v).")

    @model_validator(mode="after")
    def _validate_hsv_band(self) -> GreenThresholdConfig:
        channels = ("hue", "saturation", "value")
        limits = (_HSV_HUE_RANGE, _HSV_CHANNEL_RANGE, _HSV_CHANNEL_RANGE)

        for index, (channel, (low, high)) in enumerate(zip(channels, limits, strict=True)):
            for label, band in (("hsv_lower", self.hsv_lower), ("hsv_upper", self.hsv_upper)):
                if not low <= band[index] <= high:
                    raise ValueError(
                        f"{label}[{index}] ({channel}) must be between {low} and {high}, "
                        f"got {band[index]}"
                    )

        for index, channel in enumerate(channels):
            if self.hsv_lower[index] > self.hsv_upper[index]:
                raise ValueError(
                    f"hsv_lower[{index}] ({channel}) must not exceed hsv_upper[{index}] "
                    f"({channel}): {self.hsv_lower[index]} > {self.hsv_upper[index]}"
                )

        return self


class AggregationConfig(BaseModel):
    """How per-frame detections become a final result.

    Replaces the prototype's approach of appending every polygon to a list that grew
    for the whole run.
    """

    model_config = _STRICT

    min_confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Detections below this confidence are rejected and counted. The prototype "
            "stored a threshold like this and never read it."
        ),
    )
    consensus_polygon_window: int = Field(
        gt=0,
        description="Detections retained for the consensus polygon, keeping memory bounded.",
    )
    early_exit: bool = Field(
        description=(
            "Stop once the running metric has converged. Off by default: it is a real "
            "throughput win on long feeds but a real coverage risk, so it is opt-in."
        )
    )
    min_valid_samples: int = Field(
        gt=0, description="Valid detections required before convergence can be considered."
    )
    relative_stderr_threshold: float = Field(
        gt=0.0,
        description="Relative standard error at which the metric counts as converged.",
    )

    @model_validator(mode="after")
    def _validate_early_exit(self) -> AggregationConfig:
        if self.early_exit and self.min_valid_samples < 2:
            raise ValueError(
                "min_valid_samples must be at least 2 when early_exit is enabled; "
                "standard error is undefined for a single sample"
            )
        return self


class ReportingConfig(BaseModel):
    """How the run reports itself to the platform."""

    model_config = _STRICT

    enabled: bool = Field(description="When false, reporting is skipped entirely.")
    api_url: str = Field(description="Base URL of the reporting service.")
    timeout_seconds: float = Field(
        gt=0,
        le=60,
        description=(
            "Per-request timeout. Kept short so an unreachable platform cannot stall a batch job."
        ),
    )
    max_attempts: int = Field(
        ge=1, le=10, description="Attempts per report before backing off and giving up."
    )
    progress_every_frames: int = Field(
        gt=0, description="Emit a progress report after this many analysed frames."
    )
    progress_every_seconds: float = Field(
        gt=0, description="Or after this long, whichever comes first."
    )

    @field_validator("api_url")
    @classmethod
    def _validate_api_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"must start with http:// or https://, got {value!r}")
        return value.rstrip("/")


class LoggingConfig(BaseModel):
    """Observability settings.

    The prototype's ``debug_mode: True`` is replaced by a real log level, because a
    boolean could not distinguish "quiet" from "diagnosable".
    """

    model_config = _STRICT

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        description="Minimum level emitted."
    )
    format: Literal["json", "console"] = Field(
        description="json for the batch environment; console for a human at a terminal."
    )


class Settings(BaseModel):
    """The complete, validated configuration for one run."""

    model_config = _STRICT

    video: VideoConfig
    field_detector: GreenThresholdConfig
    aggregation: AggregationConfig
    reporting: ReportingConfig
    logging: LoggingConfig


# Environment variables that may override a value from the file. An explicit allowlist
# rather than a general prefix sweep: an override that silently reinterpreted an
# arbitrary key would reintroduce exactly the implicit-behaviour problem this module
# exists to remove. Values are strings here and are coerced by the same validators the
# file goes through, so `TRACKBOX_TARGET_FPS=abc` fails the same way the file would.
_ENV_OVERRIDES: Mapping[str, str] = {
    "TRACKBOX_VIDEO_PATH": "video.path",
    "TRACKBOX_TARGET_FPS": "video.target_fps",
    "TRACKBOX_API_URL": "reporting.api_url",
    "TRACKBOX_LOG_LEVEL": "logging.level",
    "TRACKBOX_LOG_FORMAT": "logging.format",
}


def _read_config_file(path: Path) -> dict[str, Any]:
    """Read and parse the config file, raising ConfigError on any problem."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except IsADirectoryError as exc:
        raise ConfigError(f"configuration path is a directory, not a file: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"configuration file could not be read: {path} ({exc})") from exc

    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"configuration file is not valid YAML: {path}\n{exc}") from exc

    if parsed is None:
        raise ConfigError(f"configuration file is empty: {path}")
    if not isinstance(parsed, dict):
        raise ConfigError(
            f"configuration must be a mapping at the top level, got {type(parsed).__name__}: {path}"
        )
    return parsed


def _apply_overrides(raw: dict[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``raw`` with dotted-path overrides applied.

    Both environment variables and command-line flags funnel through here, so an
    override cannot take a route that skips validation. A dotted path that names a
    section which does not exist yet (``a.b`` when ``a`` is absent) creates it, and the
    resulting structure is then validated like any other -- an override that introduces
    an unknown key still fails.
    """
    merged = copy.deepcopy(raw)

    for dotted_key, value in overrides.items():
        *parents, leaf = dotted_key.split(".")
        cursor = merged
        for key in parents:
            section = cursor.get(key)
            if not isinstance(section, dict):
                section = {}
                cursor[key] = section
            cursor = section
        cursor[leaf] = value

    return merged


def _env_overrides(environ: Mapping[str, str]) -> dict[str, Any]:
    """Collect the allowlisted environment overrides that are actually set."""
    return {
        dotted_key: environ[variable]
        for variable, dotted_key in _ENV_OVERRIDES.items()
        # An unset or blank variable means "no override", never "use an empty value".
        if environ.get(variable, "").strip()
    }


def _format_validation_error(exc: ValidationError, source: str) -> str:
    """Turn a pydantic ValidationError into an operator-readable message."""
    lines = [f"invalid configuration in {source}:"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)


def load_settings(
    path: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Settings:
    """Load, override, and validate configuration for a run.

    Precedence, lowest to highest: the configuration file, environment variables,
    explicit ``overrides`` (command-line flags). Every layer is validated by the same
    models, so no layer can introduce a value the file would have been rejected for.

    Args:
        path: Path to the YAML configuration file.
        environ: Environment to read overrides from. Defaults to ``os.environ``;
            injectable so tests do not have to mutate process state.
        overrides: Additional dotted-path overrides, e.g. ``{"video.path": "x.mp4"}``.

    Returns:
        A fully validated, immutable :class:`Settings`.

    Raises:
        ConfigError: The file is missing, unreadable, unparsable, or fails validation.
            Raised before any input is opened, so a misconfigured run costs nothing.
    """
    config_path = Path(path)
    raw = _read_config_file(config_path)
    raw = _apply_overrides(raw, _env_overrides(os.environ if environ is None else environ))
    if overrides:
        raw = _apply_overrides(raw, overrides)

    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc, str(config_path))) from exc
