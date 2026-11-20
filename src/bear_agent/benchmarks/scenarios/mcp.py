"""MCP lifecycle, discovery and call benchmarks using a real stdio server."""

from __future__ import annotations

import contextlib
import json
import sys
from typing import Any, ClassVar

from bear_agent.benchmarks.models import BenchmarkContext, BenchmarkSample, BenchmarkSkipped
from bear_agent.config.models import MCPConfig, MCPServerConfig
from bear_agent.mcp.manager import MCPManager
from bear_agent.secrets.store import secret_store_from_config
from bear_agent.tools.registry import ToolRegistry


def _server_config() -> MCPServerConfig:
    return MCPServerConfig(
        transport="stdio",
        command=sys.executable,
        args=["-I", "-m", "bear_agent.benchmarks.mcp_echo_server"],
        trusted=True,
        required=True,
        timeout_s=20.0,
        startup_timeout_s=20.0,
    )


def _manager(ctx: BenchmarkContext, names: list[str]) -> tuple[MCPManager, ToolRegistry]:
    registry = ToolRegistry()
    manager = MCPManager(
        MCPConfig(enabled=True, servers={name: _server_config() for name in names}),
        registry,
        workspace=ctx.workspace,
        server_cwd=ctx.data_dir / "mcp",
        redactor=ctx.redactor,
        secret_store=secret_store_from_config(ctx.settings.secrets.backend),
    )
    return manager, registry


class MCPInitializeScenario:
    id = "mcp.initialize"
    name = "MCP initialize/discover"
    description = "Spawn a real stdio MCP server, initialize it and register its tools."
    category = "mcp"
    kind = "synthetic"
    requires_network = False
    default_params: ClassVar[dict[str, Any]] = {}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        return None

    async def cleanup(self, ctx: BenchmarkContext) -> None:
        manager = ctx.state.get("manager")
        if manager is not None:
            with contextlib.suppress(Exception):
                await manager.stop()

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        manager, registry = _manager(ctx, ["bench"])
        ctx.state["manager"] = manager
        try:
            statuses = await manager.start()
        except Exception as exc:
            raise BenchmarkSkipped(f"MCP server failed to start: {exc}") from exc
        status = statuses["bench"]
        if status.state != "ready":
            raise BenchmarkSkipped(f"MCP server not ready: {status.last_error}")
        return BenchmarkSample(
            operations=1,
            metadata={"tools": len(status.tools), "registered": len(registry.names())},
        )


class MCPCallScenario:
    id = "mcp.call"
    name = "MCP tool call"
    description = "Call a discovered MCP tool over a persistent stdio connection."
    category = "mcp"
    kind = "synthetic"
    requires_network = False
    default_params = {"count": 20}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        manager, registry = _manager(ctx, ["bench"])
        try:
            statuses = await manager.start()
        except Exception as exc:
            await manager.stop()
            raise BenchmarkSkipped(f"MCP server failed to start: {exc}") from exc
        if statuses["bench"].state != "ready":
            await manager.stop()
            raise BenchmarkSkipped(f"MCP server not ready: {statuses['bench'].last_error}")
        ctx.state.update(manager=manager, registry=registry)

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        manager: MCPManager = ctx.state["manager"]
        count = int(ctx.params.get("count", 20))
        for index in range(count):
            result = await manager.call_tool("bench", "echo", {"text": f"benchmark {index}"})
            if result.get("is_error") or f"benchmark {index}" not in json.dumps(result):
                raise RuntimeError("MCP echo did not return the expected text")
        return BenchmarkSample(operations=count)

    async def cleanup(self, ctx: BenchmarkContext) -> None:
        manager = ctx.state.get("manager")
        if manager is not None:
            await manager.stop()


class MCPMultiServerScenario:
    id = "mcp.multi_server"
    name = "Multiple MCP servers"
    description = "Start two MCP servers concurrently and namespace their tools."
    category = "mcp"
    kind = "synthetic"
    requires_network = False
    default_params: ClassVar[dict[str, Any]] = {}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        return None

    async def cleanup(self, ctx: BenchmarkContext) -> None:
        manager = ctx.state.get("manager")
        if manager is not None:
            with contextlib.suppress(Exception):
                await manager.stop()

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        manager, registry = _manager(ctx, ["alpha", "beta"])
        ctx.state["manager"] = manager
        try:
            statuses = await manager.start()
        except Exception as exc:
            raise BenchmarkSkipped(f"MCP servers failed to start: {exc}") from exc
        if any(status.state != "ready" for status in statuses.values()):
            raise BenchmarkSkipped("one or more MCP servers failed to start")
        registered = set(registry.names())
        expected = {
            "mcp__alpha__echo",
            "mcp__alpha__add",
            "mcp__beta__echo",
            "mcp__beta__add",
        }
        if registered != expected:
            raise RuntimeError(f"MCP namespacing is inconsistent: {sorted(registered)}")
        return BenchmarkSample(
            operations=1,
            metadata={
                "servers": len(statuses),
                "tools": len(registered),
                "registered": sorted(registered),
            },
        )


class MCPReconnectScenario:
    id = "mcp.reconnect"
    name = "MCP reconnect"
    description = "Stop and restart one MCP server connection and rediscover tools."
    category = "mcp"
    kind = "synthetic"
    requires_network = False
    default_params: ClassVar[dict[str, Any]] = {}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        manager, registry = _manager(ctx, ["bench"])
        try:
            await manager.start()
        except Exception as exc:
            await manager.stop()
            raise BenchmarkSkipped(f"MCP server failed to start: {exc}") from exc
        ctx.state.update(manager=manager, registry=registry)

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        manager: MCPManager = ctx.state["manager"]
        status = await manager.reconnect("bench")
        if status.state != "ready":
            raise RuntimeError(f"reconnect failed: {status.last_error}")
        return BenchmarkSample(operations=1, metadata={"tools": len(status.tools)})

    async def cleanup(self, ctx: BenchmarkContext) -> None:
        manager = ctx.state.get("manager")
        if manager is not None:
            await manager.stop()


def mcp_scenarios() -> list[Any]:
    return [
        MCPInitializeScenario(),
        MCPCallScenario(),
        MCPMultiServerScenario(),
        MCPReconnectScenario(),
    ]
