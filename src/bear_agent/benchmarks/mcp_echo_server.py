"""Minimal MCP server used by the MCP benchmarks.

Run as ``python -I -m bear_agent.benchmarks.mcp_echo_server``. Only the MCP SDK
writes to stdout; the benchmark harness never parses human output from here.
"""

from __future__ import annotations

import asyncio

from mcp.server.mcpserver import MCPServer

server = MCPServer(
    name="bear-bench-mcp",
    title="Bear-Agent benchmark MCP server",
    version="1.0.0",
    log_level="CRITICAL",
)


@server.tool(name="echo", description="Echo text back unchanged")
async def echo(text: str) -> str:
    return text


@server.tool(name="add", description="Add two integers")
async def add(a: int, b: int) -> int:
    return a + b


if __name__ == "__main__":
    asyncio.run(server.run_stdio_async())
