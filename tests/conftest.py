"""Shared test fixtures.

Config tests intentionally mutate a *dict* loaded from the shipped configuration
rather than doing string surgery on the YAML text. String replacement breaks the
moment someone reflows the file; dict mutation keeps the tests describing the mistake
being made (``video.target_fps = -1``) instead of the characters involved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_CONFIG = REPO_ROOT / "config" / "default.yaml"


@pytest.fixture(scope="session")
def shipped_config_text() -> str:
    """The configuration file shipped in the repository, as text."""
    return SHIPPED_CONFIG.read_text(encoding="utf-8")


@pytest.fixture
def config_data(shipped_config_text: str) -> dict[str, Any]:
    """The shipped configuration as a mutable dict, one fresh copy per test."""
    data = yaml.safe_load(shipped_config_text)
    assert isinstance(data, dict)
    return data


@pytest.fixture
def write_config(tmp_path: Path):
    """Write config content to a temp file and return its path.

    Accepts either a mapping (dumped to YAML) or raw text, so tests can also supply
    content that is deliberately not valid YAML.
    """

    def _write(content: dict[str, Any] | list[Any] | str) -> Path:
        path = tmp_path / f"config-{uuid4().hex[:8]}.yaml"
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_text(yaml.safe_dump(content), encoding="utf-8")
        return path

    return _write


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A short synthetic feed, generated once for the whole session.

    Deliberately tiny: 180 frames at 30fps. The tests that use it care about scheduling
    and plumbing, not about detection quality, and a smaller fixture keeps the suite
    fast enough to actually run.
    """
    from tools.synthetic_generator import generate_synthetic_video

    path = tmp_path_factory.mktemp("video") / "feed.mp4"
    generate_synthetic_video(str(path), numFrames=180)
    return path


@pytest.fixture(scope="session")
def uniform_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A feed with no green in it at all, for the found-nothing path."""

    def _make(colour: tuple[int, int, int], name: str) -> Path:
        import cv2
        import numpy as np

        path = tmp_path_factory.mktemp("uniform") / name
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 30.0, (320, 240))
        frame = np.full((240, 320, 3), colour, dtype=np.uint8)
        for _ in range(60):
            writer.write(frame)
        writer.release()
        return path

    return _make((255, 255, 255), "white.mp4")
