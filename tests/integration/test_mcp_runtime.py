"""Real MCP stdio server through Container.startup and AgentRuntime."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from tests.support import make_container, text_script, tool_script

from bear_agent.config.models import MCPServerConfig
from bear_agent.core.events import EventType
from bear_agent.runtime.stop_policy import RunStatus

SERVER_SCRIPT = Path(__file__).resolve().parents[1] / "support" / "mcp_server.py"


@pytest.mark.asyncio
async def test_runtime_executes_discovered_mcp_tool(workspace: Path, settings) -> None:
    settings.mcp.servers = {
        "calc": MCPServerConfig(
            transport="stdio",
            command=sys.executable,
            args=[str(SERVER_SCRIPT)],
            trusted=True,
            required=True,
            timeout_s=20,
            startup_timeout_s=20,
        )
    }
    container, _provider = make_container(
        settings,
        workspace,
        [
            tool_script("mcp-call", "mcp__calc__echo", {"text": "hello mcp"}),
            text_script("mcp done"),
        ],
    )
    try:
        await container.startup()
        assert container.mcp is not None
        assert container.registry.has("mcp__calc__echo")
        assert "echo" in container.registry.get("mcp__calc__echo").description

        session = await container.sessions.create(cwd=workspace, model="m", provider="p")
        result = await container.runtime.run(session.id, "use the MCP tool")
        assert result.status == RunStatus.COMPLETED
        messages = await container.sessions.load_messages(session.id)
        tools = [message for message in messages if message.role == "tool"]
        assert len(tools) == 1
        assert "hello mcp" in tools[0].tool_result().content
        actions = [entry.action for entry in await container.audit.tail(50)]
        assert "tool.started" in actions
        assert "tool.finished" in actions
        types = [event.type for event in await container.sessions.events_for(session.id)]
        assert EventType.TOOL_CALL_COMPLETED in types
        statuses = container.mcp.status()
        assert statuses["calc"].state == "ready"
        assert "mcp__calc__echo" in statuses["calc"].tools
    finally:
        await container.close()
    # close stops the subprocess and unregisters the tool.
    assert container.mcp is not None
    assert not container.registry.has("mcp__calc__echo")
