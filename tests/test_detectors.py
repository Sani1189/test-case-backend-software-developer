"""The detector seam, and the green-threshold implementation behind it."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from trackbox_pitch.config import load_settings
from trackbox_pitch.detectors import FieldDetector, available_detectors, build_detector
from trackbox_pitch.detectors.base import register
from trackbox_pitch.errors import ConfigError
from trackbox_pitch.models import DetectionOutcome, RejectionReason

GREEN = np.zeros((240, 320, 3), dtype=np.uint8)
GREEN[:] = (34, 139, 34)


def settings_for_file(path="config/default.yaml"):
    return load_settings(path, environ={})


def frame_blank() -> np.ndarray:
    """A camera cut: the generator writes an all-black frame."""
    return np.zeros((240, 320, 3), dtype=np.uint8)


def frame_green() -> np.ndarray:
    """A close-up: the whole frame is pitch green."""
    return GREEN.copy()


def frame_noise() -> np.ndarray:
    """The injected false positive: a small white rectangle on green."""
    frame = GREEN.copy()
    rectangle = np.array([[10, 10], [40, 10], [40, 30], [10, 30]], np.int32)
    cv2.polylines(frame, [rectangle], True, (255, 255, 255), 2)
    return frame


def frame_pitch() -> np.ndarray:
    """A normal frame: the pitch outline drawn on green.

    The outline is a *closed* ring inside the canvas, mirroring what the real generator
    draws. That matters: a closed ring leaves the green connected, so the largest
    external contour is still the frame. An outline that runs off the canvas would clip
    into an open shape and split the green into separate components, which is not what
    a real frame looks like.
    """
    frame = GREEN.copy()
    outline = np.array([[40, 40], [280, 40], [300, 200], [20, 200]], np.int32)
    cv2.polylines(frame, [outline], True, (255, 255, 255), 3)
    return frame


class TestRegistry:
    def test_the_configured_detector_is_registered(self):
        settings = settings_for_file()
        assert settings.field_detector.type in available_detectors()

    def test_builds_the_detector_named_by_config(self):
        detector = build_detector(settings_for_file().field_detector)
        assert isinstance(detector, FieldDetector)
        assert detector.detector_id == "green_threshold"

    def test_unknown_name_raises_config_error_at_build_time(self):
        class FakeConfig:
            type = "does_not_exist"

        with pytest.raises(ConfigError, match="no detector registered"):
            build_detector(FakeConfig())

    def test_config_without_a_type_is_a_config_error(self):
        class NoType:
            pass

        with pytest.raises(ConfigError, match="no 'type' attribute"):
            build_detector(NoType())

    def test_the_config_literal_and_the_registry_agree(self):
        """The two places a detector name is declared must not drift apart.

        Configuration validates the name against a ``Literal``; the registry resolves it
        to an implementation. If those disagreed, a config would validate and then fail
        at wiring time -- exactly the late failure this design is meant to prevent.
        """
        from trackbox_pitch.config import GreenThresholdConfig

        literal_values = set(GreenThresholdConfig.model_fields["type"].annotation.__args__)
        assert literal_values == available_detectors()

    def test_duplicate_registration_is_refused(self):
        with pytest.raises(RuntimeError, match="already registered"):

            @register("green_threshold")
            def _duplicate(_config):  # pragma: no cover - never called
                return None


class TestGreenThresholdDetector:
    @pytest.fixture
    def detector(self):
        return build_detector(settings_for_file().field_detector)

    def test_blank_frame_is_rejected_as_an_empty_mask(self, detector):
        outcome = detector.detect(frame_blank())
        assert outcome.detection is None
        assert outcome.rejection is RejectionReason.EMPTY_MASK

    def test_a_pitch_frame_produces_a_detection(self, detector):
        outcome = detector.detect(frame_pitch())
        assert isinstance(outcome, DetectionOutcome)
        assert outcome.detection is not None
        assert outcome.detection.detector_id == "green_threshold"
        assert outcome.detection.area > 0

    def test_confidence_is_a_bounded_proxy(self, detector):
        detection = detector.detect(frame_pitch()).detection
        assert detection is not None
        assert 0.0 <= detection.confidence <= 1.0

    def test_known_limitation_the_boundary_is_the_frame_not_the_pitch(self, detector):
        """Documents the flaw rather than pretending it is not there.

        The background is solid green and the pitch is drawn as white lines on top, so
        the green mask covers the whole frame and the largest external contour is the
        frame edge. A normal frame, a close-up, and the injected noise frame therefore
        all yield the *same* polygon.

        If this test starts failing, someone has changed the detector's behaviour --
        which is allowed, but it must be a deliberate, documented change rather than an
        accident, because DECISIONS.md tells the reviewer this is a known limitation.
        """
        areas = {
            name: detector.detect(make()).detection.area
            for name, make in (
                ("normal", frame_pitch),
                ("close-up", frame_green),
                ("noise", frame_noise),
            )
        }
        assert len(set(areas.values())) == 1, f"areas diverged: {areas}"

    def test_min_area_rejects_small_contours(self):
        # A detector configured to need a huge contour cannot find one on a tiny frame.
        config = settings_for_file().field_detector.model_copy(update={"min_area": 10_000_000})
        detector = build_detector(config)
        outcome = detector.detect(frame_pitch())
        assert outcome.rejection is RejectionReason.BELOW_MIN_AREA

    def test_detector_constants_are_built_once_not_per_frame(self, detector):
        """The prototype rebuilt its HSV arrays on every frame."""
        lower_before = detector._lower
        detector.detect(frame_pitch())
        assert detector._lower is lower_before

    def test_an_empty_array_is_rejected_rather_than_crashing(self, detector):
        outcome = detector.detect(np.zeros((0, 0, 3), dtype=np.uint8))
        assert outcome.rejection is RejectionReason.EMPTY_MASK

    def test_the_detector_does_not_raise_on_any_generator_frame_type(self, detector):
        for make in (frame_blank, frame_green, frame_noise, frame_pitch):
            detector.detect(make())  # must not raise
