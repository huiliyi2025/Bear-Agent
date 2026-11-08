"""Tool framework: abstraction, registry, and built-in implementations."""

from bear_agent.tools.base import Permission, Tool, ToolResult
from bear_agent.tools.builtin import builtin_tools
from bear_agent.tools.registry import ToolRegistry

__all__ = [
    "Permission",
    "Tool",
    "ToolRegistry",
    "ToolResult",
    "builtin_tools",
]
