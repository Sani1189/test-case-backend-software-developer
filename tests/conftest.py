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
