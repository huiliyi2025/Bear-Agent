"""MCP client integration.

Public surface:

* :class:`MCPManager` — lifecycle, discovery, tool calls and status.
* :class:`MCPTool` — ``Tool`` adapter registered in the tool registry.
* :class:`MCPServerStatus` — secret-free per-server status snapshot.
"""

from bear_agent.mcp.manager import MCPManager, MCPServerStatus
from bear_agent.mcp.tools import MCPTool, namespaced_tool_name

__all__ = [
    "MCPManager",
    "MCPServerStatus",
    "MCPTool",
    "namespaced_tool_name",
]
