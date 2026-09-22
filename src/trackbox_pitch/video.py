"""Frame scheduling: deciding which frames are worth decoding.

The prototype opened the capture and called ``read()`` in a ``while True`` loop, which
decodes every frame of the file. That makes cost a function of file length. The
scheduler here makes cost a function of how much of the feed actually needs inspecting.

Two distinct savings:

1. **Sampling.** A stride is derived from the stream's real frame rate against the
   configured ``target_fps``, so a 30fps feed analysed at 5fps inspects roughly one
   frame in six. The prototype's ``target_fps`` setting existed but was never read.

2. **Not decoding what is skipped.** Skipped frames are advanced with ``grab()``, which
   demuxes without decoding to a BGR array. ``read()`` is ``grab()`` plus ``retrieve()``,
   and ``retrieve()`` is the expensive half. Avoiding it is a real reduction in work --
   though an honest one, not a free lunch: on inter-frame codecs you still cannot jump
   past a frame without demuxing it, and ``CAP_PROP_POS_FRAMES`` seeking is not
   reliably frame-accurate across containers. See ``DECISIONS.md``.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .errors import InputError, StreamError

logger = logging.getLogger(__name__)

#: How far short of the reported length a stream may fall before it counts as
#: truncated rather than merely imprecise. Container frame counts are approximate.
DEFAULT_TRUNCATION_TOLERANCE = 0.02


@dataclass(frozen=True, slots=True)
class StreamInfo:
    """What the container told us about the feed."""

    path: Path
    fps: float
    frame_count: int
    width: int
    height: int
    stride: int

    @property
    def expected_samples(self) -> int:
        """How many frames the schedule should yield."""
        if self.frame_count <= 0:
            return 0
        return math.ceil(self.frame_count / self.stride)


@dataclass(frozen=True, slots=True)
class SampledFrame:
    """One frame the schedule selected."""

    index: int
    """Position in the source stream, not in the sampled sequence."""

    frame: np.ndarray


class FrameScheduler:
    """Iterates the frames worth analysing, skipping the rest as cheaply as possible.

    Use as a context manager::

        with FrameScheduler(path, target_fps) as schedule:
            for sampled in schedule.frames():
                ...
    """

    def __init__(
        self,
        path: str | Path,
        target_fps: float,
        *,
        truncation_tolerance: float = DEFAULT_TRUNCATION_TOLERANCE,
    ) -> None:
        self._path = Path(path)
        self._target_fps = target_fps
        self._truncation_tolerance = truncation_tolerance

        self._capture: cv2.VideoCapture | None = None
        self.info: StreamInfo | None = None

        self.frames_read: int = 0
        """Frames advanced past, analysed or not."""

        self.frames_skipped: int = 0
        """Frames advanced past without decoding."""

    def __enter__(self) -> FrameScheduler:
        self._open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def _open(self) -> None:
        if not self._path.exists():
            raise InputError(f"input video not found: {self._path}")

        capture = cv2.VideoCapture(str(self._path))
        if not capture.isOpened():
            capture.release()
            raise InputError(f"could not open input video: {self._path}")

        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if frame_count <= 0:
            capture.release()
            raise InputError(
                f"input video reports no frames, so there is nothing to analyse: {self._path}"
            )

        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)

        if fps > 0:
            stride = max(1, math.ceil(fps / self._target_fps))
        else:
            # Conservative direction: an unknown frame rate means analyse everything,
            # which costs more but never discards data. Logged rather than silent --
            # an operator needs to know why a run is slower than the config implies.
            stride = 1
            logger.warning(
                "stream reports no frame rate; analysing every frame instead of every "
                "1/%s of a second",
                self._target_fps,
                extra={"event": "video.fps_unknown", "path": str(self._path)},
            )

        self._capture = capture
        self.info = StreamInfo(
            path=self._path,
            fps=fps,
            frame_count=frame_count,
            width=width,
            height=height,
            stride=stride,
        )

        logger.info(
            "opened stream: %s frames at %.2f fps, analysing with stride %d",
            frame_count,
            fps,
            stride,
            extra={
                "event": "video.opened",
                "frame_count": frame_count,
                "fps": fps,
                "stride": stride,
                "expected_samples": self.info.expected_samples,
            },
        )

    def frames(self) -> Iterator[SampledFrame]:
        """Yield the frames the schedule selects, in order.

        Raises:
            StreamError: A selected frame failed to decode, the resolution changed
                mid-stream, or the stream ended materially before its reported length.
                All three mean the feed is not what it claimed to be, and reporting a
                truncated run as a success is the failure mode being avoided.
        """
        if self._capture is None or self.info is None:
            raise RuntimeError("FrameScheduler.frames() used outside its context manager")

        capture = self._capture
        info = self.info

        index = 0
        analysed = 0
        skipped = 0
        expected_shape: tuple[int, ...] | None = None

        while True:
            if not capture.grab():
                break

            if index % info.stride == 0:
                ok, frame = capture.retrieve()
                if not ok:
                    raise StreamError(
                        f"frame {index} of {self._path} could not be decoded; "
                        f"the feed is corrupt at this point"
                    )

                if expected_shape is None:
                    expected_shape = frame.shape
                elif frame.shape != expected_shape:
                    raise StreamError(
                        f"resolution changed mid-stream at frame {index} of {self._path}: "
                        f"expected {expected_shape[1]}x{expected_shape[0]}, "
                        f"got {frame.shape[1]}x{frame.shape[0]}. Mixing coordinate spaces "
                        f"would silently invalidate every metric, so the run stops instead."
                    )

                yield SampledFrame(index=index, frame=frame)
                analysed += 1
            else:
                skipped += 1

            index += 1

        self.frames_read = index
        self.frames_skipped = skipped

        logger.debug(
            "stream exhausted: read %d frames, analysed %d, skipped %d",
            index,
            analysed,
            skipped,
            extra={"event": "video.exhausted", "frames_read": index, "frames_analysed": analysed},
        )

        self._check_not_truncated(index)

    def _check_not_truncated(self, frames_read: int) -> None:
        """Distinguish a clean end-of-file from a stream that stopped early.

        The prototype treated any ``ret=False`` as "the video finished", so a truncated
        or corrupt feed was reported as a completed run with a healthy frame count.
        """
        assert self.info is not None
        reported = self.info.frame_count

        if reported <= 0:
            return

        shortfall = reported - frames_read
        if shortfall > reported * self._truncation_tolerance:
            raise StreamError(
                f"stream ended early: read {frames_read} of {reported} reported frames "
                f"({shortfall} missing, more than the {self._truncation_tolerance:.0%} "
                f"tolerance). Treating this as a truncated feed rather than a complete run."
            )
