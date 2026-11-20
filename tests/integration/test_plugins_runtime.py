"""Plugin loading through Container.startup with real tools/MCP."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import BaseModel, Field
from tests.support import make_container, text_script, tool_script

from bear_agent.core.modes import ToolSideEffect
from bear_agent.runtime.stop_policy import RunStatus
from bear_agent.tools.base import Tool, ToolContext, ToolResult

SERVER_SCRIPT = Path(__file__).resolve().parents[1] / "support" / "mcp_server.py"


def _write_manifest(plugin_dir: Path, body: str) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.toml").write_text(body, encoding="utf-8")


@pytest.mark.asyncio
async def test_manifest_plugin_contributes_mcp_server(workspace: Path, settings, tmp_path) -> None:
    plugin_dir = tmp_path / "plugins" / "demo"
    _write_manifest(
        plugin_dir,
        f"""
schema_version = 1
id = "demo"
name = "Demo MCP plugin"
version = "1.0.0"
requires_bear = ">=0.4.0,<0.7.0"
enabled_by_default = true

[permissions]
mcp = true

[mcp_servers.calc]
transport = "stdio"
command = "{sys.executable.replace(chr(92), "/")}"
args = ["{str(SERVER_SCRIPT).replace(chr(92), "/")}"]
trusted = true
required = true
timeout_s = 20
startup_timeout_s = 20
""",
    )
    settings.plugins.dirs = [tmp_path / "plugins"]
    container, _provider = make_container(
        settings,
        workspace,
        [
            tool_script("mcp-call", "mcp__demo_calc__echo", {"text": "plugin mcp"}),
            text_script("plugin done"),
        ],
    )
    try:
        await container.startup()
        assert container.plugin_report is not None
        assert "demo" in container.plugin_report.loaded
        assert container.registry.has("mcp__demo_calc__echo")
        session = await container.sessions.create(cwd=workspace, model="m", provider="p")
        result = await container.runtime.run(session.id, "use plugin tool")
        assert result.status == RunStatus.COMPLETED
        messages = await container.sessions.load_messages(session.id)
        tool_message = next(message for message in messages if message.role == "tool")
        assert "plugin mcp" in tool_message.tool_result().content
    finally:
        await container.close()


class _EchoArgs(BaseModel):
    text: str = Field(description="Text to echo")


class _PluginEchoTool(Tool):
    name = "plugin_echo"
    description = "Echo from a Python plugin"
    args_model = _EchoArgs
    side_effect = ToolSideEffect.READ
    idempotent = True

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, _EchoArgs)
        return ToolResult(content=f"plugin:{args.text}")


@pytest.mark.asyncio
async def test_python_plugin_registers_tool_only_when_allowed(
    workspace: Path, settings, tmp_path
) -> None:
    plugin_dir = tmp_path / "plugins" / "py_echo"
    _write_manifest(
        plugin_dir,
        """
schema_version = 1
id = "py_echo"
name = "Python echo"
version = "1.0.0"
requires_bear = ">=0.4.0,<0.7.0"
enabled_by_default = true
entrypoint = "plugin.py"

[permissions]
python = true
""",
    )
    (plugin_dir / "plugin.py").write_text(
        """
from pydantic import BaseModel, Field
from bear_agent.tools.base import Tool, ToolResult

class Args(BaseModel):
    text: str = Field(description="text")

class Echo(Tool):
    name = "plugin_echo"
    description = "echo"
    args_model = Args
    async def run(self, args, ctx):
        return ToolResult(content="plugin:" + args.text)

def register(ctx):
    ctx.add_tool(Echo())
""",
        encoding="utf-8",
    )
    settings.plugins.dirs = [tmp_path / "plugins"]
    settings.plugins.allow_python = True
    container, _provider = make_container(
        settings,
        workspace,
        [
            tool_script("plugin-call", "plugin_echo", {"text": "python"}),
            text_script("python plugin done"),
        ],
    )
    try:
        await container.startup()
        assert container.registry.has("plugin_echo")
        session = await container.sessions.create(cwd=workspace, model="m", provider="p")
        result = await container.runtime.run(session.id, "use plugin")
        assert result.status == RunStatus.COMPLETED
        messages = await container.sessions.load_messages(session.id)
        tool_message = next(message for message in messages if message.role == "tool")
        assert tool_message.tool_result().content == "plugin:python"
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_python_plugin_is_disabled_without_explicit_opt_in(
    workspace: Path, settings, tmp_path
) -> None:
    plugin_dir = tmp_path / "plugins" / "blocked"
    _write_manifest(
        plugin_dir,
        """
schema_version = 1
id = "blocked"
name = "Blocked"
version = "1.0.0"
enabled_by_default = true
entrypoint = "plugin.py"

[permissions]
python = true
""",
    )
    (plugin_dir / "plugin.py").write_text(
        "def register(ctx):\n    raise RuntimeError('must not load')\n",
        encoding="utf-8",
    )
    settings.plugins.dirs = [tmp_path / "plugins"]
    settings.plugins.allow_python = False
    container, _provider = make_container(settings, workspace, [text_script("ok")])
    try:
        await container.startup()
        assert container.plugin_report is not None
        assert "blocked" in container.plugin_report.failed
        assert not container.registry.has("plugin_echo")
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_python_plugin_registers_in_process_hook(workspace: Path, settings, tmp_path) -> None:
    plugin_dir = tmp_path / "plugins" / "hook_plugin"
    _write_manifest(
        plugin_dir,
        """
schema_version = 1
id = "hook_plugin"
name = "Hook plugin"
version = "1.0.0"
enabled_by_default = true
entrypoint = "plugin.py"

[permissions]
python = true
hooks = true
""",
    )
    (plugin_dir / "plugin.py").write_text(
        """
async def add_context(invocation):
    from bear_agent.hooks.models import HookDecision
    return HookDecision(
        action="continue",
        reason="plugin context",
        context_append="PLUGIN-HOOK-CONTEXT",
    )

def register(ctx):
    ctx.add_hook(
        add_context,
        ["model_before"],
        can_append_context=True,
        fail_mode="closed",
    )
""",
        encoding="utf-8",
    )
    settings.plugins.dirs = [tmp_path / "plugins"]
    settings.plugins.allow_python = True
    container, provider = make_container(settings, workspace, [text_script("ok")])
    try:
        await container.startup()
        session = await container.sessions.create(cwd=workspace, model="m", provider="p")
        result = await container.runtime.run(session.id, "hello")
        assert result.status == RunStatus.COMPLETED
        rendered = "\n".join(
            message.text_content()
            for message in provider.requests[0].messages
            if message.role == "system"
        )
        assert "PLUGIN-HOOK-CONTEXT" in rendered
    finally:
        await container.close()
