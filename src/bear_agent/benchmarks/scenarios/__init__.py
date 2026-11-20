"""Built-in benchmark scenarios, grouped by category."""

from __future__ import annotations

from typing import Any

from bear_agent.benchmarks.scenarios.core import (
    context_scenarios,
    kernel_and_tool_scenarios,
)
from bear_agent.benchmarks.scenarios.e2e import e2e_scenarios
from bear_agent.benchmarks.scenarios.mcp import mcp_scenarios
from bear_agent.benchmarks.scenarios.providers import provider_scenarios
from bear_agent.benchmarks.scenarios.tasks import task_scenarios


def all_scenarios() -> list[Any]:
    return [
        *kernel_and_tool_scenarios(),
        *context_scenarios(),
        *task_scenarios(),
        *provider_scenarios(),
        *mcp_scenarios(),
        *e2e_scenarios(),
    ]


__all__ = ["all_scenarios"]
