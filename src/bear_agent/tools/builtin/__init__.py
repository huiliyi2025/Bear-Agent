from bear_agent.tools.base import Tool
from bear_agent.tools.builtin.files import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from bear_agent.tools.builtin.search import GlobTool, GrepTool


def create_builtin_tools(*, shell: Tool | None = None) -> list[Tool]:
    tools: list[Tool] = [
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        ListDirTool(),
        GlobTool(),
        GrepTool(),
    ]
    if shell is not None:
        tools.append(shell)
    return tools


__all__ = [
    "EditFileTool",
    "GlobTool",
    "GrepTool",
    "ListDirTool",
    "ReadFileTool",
    "WriteFileTool",
    "create_builtin_tools",
]
