"""Plugin framework: manifests, discovery, state, dependencies and Python load."""

from __future__ import annotations

import inspect
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from bear_agent.config.models import HookCommandConfig, MCPServerConfig, PluginsConfig
from bear_agent.core.errors import PluginLoadError, PluginManifestError
from bear_agent.plugins import (
    PluginContext,
    PluginManager,
    PluginStatus,
    hook_source,
    load_manifest,
    namespaced_hook_command_id,
    namespaced_mcp_server_name,
)
from bear_agent.tools.base import Tool, ToolContext, ToolResult
from bear_agent.tools.registry import ToolRegistry

# --------------------------------------------------------------------- helpers


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return json.dumps(str(value))


def plugin_toml(
    plugin_id: str,
    *,
    name: str | None = None,
    version: str = "1.0.0",
    schema_version: int = 1,
    requires_bear: str | None = None,
    dependencies: list[str] | None = None,
    permissions: list[str] | None = None,
    hooks: list[dict[str, Any]] | None = None,
    mcp_servers: dict[str, dict[str, Any]] | None = None,
    enabled_by_default: bool = True,
    entrypoint: str | None = None,
) -> str:
    lines = [
        f"schema_version = {schema_version}",
        f"id = {_toml_value(plugin_id)}",
        f"name = {_toml_value(name or plugin_id)}",
        f"version = {_toml_value(version)}",
        f"enabled_by_default = {'true' if enabled_by_default else 'false'}",
    ]
    if requires_bear is not None:
        lines.append(f"requires_bear = {_toml_value(requires_bear)}")
    if dependencies:
        lines.append(f"dependencies = {_toml_value(dependencies)}")
    if entrypoint is not None:
        lines.append(f"entrypoint = {_toml_value(entrypoint)}")
    if permissions:
        lines.append("")
        lines.append("[permissions]")
        for permission in permissions:
            lines.append(f"{permission} = true")
    for hook in hooks or []:
        lines.append("")
        lines.append("[[hooks]]")
        for key, value in hook.items():
            lines.append(f"{key} = {_toml_value(value)}")
    for server_name, server in (mcp_servers or {}).items():
        lines.append("")
        server_key = (
            server_name
            if all(char.isalnum() or char in "_-" for char in server_name)
            else json.dumps(server_name)
        )
        lines.append(f"[mcp_servers.{server_key}]")
        for key, value in server.items():
            lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


def write_plugin(
    root: Path,
    plugin_id: str,
    *,
    directory: str | None = None,
    python_source: str | None = None,
    manifest_text: str | None = None,
    **manifest_kwargs: Any,
) -> Path:
    plugin_dir = root / (directory or plugin_id)
    plugin_dir.mkdir(parents=True, exist_ok=True)
    if manifest_text is None:
        manifest_text = plugin_toml(plugin_id, **manifest_kwargs)
    (plugin_dir / "plugin.toml").write_text(manifest_text, encoding="utf-8")
    if python_source is not None:
        (plugin_dir / "plugin.py").write_text(python_source, encoding="utf-8")
    return plugin_dir


def make_manager(
    tmp_path: Path,
    *dirs: Path,
    allow_python: bool = False,
    hooks_allow_external: bool = False,
    hooks_enabled: bool = True,
    bear_version: str | None = None,
    max_plugins: int = 50,
) -> PluginManager:
    config = PluginsConfig(
        dirs=list(dirs),
        allow_python=allow_python,
        max_plugins=max_plugins,
    )
    return PluginManager(
        config,
        data_dir=tmp_path / "data",
        bear_version=bear_version,
        hooks_allow_external=hooks_allow_external,
        hooks_enabled=hooks_enabled,
    )


@dataclass
class FakeRegistration:
    handler: Any
    events: list[Any]
    priority: int = 0
    timeout_s: float | None = None
    fail_mode: str = "open"
    can_deny: bool = False
    can_append_context: bool = False
    source: str = ""


class FakeHookManager:
    def __init__(self) -> None:
        self.registrations: list[FakeRegistration] = []
        self.commands: list[HookCommandConfig] = []
        self.unregistered: list[str] = []
        self.unregistered_hooks: list[str] = []

    def register(self, registration: FakeRegistration) -> None:
        self.registrations.append(registration)

    def register_command(self, command: HookCommandConfig) -> None:
        self.commands.append(command)

    def unregister(self, hook_id: str) -> bool:
        self.unregistered_hooks.append(hook_id)
        if hook_id.startswith("cmd:"):
            command_id = hook_id[len("cmd:") :]
            self.commands = [command for command in self.commands if command.id != command_id]
            return True
        return False

    def unregister_source(self, source: str) -> None:
        self.unregistered.append(source)
        self.registrations = [
            registration for registration in self.registrations if registration.source != source
        ]
        prefix = f"{source}:"
        self.commands = [command for command in self.commands if not command.id.startswith(prefix)]

    async def fire(self, event: str, payload: Any = None) -> list[Any]:
        results: list[Any] = []
        for registration in list(self.registrations):
            if event not in registration.events:
                continue
            value = registration.handler(event, payload)
            if inspect.isawaitable(value):
                value = await value
            results.append(value)
        return results


class FakeMCPManager:
    def __init__(self) -> None:
        self.servers: dict[str, MCPServerConfig] = {}

    def register_server(self, name: str, config: MCPServerConfig) -> None:
        self.servers[name] = config

    def remove_server(self, name: str) -> None:
        self.servers.pop(name, None)


class _EmptyArgs(BaseModel):
    pass


class ExistingTool(Tool):
    name = "existing"
    description = "a pre-existing tool"
    args_model = _EmptyArgs

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(content="existing")


@dataclass
class Harness:
    manager: PluginManager
    context: PluginContext
    hooks: FakeHookManager
    mcp: FakeMCPManager
    registry: ToolRegistry


def harness(
    tmp_path: Path,
    *dirs: Path,
    allow_python: bool = False,
    hooks_allow_external: bool = False,
    registry: ToolRegistry | None = None,
    bear_version: str | None = None,
    hooks_enabled: bool = True,
    max_plugins: int = 50,
    **context_kwargs: Any,
) -> Harness:
    hooks = FakeHookManager()
    mcp = FakeMCPManager()
    tools = registry or ToolRegistry()
    context = PluginContext(
        workspace=tmp_path,
        register_hook=hooks.register,
        register_hook_command=hooks.register_command,
        unregister_hook_source=hooks.unregister_source,
        unregister_hook=hooks.unregister,
        register_tool=tools.register,
        unregister_tool=tools.unregister,
        tool_registry=tools,
        register_mcp_server=mcp.register_server,
        unregister_mcp_server=mcp.remove_server,
        hook_registration_factory=FakeRegistration,
        **context_kwargs,
    )
    manager = make_manager(
        tmp_path,
        *dirs,
        allow_python=allow_python,
        hooks_allow_external=hooks_allow_external,
        hooks_enabled=hooks_enabled,
        bear_version=bear_version,
        max_plugins=max_plugins,
    )
    return Harness(manager=manager, context=context, hooks=hooks, mcp=mcp, registry=tools)


# -------------------------------------------------------------------- manifests


def test_manifest_valid_and_helpers(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    plugin = write_plugin(
        root,
        "demo",
        version="1.2.3",
        requires_bear=">=0.4.0,<0.8.0",
        dependencies=["base>=1.0"],
        permissions=["hooks"],
        hooks=[
            {
                "id": "lint",
                "event": "before_tool_call",
                "command": "python",
                "args": ["hook.py"],
                "fail_mode": "closed",
            }
        ],
    )

    manifest = load_manifest(plugin)
    assert manifest.id == "demo"
    assert manifest.name == "demo"
    assert manifest.version == "1.2.3"
    assert manifest.enabled_by_default is True
    assert manifest.permissions.hooks is True
    assert manifest.permissions.mcp is False
    assert manifest.permissions.python is False
    assert manifest.bear_specifier.contains("0.4.0")
    assert "base" in manifest.dependency_specifiers
    assert manifest.namespaced_hook_id("lint") == "plugin:demo:lint"
    assert manifest.hooks[0].fail_mode == "closed"

    # The manifest file itself is accepted by load_manifest.
    assert load_manifest(plugin / "plugin.toml").id == "demo"

    assert hook_source("demo") == "plugin:demo"
    assert namespaced_hook_command_id("demo", "lint") == "plugin:demo:lint"
    assert namespaced_mcp_server_name("demo", "docs") == "demo_docs"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "Bad Id"),
        ("version", "not-a-version"),
        ("schema_version", 2),
        ("requires_bear", ">="),
    ],
)
def test_manifest_rejects_invalid_scalar_fields(tmp_path: Path, field: str, value: Any) -> None:
    root = tmp_path / "plugins"
    plugin = root / "demo"
    plugin.mkdir(parents=True)
    manifest_text = plugin_toml("demo")
    if field == "id":
        manifest_text = manifest_text.replace('id = "demo"', f"id = {_toml_value(value)}")
    elif field == "version":
        manifest_text = manifest_text.replace(
            'version = "1.0.0"', f"version = {_toml_value(value)}"
        )
    elif field == "schema_version":
        manifest_text = manifest_text.replace("schema_version = 1", f"schema_version = {value}")
    else:
        manifest_text += f"requires_bear = {_toml_value(value)}\n"
    (plugin / "plugin.toml").write_text(manifest_text, encoding="utf-8")

    with pytest.raises(PluginManifestError) as caught:
        load_manifest(plugin)
    message = str(caught.value)
    assert "plugin.toml" in message
    assert str(plugin) in message


def test_manifest_missing_and_invalid_toml_are_actionable(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(PluginManifestError, match="plugin manifest not found"):
        load_manifest(missing)

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "plugin.toml").write_text("this is not = = toml", encoding="utf-8")
    with pytest.raises(PluginManifestError) as caught:
        load_manifest(broken)
    assert str(broken / "plugin.toml") in str(caught.value)


def test_manifest_requires_permission_for_hooks_and_mcp(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    hooks_without_permission = write_plugin(
        root,
        "hooks",
        permissions=[],
        hooks=[{"id": "lint", "event": "before_tool_call", "command": "python"}],
    )
    with pytest.raises(PluginManifestError, match=r"permissions\.hooks"):
        load_manifest(hooks_without_permission)

    mcp_without_permission = write_plugin(
        root,
        "mcp",
        permissions=[],
        mcp_servers={"docs": {"transport": "stdio", "command": "python", "trusted": True}},
    )
    with pytest.raises(PluginManifestError, match=r"permissions\.mcp"):
        load_manifest(mcp_without_permission)

    entrypoint_without_python = write_plugin(root, "entry", permissions=[], entrypoint="custom.py")
    with pytest.raises(PluginManifestError, match=r"permissions\.python"):
        load_manifest(entrypoint_without_python)


def test_manifest_python_entrypoint_must_exist_inside_plugin(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    missing = write_plugin(root, "missing", permissions=["python"])
    with pytest.raises(PluginManifestError, match="does not exist"):
        load_manifest(missing)

    escape = root / "escape"
    escape.mkdir()
    (escape / "plugin.toml").write_text(
        plugin_toml("escape", permissions=["python"], entrypoint="../outside.py"),
        encoding="utf-8",
    )
    with pytest.raises(PluginManifestError, match="entrypoint"):
        load_manifest(escape)


def test_manifest_rejects_untrusted_mcp_and_bad_names(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    untrusted = write_plugin(
        root,
        "untrusted",
        permissions=["mcp"],
        mcp_servers={"docs": {"transport": "stdio", "command": "python", "trusted": False}},
    )
    with pytest.raises(PluginManifestError, match="not trusted"):
        load_manifest(untrusted)

    bad_name = write_plugin(
        root,
        "badname",
        permissions=["mcp"],
        mcp_servers={"bad name": {"transport": "stdio", "command": "python", "trusted": True}},
    )
    with pytest.raises(PluginManifestError, match="invalid MCP server name"):
        load_manifest(bad_name)


def test_manifest_dependency_syntax_and_duplicates(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    invalid = write_plugin(root, "invalid", dependencies=["other[extra]"])
    with pytest.raises(PluginManifestError, match="invalid dependency"):
        load_manifest(invalid)

    duplicate = write_plugin(root, "duplicate", dependencies=["other>=1.0", "other<2.0"])
    with pytest.raises(PluginManifestError, match="duplicate dependency"):
        load_manifest(duplicate)

    bad_specifier = write_plugin(root, "badspec", dependencies=["other>="])
    with pytest.raises(PluginManifestError, match="invalid version specifier"):
        load_manifest(bad_specifier)

    self_dependency = write_plugin(root, "selfdep", dependencies=["selfdep>=1.0"])
    with pytest.raises(PluginManifestError, match="depend on itself"):
        load_manifest(self_dependency)


def test_context_from_managers_wires_agreed_hook_api(tmp_path: Path) -> None:
    hooks = FakeHookManager()
    registry = ToolRegistry()
    context = PluginContext.from_managers(
        workspace=tmp_path, hook_manager=hooks, tool_registry=registry
    )
    assert context.register_hook == hooks.register
    assert context.unregister_hook_source == hooks.unregister_source
    assert context.register_hook_command == hooks.register_command
    assert context.unregister_hook == hooks.unregister
    assert context.register_tool == registry.register
    assert context.unregister_tool == registry.unregister
    assert context.tool_registry is registry


# ------------------------------------------------------ discovery and state


@pytest.mark.asyncio
async def test_discovery_dedupes_dirs_and_scans_data_dir(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    write_plugin(root, "alpha")
    data_plugins = tmp_path / "data" / "plugins"
    data_plugins.mkdir(parents=True)
    write_plugin(data_plugins, "beta")

    test_harness = harness(tmp_path, root, root)
    report = await test_harness.manager.load(test_harness.context)

    assert report.loaded == ["alpha", "beta"]
    assert report.failed == {}
    assert report.discovery_errors == []
    assert test_harness.manager.info("alpha") is not None


@pytest.mark.asyncio
async def test_duplicate_plugin_ids_across_directories_are_conflict(
    tmp_path: Path,
) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    write_plugin(root_a, "same", directory="one")
    write_plugin(root_b, "same", directory="two")

    test_harness = harness(tmp_path, root_a, root_b)
    report = await test_harness.manager.load(test_harness.context)

    assert report.loaded == []
    assert report.failed == {}
    assert any("duplicate plugin id" in conflict for conflict in report.conflicts)
    assert any("duplicate plugin id" in error for error in report.discovery_errors)
    assert test_harness.manager.info("same") is None


@pytest.mark.asyncio
async def test_enable_disable_state_persists_and_write_is_atomic(
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    write_plugin(root, "demo", enabled_by_default=False)

    first = make_manager(tmp_path, root)
    info = first.info("demo")
    assert info is not None and info.enabled is False
    assert first.enable("demo") is True

    state_path = tmp_path / "data" / "plugins" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state == {"demo": {"enabled": True}}
    assert list(state_path.parent.glob(".state-*")) == []

    second = make_manager(tmp_path, root)
    second_info = second.info("demo")
    assert second_info is not None and second_info.enabled is True

    assert await second.disable("demo") is True
    third = make_manager(tmp_path, root)
    third_info = third.info("demo")
    assert third_info is not None
    assert third_info.enabled is False
    assert third_info.status is PluginStatus.DISABLED

    with pytest.raises(PluginLoadError, match="unknown plugin"):
        first.enable("absent")
    with pytest.raises(PluginLoadError, match="unknown plugin"):
        await first.disable("absent")


@pytest.mark.asyncio
async def test_globally_disabled_plugins_do_not_load(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    write_plugin(root, "demo")
    manager = PluginManager(PluginsConfig(dirs=[root], enabled=False), data_dir=tmp_path / "data")
    test_harness = harness(tmp_path, root)
    report = await manager.load(test_harness.context)

    assert report.disabled is True
    assert report.loaded == []
    assert manager.info("demo") is not None


# --------------------------------------------------------------- dependencies


@pytest.mark.asyncio
async def test_dependency_order_is_topological(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    write_plugin(root, "base")
    write_plugin(root, "mid", dependencies=["base>=1.0"])
    write_plugin(root, "top", dependencies=["mid>=1.0"])

    test_harness = harness(tmp_path, root)
    report = await test_harness.manager.load(test_harness.context)

    assert report.loaded == ["base", "mid", "top"]
    assert report.order == ["base", "mid", "top"]


@pytest.mark.asyncio
async def test_missing_dependency_fails_only_dependent(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    write_plugin(root, "dependent", dependencies=["absent>=1.0"])
    write_plugin(root, "solo")

    test_harness = harness(tmp_path, root)
    report = await test_harness.manager.load(test_harness.context)

    assert report.loaded == ["solo"]
    assert "dependent" in report.failed
    assert "missing dependency" in report.failed["dependent"]


@pytest.mark.asyncio
async def test_dependency_cycle_and_version_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    write_plugin(root, "x", dependencies=["y"])
    write_plugin(root, "y", dependencies=["x"])
    write_plugin(root, "old", version="1.0.0")
    write_plugin(root, "needs_new", dependencies=["old>=2.0"])
    write_plugin(root, "solo")

    test_harness = harness(tmp_path, root)
    report = await test_harness.manager.load(test_harness.context)

    assert report.loaded == ["old", "solo"]
    assert "cycle" in report.failed["x"]
    assert "cycle" in report.failed["y"]
    assert "requires" in report.failed["needs_new"]


@pytest.mark.asyncio
async def test_dependency_on_disabled_plugin_fails(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    write_plugin(root, "base", enabled_by_default=False)
    write_plugin(root, "dependent", dependencies=["base"])

    test_harness = harness(tmp_path, root)
    report = await test_harness.manager.load(test_harness.context)

    assert report.skipped == ["base"]
    assert "disabled" in report.failed["dependent"]


@pytest.mark.asyncio
async def test_requires_bear_incompatibility_marks_failed(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    write_plugin(root, "future", requires_bear=">=9.0,<10.0")

    test_harness = harness(tmp_path, root, bear_version="0.4.0")
    report = await test_harness.manager.load(test_harness.context)

    assert report.loaded == []
    assert "requires Bear-Agent" in report.failed["future"]


# -------------------------------------------------------------- contributions


@pytest.mark.asyncio
async def test_hook_commands_require_external_opt_in_and_are_namespaced(
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    write_plugin(
        root,
        "demo",
        permissions=["hooks"],
        hooks=[
            {
                "id": "lint",
                "event": "before_tool_call",
                "command": "python",
                "args": ["hook.py"],
                "fail_mode": "closed",
            }
        ],
    )

    blocked = harness(tmp_path, root)
    blocked_report = await blocked.manager.load(blocked.context)
    assert "demo" in blocked_report.failed
    assert "allow_external" in blocked_report.failed["demo"]
    assert blocked.hooks.commands == []

    allowed = harness(tmp_path, root, hooks_allow_external=True)
    report = await allowed.manager.load(allowed.context)
    assert report.loaded == ["demo"]
    assert allowed.hooks.commands[0].id == "plugin:demo:lint"
    assert allowed.hooks.commands[0].fail_mode == "closed"
    contributions = report.contributions["demo"]
    assert contributions.hook_commands[0].id == "plugin:demo:lint"
    assert contributions.source == "plugin:demo"

    assert await allowed.manager.unload("demo") is True
    assert allowed.hooks.commands == []
    assert "cmd:plugin:demo:lint" in allowed.hooks.unregistered_hooks


@pytest.mark.asyncio
async def test_reserved_hook_id_conflict_applies_no_contributions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    write_plugin(
        root,
        "demo",
        permissions=["hooks"],
        hooks=[{"id": "lint", "event": "before_tool_call", "command": "python"}],
    )

    test_harness = harness(
        tmp_path,
        root,
        hooks_allow_external=True,
        reserved_hook_ids=frozenset({"plugin:demo:lint"}),
    )
    report = await test_harness.manager.load(test_harness.context)

    assert report.loaded == []
    assert any("hook id conflict" in conflict for conflict in report.conflicts)
    assert test_harness.hooks.commands == []


@pytest.mark.asyncio
async def test_mcp_servers_are_namespaced_and_applied(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    write_plugin(
        root,
        "demo",
        permissions=["mcp"],
        mcp_servers={
            "docs": {
                "transport": "stdio",
                "command": "python",
                "args": ["-m", "docs"],
                "trusted": True,
            }
        },
    )

    test_harness = harness(tmp_path, root)
    report = await test_harness.manager.load(test_harness.context)

    assert report.loaded == ["demo"]
    assert set(test_harness.mcp.servers) == {"demo_docs"}
    contributions = report.contributions["demo"]
    assert set(contributions.mcp_servers) == {"demo_docs"}
    assert test_harness.manager.info("demo").mcp_servers == ["demo_docs"]  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_namespaced_mcp_name_collision_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    server = {"transport": "stdio", "command": "python", "trusted": True}
    write_plugin(root, "a", permissions=["mcp"], mcp_servers={"b_c": dict(server)})
    write_plugin(root, "a_b", permissions=["mcp"], mcp_servers={"c": dict(server)})

    test_harness = harness(tmp_path, root)
    report = await test_harness.manager.load(test_harness.context)

    assert report.loaded == ["a"]
    assert "a_b" in report.failed
    assert any("a_b_c" in conflict for conflict in report.conflicts)
    assert set(test_harness.mcp.servers) == {"a_b_c"}


@pytest.mark.asyncio
async def test_reserved_mcp_name_conflict_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    write_plugin(
        root,
        "demo",
        permissions=["mcp"],
        mcp_servers={"docs": {"transport": "stdio", "command": "python", "trusted": True}},
    )
    test_harness = harness(tmp_path, root, reserved_mcp_servers=frozenset({"demo_docs"}))
    report = await test_harness.manager.load(test_harness.context)

    assert report.loaded == []
    assert any("MCP server name conflict" in conflict for conflict in report.conflicts)
    assert test_harness.mcp.servers == {}


# ------------------------------------------------------------- Python plugins


PYTHON_PLUGIN_SOURCE = """
from pydantic import BaseModel

from bear_agent.config.models import MCPServerConfig
from bear_agent.tools.base import Tool, ToolResult


class EchoArgs(BaseModel):
    text: str = ""


class EchoTool(Tool):
    name = "plugin_echo"
    description = "echo text"
    args_model = EchoArgs

    async def run(self, args, ctx):
        return ToolResult(content=args.text)


def handler(event, payload=None):
    return {"event": event, "payload": payload}


def register(ctx):
    ctx.add_tool(EchoTool())
    ctx.add_hook(
        handler,
        ["before_tool_call"],
        priority=7,
        timeout_s=2.5,
        fail_mode="closed",
        can_deny=True,
    )
    ctx.add_mcp_server(
        "sidecar",
        MCPServerConfig(transport="stdio", command="python", trusted=True),
    )
"""


@pytest.mark.asyncio
async def test_python_plugin_registers_hook_tool_and_mcp(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    write_plugin(
        root,
        "pyplug",
        permissions=["python", "hooks", "mcp"],
        python_source=PYTHON_PLUGIN_SOURCE,
    )
    test_harness = harness(tmp_path, root, allow_python=True, hooks_allow_external=True)

    report = await test_harness.manager.load(test_harness.context)

    assert report.loaded == ["pyplug"]
    assert report.failed == {}
    assert test_harness.registry.has("plugin_echo")
    assert set(test_harness.mcp.servers) == {"pyplug_sidecar"}
    assert "bear_plugin_pyplug" in sys.modules

    registration = test_harness.hooks.registrations[0]
    assert registration.source == "plugin:pyplug"
    assert registration.events == ["before_tool_call"]
    assert registration.priority == 7
    assert registration.timeout_s == 2.5
    assert registration.fail_mode == "closed"
    assert registration.can_deny is True
    assert await test_harness.hooks.fire("before_tool_call", {"n": 1}) == [
        {"event": "before_tool_call", "payload": {"n": 1}}
    ]

    contributions = report.contributions["pyplug"]
    assert [tool.name for tool in contributions.tools] == ["plugin_echo"]
    assert len(contributions.hook_registrations) == 1
    assert contributions.module_name == "bear_plugin_pyplug"

    assert await test_harness.manager.unload("pyplug") is True
    assert test_harness.registry.names() == []
    assert test_harness.hooks.registrations == []
    assert test_harness.mcp.servers == {}
    assert "bear_plugin_pyplug" not in sys.modules
    info = test_harness.manager.info("pyplug")
    assert info is not None
    assert info.status is PluginStatus.UNLOADED
    assert info.loaded is False
    assert await test_harness.manager.unload("pyplug") is False
    with pytest.raises(PluginLoadError, match="unknown plugin"):
        await test_harness.manager.unload("absent")


@pytest.mark.asyncio
async def test_python_plugin_denied_by_config_never_runs(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    marker = tmp_path / "executed.marker"
    source = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n"
        "\n"
        "def register(ctx):\n"
        "    pass\n"
    )
    write_plugin(
        root,
        "pyplug",
        permissions=["python"],
        python_source=source,
    )
    test_harness = harness(tmp_path, root, allow_python=False)

    report = await test_harness.manager.load(test_harness.context)

    assert report.loaded == []
    assert "allow_python" in report.failed["pyplug"]
    assert marker.exists() is False
    assert "bear_plugin_pyplug" not in sys.modules


@pytest.mark.asyncio
async def test_python_register_failure_fails_dependents_without_partial_apply(
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    write_plugin(
        root,
        "broken",
        permissions=["python", "hooks"],
        hooks=[{"id": "lint", "event": "before_tool_call", "command": "python"}],
        python_source=("def register(ctx):\n    raise RuntimeError('boom')\n"),
    )
    write_plugin(
        root,
        "dependent",
        dependencies=["broken"],
        permissions=["hooks"],
        hooks=[{"id": "dep", "event": "before_tool_call", "command": "python"}],
    )
    write_plugin(root, "solo")

    test_harness = harness(tmp_path, root, allow_python=True, hooks_allow_external=True)
    report = await test_harness.manager.load(test_harness.context)

    assert report.loaded == ["solo"]
    assert "RuntimeError" in report.failed["broken"]
    assert "failed to load" in report.failed["dependent"]
    assert test_harness.hooks.commands == []
    assert test_harness.hooks.registrations == []
    assert "bear_plugin_broken" not in sys.modules


@pytest.mark.asyncio
async def test_python_contributions_require_declared_permissions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    write_plugin(
        root,
        "hooksbad",
        permissions=["python"],
        python_source=(
            "def handler(event, payload=None):\n"
            "    return None\n"
            "\n"
            "def register(ctx):\n"
            "    ctx.add_hook(handler, ['before_tool_call'])\n"
        ),
    )
    write_plugin(
        root,
        "mcpbad",
        permissions=["python"],
        python_source=(
            "from bear_agent.config.models import MCPServerConfig\n"
            "\n"
            "def register(ctx):\n"
            "    ctx.add_mcp_server(\n"
            "        'sidecar',\n"
            "        MCPServerConfig(transport='stdio', command='python', trusted=True),\n"
            "    )\n"
        ),
    )

    test_harness = harness(tmp_path, root, allow_python=True)
    report = await test_harness.manager.load(test_harness.context)

    assert report.loaded == []
    assert "permissions.hooks" in report.failed["hooksbad"]
    assert "permissions.mcp" in report.failed["mcpbad"]
    assert test_harness.hooks.registrations == []
    assert test_harness.mcp.servers == {}


@pytest.mark.asyncio
async def test_python_tool_conflict_rejects_plugin_atomically(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    source = """
from pydantic import BaseModel

from bear_agent.tools.base import Tool, ToolResult


class Args(BaseModel):
    pass


class Existing(Tool):
    name = "existing"
    description = "conflicts with a builtin"
    args_model = Args

    async def run(self, args, ctx):
        return ToolResult(content="")


def register(ctx):
    ctx.add_tool(Existing())
"""
    write_plugin(
        root,
        "conflict",
        permissions=["python", "hooks"],
        hooks=[{"id": "lint", "event": "before_tool_call", "command": "python"}],
        python_source=source,
    )
    registry = ToolRegistry()
    registry.register(ExistingTool())

    test_harness = harness(
        tmp_path,
        root,
        allow_python=True,
        hooks_allow_external=True,
        registry=registry,
    )
    report = await test_harness.manager.load(test_harness.context)

    assert report.loaded == []
    assert any("tool name conflict" in conflict for conflict in report.conflicts)
    assert test_harness.registry.names() == ["existing"]
    assert test_harness.hooks.commands == []
    assert test_harness.hooks.registrations == []


# -------------------------------------------------------------- validate/CLI


@pytest.mark.asyncio
async def test_validate_all_reports_manifest_dependency_and_name_problems(
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    write_plugin(root, "good")
    write_plugin(root, "future", requires_bear=">=9.0")
    write_plugin(root, "missingdep", dependencies=["absent"])
    write_plugin(root, "x", dependencies=["y"])
    write_plugin(root, "y", dependencies=["x"])
    write_plugin(
        root,
        "python_disabled",
        permissions=["python"],
        python_source="def register(ctx):\n    pass\n",
    )
    write_plugin(
        root,
        "hooks_disabled",
        permissions=["hooks"],
        hooks=[{"id": "lint", "event": "before_tool_call", "command": "python"}],
    )
    server = {"transport": "stdio", "command": "python", "trusted": True}
    write_plugin(root, "a", permissions=["mcp"], mcp_servers={"b_c": dict(server)})
    write_plugin(root, "a_b", permissions=["mcp"], mcp_servers={"c": dict(server)})

    bad = root / "badtoml"
    bad.mkdir()
    (bad / "plugin.toml").write_text("schema_version = = 1", encoding="utf-8")

    test_harness = harness(tmp_path, root)
    validation = test_harness.manager.validate_all()
    by_id = {plugin.plugin_id: plugin for plugin in validation.plugins}

    assert validation.ok is False
    assert by_id["good"].ok is True
    assert any("requires Bear-Agent" in error for error in by_id["future"].errors)
    assert any("missing dependency" in error for error in by_id["missingdep"].errors)
    assert any("cycle" in error for error in by_id["x"].errors)
    assert any("cycle" in error for error in by_id["y"].errors)
    assert any("allow_python" in error for error in by_id["python_disabled"].errors)
    assert any("allow_external" in error for error in by_id["hooks_disabled"].errors)
    assert any("a_b_c" in conflict for conflict in validation.conflicts)
    assert any("invalid TOML" in error for error in validation.discovery_errors)
    assert validation.as_dict()["ok"] is False

    status = test_harness.manager.status()
    assert status["discovery_errors"] == validation.discovery_errors
    assert isinstance(status["conflicts"], list)


@pytest.mark.asyncio
async def test_cli_facing_contract_list_status_info_enable_disable_validate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins"
    write_plugin(root, "demo", enabled_by_default=False)
    test_harness = harness(tmp_path, root)
    manager = test_harness.manager

    listed = manager.list()
    assert [item.plugin_id for item in listed] == ["demo"]
    assert listed[0].enabled is False
    assert listed[0].status is PluginStatus.DISABLED
    assert manager.info("absent") is None

    status = manager.status()
    for key in (
        "config_enabled",
        "allow_python",
        "hooks_allow_external",
        "state_path",
        "loaded",
        "failed",
        "disabled",
        "plugins",
    ):
        assert key in status

    assert manager.enable("demo") is True
    report = await manager.load(test_harness.context)
    assert report.loaded == ["demo"]
    assert report.as_dict()["loaded"] == ["demo"]
    assert manager.status()["loaded"] == ["demo"]
    info = manager.info("demo")
    assert info is not None and info.loaded is True and info.status is PluginStatus.LOADED

    assert await manager.disable("demo") is True
    info = manager.info("demo")
    assert info is not None
    assert info.enabled is False
    assert info.status is PluginStatus.DISABLED
    assert manager.status()["loaded"] == []

    validation = manager.validate_all()
    assert validation.ok is True
    assert validation.as_dict()["ok"] is True

    assert test_harness.hooks.commands == []
    assert test_harness.hooks.registrations == []


@pytest.mark.asyncio
async def test_invalid_manifest_does_not_block_other_plugins(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    invalid = root / "invalid"
    invalid.mkdir(parents=True)
    (invalid / "plugin.toml").write_text(plugin_toml("Invalid Id"), encoding="utf-8")
    write_plugin(root, "solo")

    test_harness = harness(tmp_path, root)
    report = await test_harness.manager.load(test_harness.context)

    assert report.loaded == ["solo"]
    assert any("invalid plugin manifest" in error for error in report.discovery_errors)
    assert report.ok is False


def test_default_hook_registration_factory_uses_real_hook_models() -> None:
    from bear_agent.hooks.events import HookEvent
    from bear_agent.hooks.models import HookRegistration
    from bear_agent.plugins.api import default_hook_registration_factory

    def handler(invocation):
        return None

    registration = default_hook_registration_factory(
        id="plugin:demo:hook:0",
        handler=handler,
        events=["before_tool_call"],
        priority=3,
        timeout_s=None,
        fail_mode="closed",
        can_deny=False,
        can_append_context=False,
        source="plugin:demo",
    )

    assert isinstance(registration, HookRegistration)
    assert registration.id == "plugin:demo:hook:0"
    assert registration.events == frozenset({HookEvent.TOOL_BEFORE})
    assert registration.timeout_s == 0.0
    assert registration.source == "plugin:demo"


@pytest.mark.asyncio
async def test_validate_after_load_preserves_runtime_state(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    write_plugin(
        root,
        "demo",
        permissions=["hooks"],
        hooks=[{"id": "lint", "event": "before_tool_call", "command": "python"}],
    )
    test_harness = harness(tmp_path, root, hooks_allow_external=True)
    report = await test_harness.manager.load(test_harness.context)
    assert report.loaded == ["demo"]

    validation = test_harness.manager.validate_all()
    assert validation.ok is True
    info = test_harness.manager.info("demo")
    assert info is not None and info.status is PluginStatus.LOADED
    assert [command.id for command in test_harness.hooks.commands] == ["plugin:demo:lint"]


@pytest.mark.asyncio
async def test_python_module_name_collision_is_a_conflict(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    write_plugin(
        root,
        "a-b",
        permissions=["python"],
        python_source="def register(ctx):\n    pass\n",
    )
    write_plugin(
        root,
        "a_b",
        permissions=["python"],
        python_source="def register(ctx):\n    pass\n",
    )
    test_harness = harness(tmp_path, root, allow_python=True)
    report = await test_harness.manager.load(test_harness.context)

    assert report.loaded == ["a-b"]
    assert "a_b" in report.failed
    assert any("module name collision" in conflict for conflict in report.conflicts)

    validation = test_harness.manager.validate_all()
    assert any("module name collision" in conflict for conflict in validation.conflicts)


@pytest.mark.asyncio
async def test_toggling_global_config_off_unloads_plugins(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    write_plugin(
        root,
        "demo",
        permissions=["hooks"],
        hooks=[{"id": "lint", "event": "before_tool_call", "command": "python"}],
    )
    test_harness = harness(tmp_path, root, hooks_allow_external=True)
    report = await test_harness.manager.load(test_harness.context)
    assert report.loaded == ["demo"]
    assert len(test_harness.hooks.commands) == 1

    test_harness.manager.config.enabled = False
    disabled = await test_harness.manager.load(test_harness.context)
    assert disabled.disabled is True
    assert test_harness.hooks.commands == []
