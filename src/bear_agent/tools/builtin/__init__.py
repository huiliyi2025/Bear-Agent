from bear_agent.tools.base import Tool
from bear_agent.tools.builtin.files import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from bear_agent.tools.builtin.search import GlobTool, GrepTool


def create_builtin_tools() -> list[Tool]:
    return [
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        ListDirTool(),
        GlobTool(),
        GrepTool(),
    ]


__all__ = [
    "EditFileTool",
    "GlobTool",
    "GrepTool",
    "ListDirTool",
    "ReadFileTool",
    "WriteFileTool",
    "create_builtin_tools",
]
