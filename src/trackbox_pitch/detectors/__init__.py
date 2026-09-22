"""Field detector implementations and their registry.

Importing this package makes every detector available by name. Adding a detector means
adding a module here and a matching ``Literal`` in the configuration model; a test
asserts the two lists agree, so they cannot drift apart silently.
"""

from __future__ import annotations

from . import green_threshold  # noqa: F401  (imported for its registration side effect)
from .base import (
    FieldDetector,
    available_detectors,
    build_detector,
    register,
)

__all__ = [
    "FieldDetector",
    "available_detectors",
    "build_detector",
    "register",
]
