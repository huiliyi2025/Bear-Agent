"""Pytest fixtures for Bear-Agent."""

from __future__ import annotations

from pathlib import Path

import pytest

from bear_agent.config.models import Settings
from tests.support import default_settings


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "hello.txt").write_text("hello from bear\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    return root


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return default_settings(tmp_path)
