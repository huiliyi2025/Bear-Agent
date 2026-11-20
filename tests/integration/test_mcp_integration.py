"""End-to-end MCP integration tests.

These tests spawn the real stdio server in ``tests/support/mcp_server.py`` and
one real Streamable HTTP server (in-process uvicorn) — no MCP protocol mocking.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import sys
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from tests.support import make_container

from bear_agent.config.models import MCPConfig, MCPServerConfig, Settings
from bear_agent.core.cancel import CancelToken, RunCancelled
from bear_agent.core.errors import MCPToolError, MCPUnavailable
from bear_agent.core.messages import ToolCall
from bear_agent.core.modes import PermissionMode
from bear_agent.mcp.manager import MCPManager
from bear_agent.mcp.tools import MCPArguments
from bear_agent.policy.path_guard import PathGuard
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.tools.base import Tool, ToolContext, ToolResult
from bear_agent.tools.executor import ToolExecutor
from bear_agent.tools.registry import ToolRegistry

SERVER_SCRIPT = Path(__file__).resolve().parents[1] / "support" / "mcp_server.py"


def stdio_config(**overrides: Any) -> MCPServerConfig:
    values: dict[str, Any] = {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(SERVER_SCRIPT)],
        "trusted": True,
        "required": True,
        "timeout_s": 15.0,
        "startup_timeout_s": 30.0,
        "env": {"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
    }
    values.update(overrides)
    return MCPServerConfig(**values)


@pytest.fixture
async def mcp_managers(
    workspace: Path,
) -> AsyncIterator[Callable[..., Any]]:
    created: list[MCPManager] = []

    async def make(
        servers: dict[str, MCPServerConfig],
        *,
        registry: ToolRegistry | None = None,
        redactor: SecretRedactor | None = None,
    ) -> tuple[MCPManager, ToolRegistry]:
        tool_registry = registry if registry is not None else ToolRegistry()
        manager = MCPManager(
            MCPConfig(servers=servers),
            tool_registry,
            workspace=workspace,
            redactor=redactor or SecretRedactor(),
        )
        created.append(manager)
        return manager, tool_registry

    yield make
    for manager in created:
        await manager.stop()


class DummyTool(Tool):
    """Pre-registered tool used to provoke a name collision."""

    name = "mcp__calc__add"
    description = "existing local tool"
    args_model = MCPArguments

    async def run(self, args: Any, ctx: ToolContext) -> ToolResult:
        return ToolResult(content="local")


def _content_text(payload: dict[str, Any]) -> str:
    return "\n".join(
        str(block.get("text", ""))
        for block in payload.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    )


# ---------------------------------------------------------------- stdio


async def test_stdio_discovery_namespacing_and_schema(
    mcp_managers: Callable[..., Any],
) -> None:
    manager, registry = await mcp_managers({"calc": stdio_config()})
    statuses = await manager.start()

    status = statuses["calc"]
    assert status.state == "ready"
    assert status.transport == "stdio"
    assert status.required is True
    assert status.last_error is None
    assert status.reconnect_count == 0
    assert set(status.tools) == {
        "mcp__calc__echo",
        "mcp__calc__add",
        "mcp__calc__fail",
        "mcp__calc__slow",
    }
    assert set(registry.names()) == set(status.tools)

    add = registry.get("mcp__calc__add")
    assert add is not None
    assert add.spec.name == "mcp__calc__add"
    assert add.spec.parameters["properties"]["a"]["type"] == "integer"
    assert set(add.spec.parameters["required"]) == {"a", "b"}
    assert "calc" in add.description and "add" in add.description
    assert add.side_effect.value == "exec"
    assert add.idempotent is False

    echo = registry.get("mcp__calc__echo")
    assert echo is not None
    assert echo.spec.parameters["properties"]["text"]["type"] == "string"


async def test_stdio_calls_echo_add_and_error(
    mcp_managers: Callable[..., Any],
) -> None:
    manager, registry = await mcp_managers({"calc": stdio_config()})
    await manager.start()

    echo = await manager.call_tool("calc", "echo", {"text": "hello mcp"})
    assert echo["is_error"] is False
    assert _content_text(echo) == "hello mcp"
    assert echo["structured_content"] == {"result": "hello mcp"}

    added = await manager.call_tool("calc", "add", {"a": 2, "b": 40})
    assert added["is_error"] is False
    assert _content_text(added) == "42"
    assert added["structured_content"] == {"result": 42}

    failed = await manager.call_tool("calc", "fail", {"message": "boom"})
    assert failed["is_error"] is True
    assert isinstance(failed["content"], list) and failed["content"]

    echo_tool = registry.get("mcp__calc__echo")
    assert echo_tool is not None
    ctx = ToolContext(
        cwd=Path.cwd(),
        session_id="test",
        cancel=CancelToken(),
        path_guard=PathGuard(manager.workspace),
        approved=True,
    )
    result = await echo_tool.run(echo_tool.args_model.model_validate({"text": "via adapter"}), ctx)
    assert not result.is_error
    assert "via adapter" in result.content
    assert result.meta["server"] == "calc"
    assert result.meta["tool"] == "echo"
    assert result.meta["structured"] is True


async def test_stdio_timeout_does_not_poison_connection(
    mcp_managers: Callable[..., Any],
) -> None:
    manager, _registry = await mcp_managers({"calc": stdio_config(timeout_s=0.75)})
    await manager.start()
    started = time.monotonic()
    with pytest.raises(MCPToolError, match="timed out"):
        await manager.call_tool("calc", "slow", {"seconds": 5})
    elapsed = time.monotonic() - started
    assert elapsed < 4.0

    # The SDK is not retried/restarted; the same session must remain usable.
    echo = await manager.call_tool("calc", "echo", {"text": "still alive"})
    assert _content_text(echo) == "still alive"


async def test_stdio_cancellation_propagates(
    mcp_managers: Callable[..., Any],
) -> None:
    manager, _registry = await mcp_managers({"calc": stdio_config(timeout_s=30.0)})
    await manager.start()
    token = CancelToken()
    task = asyncio.create_task(manager.call_tool("calc", "slow", {"seconds": 10}, cancel=token))
    await asyncio.sleep(0.5)
    token.cancel()
    with pytest.raises(RunCancelled):
        await asyncio.wait_for(task, timeout=5)

    echo = await manager.call_tool("calc", "echo", {"text": "after cancel"})
    assert _content_text(echo) == "after cancel"


async def test_stop_unregisters_tools(
    mcp_managers: Callable[..., Any],
) -> None:
    manager, registry = await mcp_managers({"calc": stdio_config()})
    await manager.start()
    assert registry.names()

    await manager.stop()
    assert registry.names() == []
    status = manager.status()["calc"]
    assert status.state == "stopped"
    assert status.tools == ()
    with pytest.raises(MCPUnavailable, match="not connected"):
        await manager.call_tool("calc", "echo", {"text": "gone"})


async def test_reconnect_rediscovers_tools(
    mcp_managers: Callable[..., Any],
) -> None:
    manager, registry = await mcp_managers({"calc": stdio_config()})
    await manager.start()
    first = await manager.call_tool("calc", "echo", {"text": "before"})
    assert _content_text(first) == "before"

    status = await manager.reconnect("calc")
    assert status.state == "ready"
    assert status.reconnect_count == 1
    assert "mcp__calc__echo" in registry.names()

    second = await manager.call_tool("calc", "echo", {"text": "after"})
    assert _content_text(second) == "after"


async def test_start_after_reconnect_does_not_duplicate_connection(
    mcp_managers: Callable[..., Any],
) -> None:
    manager, registry = await mcp_managers({"calc": stdio_config()})
    await manager.start()
    await manager.stop()

    status = await manager.reconnect("calc")
    assert status.state == "ready"
    # A later start() must skip the already-connected server instead of
    # spawning a second process whose tools would collide with its own.
    statuses = await manager.start()
    assert statuses["calc"].state == "ready"
    assert len(registry.names()) == 4
    echo = await manager.call_tool("calc", "echo", {"text": "single"})
    assert _content_text(echo) == "single"


async def test_two_servers_share_no_namespace(
    mcp_managers: Callable[..., Any],
) -> None:
    manager, registry = await mcp_managers({"alpha": stdio_config(), "beta": stdio_config()})
    statuses = await manager.start()
    assert statuses["alpha"].state == "ready"
    assert statuses["beta"].state == "ready"
    assert "mcp__alpha__echo" in registry.names()
    assert "mcp__beta__echo" in registry.names()
    assert len(registry.names()) == 8

    alpha = await manager.call_tool("alpha", "echo", {"text": "from alpha"})
    beta = await manager.call_tool("beta", "echo", {"text": "from beta"})
    assert _content_text(alpha) == "from alpha"
    assert _content_text(beta) == "from beta"


async def test_tool_name_collision_is_all_or_nothing(
    mcp_managers: Callable[..., Any],
) -> None:
    registry = ToolRegistry()
    registry.register(DummyTool())
    manager, _registry = await mcp_managers({"calc": stdio_config()}, registry=registry)

    with pytest.raises(MCPUnavailable, match="collision"):
        await manager.start()
    # Nothing from the server may be registered when one tool collides.
    assert registry.names() == ["mcp__calc__add"]
    status = manager.status()["calc"]
    assert status.state == "error"
    assert status.last_error and "collision" in status.last_error


async def test_optional_collision_only_marks_server_error(
    mcp_managers: Callable[..., Any],
) -> None:
    registry = ToolRegistry()
    registry.register(DummyTool())
    manager, _registry = await mcp_managers(
        {"calc": stdio_config(required=False)}, registry=registry
    )
    statuses = await manager.start()
    assert statuses["calc"].state == "error"
    assert statuses["calc"].last_error
    assert registry.names() == ["mcp__calc__add"]


async def test_allowed_and_denied_tool_filters(
    mcp_managers: Callable[..., Any],
) -> None:
    manager, registry = await mcp_managers(
        {"calc": stdio_config(allowed_tools=["echo", "add"], denied_tools=["add"])}
    )
    await manager.start()
    assert registry.names() == ["mcp__calc__echo"]
    assert manager.status()["calc"].tools == ("mcp__calc__echo",)
    with pytest.raises(MCPToolError, match="not available"):
        await manager.call_tool("calc", "add", {"a": 1, "b": 2})


async def test_optional_start_failure_does_not_block_required_server(
    mcp_managers: Callable[..., Any],
) -> None:
    manager, registry = await mcp_managers(
        {
            "calc": stdio_config(),
            "broken": stdio_config(
                command="definitely-not-a-real-mcp-command-xyz",
                required=False,
                startup_timeout_s=5,
            ),
        }
    )
    statuses = await manager.start()
    assert statuses["calc"].state == "ready"
    assert statuses["broken"].state == "error"
    assert "mcp__calc__echo" in registry.names()
    echo = await manager.call_tool("calc", "echo", {"text": "ok"})
    assert _content_text(echo) == "ok"


async def test_disabled_server_is_not_started(
    mcp_managers: Callable[..., Any],
) -> None:
    manager, registry = await mcp_managers(
        {"off": stdio_config(enabled=False, trusted=False, command="python-does-not-matter")}
    )
    statuses = await manager.start()
    assert statuses["off"].state == "disabled"
    assert registry.names() == []


# ------------------------------------------------- executor integration


def _executor(container: Any, registry: ToolRegistry) -> ToolExecutor:
    return ToolExecutor(
        registry=registry,
        sessions=container.sessions,
        policy=container.policy,
        approvals=container.approvals,
        recorder=container.recorder,
        path_guard=container.path_guard,
        audit=container.audit,
        redactor=container.redactor,
        claims=container.claims,
    )


async def test_tool_executor_approval_validation_and_plan_denial(
    workspace: Path,
    settings: Settings,
    mcp_managers: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, registry = await mcp_managers({"calc": stdio_config()})
    await manager.start()

    calls: list[tuple[Any, ...]] = []
    original_call = manager.call_tool

    async def spy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(args)
        return await original_call(*args, **kwargs)

    monkeypatch.setattr(manager, "call_tool", spy)

    container, _provider = make_container(settings, workspace, [])
    try:
        executor = _executor(container, registry)
        session = await container.sessions.create(
            cwd=workspace,
            model=settings.agent.default_model,
            provider=settings.agent.default_provider,
        )

        async def execute(call_id: str, arguments: dict[str, Any]) -> ToolResult:
            return await executor.execute(
                ToolCall(id=call_id, name="mcp__calc__echo", arguments=arguments),
                session_id=session.id,
                project_id=None,
                lease_owner="test-owner",
                run_id=None,
                turn_id=None,
                cancel=CancelToken(),
            )

        # Invalid arguments are stopped by the adapter: the server is never called.
        invalid = await execute("call-invalid", {"text": 123})
        assert invalid.is_error
        assert invalid.error_kind == "invalid_arguments"
        assert calls == []

        # AUTO requests approval for EXEC and AutoApprover grants it.
        valid = await execute("call-valid", {"text": "through executor"})
        assert not valid.is_error
        assert "through executor" in valid.content
        assert len(calls) == 1

        # PLAN mode denies the exec effect before preflight/approval/run.
        container.policy.set_mode(PermissionMode.PLAN)
        denied = await execute("call-plan", {"text": "blocked"})
        assert denied.is_error
        assert denied.error_kind == "policy_denied"
        assert len(calls) == 1
    finally:
        await container.close()


# ------------------------------------------------------------ HTTP


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


async def _wait_for_port(port: int, limit_s: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_s
    while loop.time() < deadline:
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.1)
    raise AssertionError(f"HTTP MCP server did not listen on port {port}")


async def test_streamable_http_real_server(workspace: Path) -> None:
    server = MCPServer(name="bear-http-test", log_level="CRITICAL")

    @server.tool(name="echo", description="Echo text")
    async def echo(text: str) -> str:
        return text

    port = _free_port()
    server_task = asyncio.create_task(server.run_streamable_http_async(host="127.0.0.1", port=port))
    registry = ToolRegistry()
    manager = MCPManager(
        MCPConfig(
            servers={
                "remote": MCPServerConfig(
                    transport="streamable_http",
                    url=f"http://127.0.0.1:{port}/mcp",
                    trusted=True,
                    required=True,
                    timeout_s=10.0,
                    startup_timeout_s=20.0,
                    allow_insecure_http=True,
                )
            }
        ),
        registry,
        workspace=workspace,
        redactor=SecretRedactor(),
    )
    try:
        await _wait_for_port(port)
        statuses = await manager.start()
        assert statuses["remote"].state == "ready"
        assert statuses["remote"].transport == "streamable_http"
        assert registry.names() == ["mcp__remote__echo"]

        payload = await manager.call_tool("remote", "echo", {"text": "http-hi"})
        assert payload["is_error"] is False
        assert _content_text(payload) == "http-hi"
    finally:
        await manager.stop()
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(server_task, timeout=10)
