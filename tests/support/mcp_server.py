"""A real MCP stdio/streamable-http server used by the integration tests.

Run directly (``python tests/support/mcp_server.py``) to expose the server over
stdio. Tests spawn this file in a subprocess through ``MCPManager``. Nothing is
printed to stdout outside the MCP protocol; the SDK owns stdout and writes
diagnostics to stderr.
"""

from __future__ import annotations

import asyncio

from mcp.server.mcpserver import MCPServer

server = MCPServer(
    name="bear-test-mcp",
    title="Bear-Agent test MCP server",
    version="1.0.0",
    # Tool failures are expected in tests; keep server-side tracebacks off stderr
    # so pytest output stays readable.
    log_level="CRITICAL",
)


@server.tool(name="echo", description="Echo text back unchanged")
async def echo(text: str) -> str:
    return text


@server.tool(name="add", description="Add two integers")
async def add(a: int, b: int) -> int:
    return a + b


@server.tool(name="fail", description="Always raise an error")
async def fail(message: str) -> str:
    raise ValueError(message)


@server.tool(name="slow", description="Sleep for the requested number of seconds")
async def slow(seconds: float) -> str:
    await asyncio.sleep(seconds)
    return f"slept {seconds}"


if __name__ == "__main__":
    asyncio.run(server.run_stdio_async())
