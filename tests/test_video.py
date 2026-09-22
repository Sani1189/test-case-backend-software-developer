"""Frame scheduling: which frames get decoded, and how the stream can lie.

Some of these use the real synthetic fixture; the failure cases use a fake capture,
because a genuinely truncated MP4 is hard to construct reliably and a hand-made one
would test the file rather than the logic.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from trackbox_pitch.errors import InputError, StreamError
from trackbox_pitch.video import FrameScheduler


class FakeCapture:
    """Stands in for ``cv2.VideoCapture`` so failure modes can be dictated exactly."""

    def __init__(
        self,
        frames: list[np.ndarray],
        *,
        fps: float = 30.0,
        reported_frame_count: int | None = None,
        fail_retrieve_at: int | None = None,
    ) -> None:
        self._frames = frames
        self._fps = fps
        self._reported = reported_frame_count if reported_frame_count is not None else len(frames)
        self._fail_retrieve_at = fail_retrieve_at
        self._position = 0
        self.released = False

    def isOpened(self) -> bool:  # noqa: N802 -- mirrors the OpenCV API
        return True

    def get(self, prop: int) -> float:  # noqa: N802
        if prop == cv2.CAP_PROP_FPS:
            return self._fps
        if prop == cv2.CAP_PROP_FRAME_COUNT:
            return float(self._reported)
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self._frames[0].shape[1]) if self._frames else 0.0
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self._frames[0].shape[0]) if self._frames else 0.0
        return 0.0

    def grab(self) -> bool:  # noqa: N802
        if self._position >= len(self._frames):
            return False
        self._position += 1
        return True

    def retrieve(self):  # noqa: N802
        index = self._position - 1
        if index == self._fail_retrieve_at:
            return False, None
        return True, self._frames[index]

    def release(self) -> None:
        self.released = True


def green_frames(count: int, *, size=(240, 320)) -> list[np.ndarray]:
    frame = np.zeros((*size, 3), dtype=np.uint8)
    frame[:] = (34, 139, 34)
    return [frame.copy() for _ in range(count)]


def install(monkeypatch: pytest.MonkeyPatch, capture: FakeCapture) -> None:
    monkeypatch.setattr(cv2, "VideoCapture", lambda *_args, **_kwargs: capture)


class TestOpening:
    """Opening happens in ``__enter__``, so a failure surfaces on entering the context
    rather than on construction. That keeps construction cheap and side-effect free."""

    def test_missing_file_is_an_input_error(self, tmp_path):
        with (
            pytest.raises(InputError, match="not found"),
            FrameScheduler(tmp_path / "absent.mp4", target_fps=5.0),
        ):
            pass

    def test_unopenable_file_is_an_input_error(self, tmp_path, monkeypatch):
        path = tmp_path / "broken.mp4"
        path.write_bytes(b"not a video")

        class NeverOpens(FakeCapture):
            def isOpened(self) -> bool:  # noqa: N802
                return False

        install(monkeypatch, NeverOpens(green_frames(1)))
        with (
            pytest.raises(InputError, match="could not open"),
            FrameScheduler(path, target_fps=5.0),
        ):
            pass

    def test_stream_reporting_no_frames_is_an_input_error(self, tmp_path, monkeypatch):
        path = tmp_path / "empty.mp4"
        path.write_bytes(b"x")
        install(monkeypatch, FakeCapture(green_frames(0), reported_frame_count=0))
        with (
            pytest.raises(InputError, match="no frames"),
            FrameScheduler(path, target_fps=5.0),
        ):
            pass


class TestStride:
    def test_stride_is_derived_from_the_real_frame_rate(self, sample_video):
        with FrameScheduler(sample_video, target_fps=5.0) as schedule:
            assert schedule.info is not None
            # 30fps source analysed at 5fps: one frame in six.
            assert schedule.info.fps == pytest.approx(30.0)
            assert schedule.info.stride == 6
            assert schedule.info.expected_samples == 30

    def test_analysing_at_source_rate_means_stride_one(self, sample_video):
        with FrameScheduler(sample_video, target_fps=30.0) as schedule:
            assert schedule.info is not None
            assert schedule.info.stride == 1

    def test_a_higher_target_than_source_still_yields_stride_one(self, sample_video):
        with FrameScheduler(sample_video, target_fps=120.0) as schedule:
            assert schedule.info is not None
            assert schedule.info.stride == 1

    def test_only_multiples_of_the_stride_are_analysed(self, sample_video):
        with FrameScheduler(sample_video, target_fps=5.0) as schedule:
            frames = list(schedule.frames())
        assert len(frames) == 30
        assert all(f.index % 6 == 0 for f in frames)

    def test_skipped_frames_are_counted(self, sample_video):
        with FrameScheduler(sample_video, target_fps=5.0) as schedule:
            list(schedule.frames())
            assert schedule.frames_read == 180
            assert schedule.frames_skipped == 150

    def test_the_capture_is_released(self, sample_video):
        with FrameScheduler(sample_video, target_fps=5.0) as schedule:
            list(schedule.frames())
        assert schedule._capture is None


class TestUnknownFrameRate:
    def test_unknown_fps_falls_back_to_analysing_every_frame(self, tmp_path, monkeypatch):
        """The conservative direction: more work, but no data silently discarded."""
        path = tmp_path / "feed.mp4"
        path.write_bytes(b"x")
        install(monkeypatch, FakeCapture(green_frames(4), fps=0.0))

        with FrameScheduler(path, target_fps=5.0) as schedule:
            assert schedule.info is not None
            assert schedule.info.stride == 1
            assert len(list(schedule.frames())) == 4


class TestStreamIntegrity:
    def test_a_truncated_stream_is_not_a_successful_run(self, tmp_path, monkeypatch):
        """The prototype treated any ret=False as a clean end-of-file."""
        path = tmp_path / "feed.mp4"
        path.write_bytes(b"x")
        # Claims a thousand frames, delivers ten.
        install(monkeypatch, FakeCapture(green_frames(10), reported_frame_count=1000))

        with (
            FrameScheduler(path, target_fps=5.0) as schedule,
            pytest.raises(StreamError, match="ended early"),
        ):
            list(schedule.frames())

    def test_a_small_shortfall_is_tolerated(self, tmp_path, monkeypatch):
        """Container frame counts are approximate, so near-misses are not failures."""
        path = tmp_path / "feed.mp4"
        path.write_bytes(b"x")
        install(monkeypatch, FakeCapture(green_frames(100), reported_frame_count=101))

        with FrameScheduler(path, target_fps=5.0) as schedule:
            assert len(list(schedule.frames())) > 0

    def test_an_undecodable_frame_is_a_stream_error(self, tmp_path, monkeypatch):
        path = tmp_path / "feed.mp4"
        path.write_bytes(b"x")
        # Sampling at source rate means stride 1, so frame 5 really is retrieved.
        install(monkeypatch, FakeCapture(green_frames(20), fail_retrieve_at=5))

        with (
            FrameScheduler(path, target_fps=30.0) as schedule,
            pytest.raises(StreamError, match="could not be decoded"),
        ):
            list(schedule.frames())

    def test_a_resolution_change_mid_stream_stops_the_run(self, tmp_path, monkeypatch):
        """Mixing coordinate spaces would silently invalidate every metric."""
        path = tmp_path / "feed.mp4"
        path.write_bytes(b"x")
        frames = green_frames(10, size=(240, 320)) + green_frames(10, size=(480, 640))
        install(monkeypatch, FakeCapture(frames))

        with (
            FrameScheduler(path, target_fps=120.0) as schedule,
            pytest.raises(StreamError, match="resolution changed"),
        ):
            list(schedule.frames())


class TestContract:
    def test_iterating_outside_the_context_manager_is_a_programming_error(self, sample_video):
        schedule = FrameScheduler(sample_video, target_fps=5.0)
        with pytest.raises(RuntimeError, match="outside its context manager"):
            list(schedule.frames())
