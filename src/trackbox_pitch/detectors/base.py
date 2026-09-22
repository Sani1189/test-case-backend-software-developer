"""The one seam that flexes: how a playing-field boundary is found in a frame.

Everything sport- and deployment-specific collapses behind :class:`FieldDetector`.
Deliberately a single method:

The prototype split masking (``_extract_mask``) from polygon derivation
(``_derive_polygon_from_mask``). That looks like a contract but is really one
implementation's internal steps -- a learned model returns a polygon directly and has
no mask stage at all. Keeping both would force every future detector to pretend it has
one.

Only this is abstracted. Video reading, aggregation, and reporting each have exactly
one real implementation, and abstracting them would add indirection without buying a
second case.

Detectors are resolved by name from configuration, so an unknown detector is a startup
failure rather than a surprise on the first frame.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

import numpy as np

from ..errors import ConfigError
from ..models import DetectionOutcome

DetectorFactory = Callable[[Any], "FieldDetector"]

_FACTORIES: dict[str, DetectorFactory] = {}


@runtime_checkable
class FieldDetector(Protocol):
    """Finds the playing-field boundary in a single frame.

    Implementations must be cheap to construct (constants built once in ``__init__``)
    and must not raise for ordinary "nothing here" frames -- that is what
    :meth:`DetectionOutcome.rejected` is for.
    """

    detector_id: str

    def detect(self, frame: np.ndarray) -> DetectionOutcome:
        """Return a detection, or a reason there is none.

        Args:
            frame: A single BGR frame.

        Returns:
            :class:`DetectionOutcome` carrying exactly one of a detection or a
            rejection reason.
        """
        ...


def register(detector_type: str) -> Callable[[DetectorFactory], DetectorFactory]:
    """Decorator registering a factory under the config value of ``type``.

    Raises:
        RuntimeError: A detector is already registered under this name. Two
            implementations claiming one name would make behaviour depend on import
            order, which is not a thing to discover in production.
    """

    def _decorator(factory: DetectorFactory) -> DetectorFactory:
        if detector_type in _FACTORIES:
            raise RuntimeError(f"a detector is already registered as {detector_type!r}")
        _FACTORIES[detector_type] = factory
        return factory

    return _decorator


def available_detectors() -> frozenset[str]:
    """Names that configuration may legally reference."""
    return frozenset(_FACTORIES)


def build_detector(config: Any) -> FieldDetector:
    """Instantiate the detector named by ``config.type``.

    Args:
        config: A validated detector configuration carrying a ``type`` attribute.

    Raises:
        ConfigError: No detector is registered under that name. Raised at startup so
            the failure costs nothing, rather than surfacing on the first frame.
    """
    detector_type = getattr(config, "type", None)
    if detector_type is None:
        raise ConfigError(f"detector configuration has no 'type' attribute: {config!r}")

    try:
        factory = _FACTORIES[detector_type]
    except KeyError:
        raise ConfigError(
            f"no detector registered for type {detector_type!r}; "
            f"available: {sorted(_FACTORIES) or 'none'}"
        ) from None

    return factory(config)
