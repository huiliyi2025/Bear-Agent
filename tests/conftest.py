"""Shared fixtures for the test suite."""

import pytest

from bear_agent.tools.builtin import builtin_tools


@pytest.fixture
def builtin(tmp_path):
    """Built-in tools keyed by name, bound to a fresh temp working directory."""
    return {tool.name: tool for tool in builtin_tools(tmp_path)}
