"""Public plugin API and contribution types.

Two boundaries are deliberate:

* ``PluginContext`` is the only object a Python entrypoint receives. It records
  contributions while a plugin is being staged, so a broken plugin never leaves
  half-registered hooks, MCP servers or tools behind.
* ``PluginContributions`` is the declarative result of a successful load. When
  the caller does not provide registration callbacks (for example because the
  HookManager or MCPManager is owned by another module), bootstrap applies
  these contributions itself.

Python plugins run in-process with the user's privileges. They are trusted
code, not a sandbox: a plugin can do anything Bear-Agent can do. Only enable
``plugins.allow_python`` for code you would run directly.
"""

from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from bear_agent.config.models import HookCommandConfig, MCPServerConfig
from bear_agent.core.errors import PluginLoadError
from bear_agent.tools.base import Tool
from bear_agent.tools.registry import ToolRegistry

_MCP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_FAIL_MODES = {"open", "closed"}

HookHandler = Callable[..., Any]
HookRegistrar = Callable[[Any], Any]
HookCommandRegistrar = Callable[[HookCommandConfig], Any]
UnregisterHookSource = Callable[[str], Any]
UnregisterHook = Callable[[str], Any]
ToolRegistrar = Callable[[Tool], Any]
ToolUnregistrar = Callable[[str], Any]
MCPServerRegistrar = Callable[[str, MCPServerConfig], Any]
MCPServerUnregistrar = Callable[[str], Any]
HookRegistrationFactory = Callable[..., Any]


def hook_source(plugin_id: str) -> str:
    """Stable hook source for one plugin, used by ``unregister_source``."""

    return f"plugin:{plugin_id}"


def namespaced_hook_command_id(plugin_id: str, hook_id: str) -> str:
    return f"{hook_source(plugin_id)}:{hook_id}"


def namespaced_mcp_server_name(plugin_id: str, server_name: str) -> str:
    """Namespace a plugin MCP server.

    MCP server names allow only ``[A-Za-z0-9_-]``, so ``:`` cannot be used.
    ``<plugin>_<server>`` is used instead; the manager rejects collisions and
    names that exceed the MCP 64-character limit.
    """

    return f"{plugin_id}_{server_name}"


def module_name_for(plugin_id: str) -> str:
    """Import name for a plugin entrypoint (must be a valid identifier)."""

    sanitized = re.sub(r"\W", "_", plugin_id)
    return f"bear_plugin_{sanitized}"


@dataclass(slots=True)
class PendingHook:
    """A hook added by Python code before the plugin is committed."""

    handler: HookHandler
    events: tuple[Any, ...]
    priority: int = 0
    timeout_s: float | None = None
    fail_mode: str = "open"
    can_deny: bool = False
    can_append_context: bool = False


@dataclass(slots=True)
class PluginContext:
    """Staging context passed to ``register(ctx)``.

    A fresh context is derived per plugin by :meth:`for_plugin`. Callbacks are
    optional: when absent the manager only returns :class:`PluginContributions`
    and never mutates another component. Callbacks may return an awaitable.
    """

    plugin_id: str = ""
    workspace: Path = field(default_factory=Path.cwd)
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("bear_agent.plugins"))

    # Commit callbacks. All are optional.
    register_hook: HookRegistrar | None = None
    register_hook_command: HookCommandRegistrar | None = None
    unregister_hook_source: UnregisterHookSource | None = None
    unregister_hook: UnregisterHook | None = None
    register_tool: ToolRegistrar | None = None
    unregister_tool: ToolUnregistrar | None = None
    tool_registry: ToolRegistry | None = None
    register_mcp_server: MCPServerRegistrar | None = None
    unregister_mcp_server: MCPServerUnregistrar | None = None
    hook_registration_factory: HookRegistrationFactory | None = None

    # Names already used outside the plugin system (builtin tools, configured
    # hook commands, configured MCP servers). Contributions that collide are
    # rejected before anything is applied.
    reserved_hook_ids: frozenset[str] = frozenset()
    reserved_mcp_servers: frozenset[str] = frozenset()
    reserved_tools: frozenset[str] = frozenset()

    # Staged contributions. Internal to PluginManager but intentionally visible
    # for tests and for future direct drivers.
    pending_hooks: list[PendingHook] = field(default_factory=list, repr=False)
    pending_tools: list[Tool] = field(default_factory=list, repr=False)
    pending_mcp_servers: dict[str, MCPServerConfig] = field(default_factory=dict, repr=False)

    @classmethod
    def from_managers(
        cls,
        *,
        workspace: Path | str,
        hook_manager: object | None = None,
        tool_registry: ToolRegistry | None = None,
        **kwargs: Any,
    ) -> PluginContext:
        """Wire HookManager and the tool registry.

        The agreed HookManager API is ``register(registration)`` and
        ``unregister_source(source)``. When the concrete manager also exposes
        ``register_command(command)`` and ``unregister(hook_id)``, command hooks
        declared by manifests are applied and removed directly; otherwise they
        remain as ``PluginContributions.hook_commands`` for bootstrap. All
        remaining keyword arguments are forwarded to the context.
        """

        context = cls(workspace=Path(workspace), tool_registry=tool_registry, **kwargs)
        if hook_manager is not None:
            register = getattr(hook_manager, "register", None)
            unregister_source = getattr(hook_manager, "unregister_source", None)
            register_command = getattr(hook_manager, "register_command", None)
            unregister = getattr(hook_manager, "unregister", None)
            if callable(register):
                context.register_hook = register
            if callable(unregister_source):
                context.unregister_hook_source = unregister_source
            if callable(register_command):
                context.register_hook_command = register_command
            if callable(unregister):
                context.unregister_hook = unregister
        if tool_registry is not None:
            register_tool = getattr(tool_registry, "register", None)
            unregister_tool = getattr(tool_registry, "unregister", None)
            if callable(register_tool):
                context.register_tool = register_tool
            if callable(unregister_tool):
                context.unregister_tool = unregister_tool
        return context

    def for_plugin(self, plugin_id: str) -> PluginContext:
        """Derive a fresh staging context for one plugin."""

        return PluginContext(
            plugin_id=plugin_id,
            workspace=self.workspace,
            log=logging.getLogger(f"bear_agent.plugins.{plugin_id}"),
            register_hook=self.register_hook,
            register_hook_command=self.register_hook_command,
            unregister_hook_source=self.unregister_hook_source,
            unregister_hook=self.unregister_hook,
            register_tool=self.register_tool,
            unregister_tool=self.unregister_tool,
            tool_registry=self.tool_registry,
            register_mcp_server=self.register_mcp_server,
            unregister_mcp_server=self.unregister_mcp_server,
            hook_registration_factory=self.hook_registration_factory,
            reserved_hook_ids=self.reserved_hook_ids,
            reserved_mcp_servers=self.reserved_mcp_servers,
            reserved_tools=self.reserved_tools,
        )

    # ------------------------------------------------------------ contributions

    def add_hook(
        self,
        handler: HookHandler,
        events: Iterable[str | Any] | str,
        *,
        priority: int = 0,
        timeout_s: float | None = None,
        fail_mode: str = "open",
        can_deny: bool = False,
        can_append_context: bool = False,
    ) -> None:
        """Register an in-process hook handler for this plugin.

        Handlers are trusted Python code. The agreed HookManager calls them as
        ``await handler(HookInvocation)`` and expects ``HookDecision | None``.
        ``events`` accepts hook event names/aliases or HookEvent members;
        conversion/validation is delegated to HookRegistration.
        """

        if not callable(handler):
            raise PluginLoadError(
                f"plugin {self.plugin_id or '<unknown>'}: hook handler must be callable"
            )
        if isinstance(events, str):
            normalized: tuple[Any, ...] = (events,)
        else:
            normalized = tuple(events)
        if not normalized:
            raise PluginLoadError(
                f"plugin {self.plugin_id or '<unknown>'}: add_hook requires at least one event"
            )
        if fail_mode not in _FAIL_MODES:
            raise PluginLoadError(
                f"plugin {self.plugin_id or '<unknown>'}: fail_mode must be 'open' or 'closed'"
            )
        if timeout_s is not None and timeout_s <= 0:
            raise PluginLoadError(
                f"plugin {self.plugin_id or '<unknown>'}: timeout_s must be positive"
            )
        self.pending_hooks.append(
            PendingHook(
                handler=handler,
                events=normalized,
                priority=int(priority),
                timeout_s=timeout_s,
                fail_mode=fail_mode,
                can_deny=bool(can_deny),
                can_append_context=bool(can_append_context),
            )
        )

    def add_tool(self, tool: Tool) -> None:
        if not isinstance(tool, Tool):
            raise PluginLoadError(
                f"plugin {self.plugin_id or '<unknown>'}: add_tool expects a "
                "bear_agent.tools.base.Tool instance"
            )
        if not tool.name:
            raise PluginLoadError(
                f"plugin {self.plugin_id or '<unknown>'}: tool name must not be empty"
            )
        self.pending_tools.append(tool)

    def add_mcp_server(self, name: str, config: MCPServerConfig) -> None:
        if not _MCP_NAME_RE.match(name):
            raise PluginLoadError(
                f"plugin {self.plugin_id or '<unknown>'}: invalid MCP server name "
                f"{name!r}; use 1..64 characters from [A-Za-z0-9_-]"
            )
        if not isinstance(config, MCPServerConfig):
            raise PluginLoadError(
                f"plugin {self.plugin_id or '<unknown>'}: add_mcp_server expects "
                "a bear_agent.config.models.MCPServerConfig"
            )
        if config.enabled and not config.trusted:
            raise PluginLoadError(
                f"plugin {self.plugin_id or '<unknown>'}: MCP server {name!r} is "
                "enabled but not trusted"
            )
        if name in self.pending_mcp_servers:
            raise PluginLoadError(
                f"plugin {self.plugin_id or '<unknown>'}: duplicate MCP server name {name!r}"
            )
        self.pending_mcp_servers[name] = config


@dataclass(slots=True)
class PluginContributions:
    """Everything one loaded plugin contributes.

    ``hook_commands`` and ``mcp_servers`` are already namespaced. Python hook
    registrations are concrete HookRegistration objects built with the provided
    factory; ``tools`` are Tool instances.
    """

    plugin_id: str
    hook_commands: tuple[HookCommandConfig, ...] = ()
    mcp_servers: dict[str, MCPServerConfig] = field(default_factory=dict)
    hook_registrations: tuple[Any, ...] = ()
    tools: tuple[Tool, ...] = ()
    module_name: str | None = None

    @property
    def source(self) -> str:
        return hook_source(self.plugin_id)

    @property
    def empty(self) -> bool:
        return not (self.hook_commands or self.mcp_servers or self.hook_registrations or self.tools)


class PluginStatus(StrEnum):
    DISCOVERED = "discovered"
    LOADED = "loaded"
    UNLOADED = "unloaded"
    DISABLED = "disabled"
    FAILED = "failed"


class PluginInfo(BaseModel):
    """Stable, serializable view used by ``list``/``info`` and the CLI."""

    plugin_id: str
    name: str
    version: str
    path: str
    description: str = ""
    author: str = ""
    requires_bear: str = ""
    dependencies: list[str] = Field(default_factory=list)
    enabled: bool = False
    status: PluginStatus = PluginStatus.DISCOVERED
    error: str | None = None
    hooks: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    python: bool = False
    entrypoint: str | None = None
    loaded: bool = False


@dataclass(slots=True)
class PluginLoadReport:
    loaded: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    contributions: dict[str, PluginContributions] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    discovery_errors: list[str] = field(default_factory=list)
    disabled: bool = False

    @property
    def ok(self) -> bool:
        return not (self.disabled or self.failed or self.conflicts or self.discovery_errors)

    def as_dict(self) -> dict[str, Any]:
        return {
            "disabled": self.disabled,
            "loaded": list(self.loaded),
            "failed": dict(sorted(self.failed.items())),
            "skipped": list(self.skipped),
            "conflicts": list(self.conflicts),
            "order": list(self.order),
            "discovery_errors": list(self.discovery_errors),
            "contributions": sorted(self.contributions),
            "ok": self.ok,
        }


@dataclass(slots=True)
class PluginValidation:
    plugin_id: str
    path: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(slots=True)
class PluginValidationReport:
    plugins: list[PluginValidation] = field(default_factory=list)
    discovery_errors: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[str]:
        collected = list(self.discovery_errors) + list(self.conflicts)
        for plugin in self.plugins:
            collected.extend(f"{plugin.plugin_id}: {error}" for error in plugin.errors)
        return collected

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "conflicts": list(self.conflicts),
            "discovery_errors": list(self.discovery_errors),
            "plugins": [
                {
                    "plugin_id": plugin.plugin_id,
                    "path": plugin.path,
                    "ok": plugin.ok,
                    "errors": list(plugin.errors),
                    "warnings": list(plugin.warnings),
                }
                for plugin in self.plugins
            ],
        }


async def maybe_await(value: Any) -> Any:
    """Await ``value`` when a callback returned an awaitable."""

    if inspect.isawaitable(value):
        return await value
    return value


def default_hook_registration_factory(**kwargs: Any) -> Any:
    """Build a HookRegistration from the agreed ``bear_agent.hooks.models`` API.

    Imported lazily so the plugin framework does not create an import cycle with
    the hooks module. Event strings (including the documented aliases) are
    converted to HookEvent members, and ``timeout_s=None`` means "use the
    HookManager default" (recorded as 0.0).
    """

    try:
        from bear_agent.hooks.events import HookEvent
        from bear_agent.hooks.models import HookRegistration
    except ImportError as exc:  # pragma: no cover - depends on concurrent module
        raise PluginLoadError(
            "a Python plugin adds in-process hooks, but bear_agent.hooks.models is "
            "not available; wire PluginContext(register_hook=...) or "
            "hook_registration_factory=... explicitly"
        ) from exc
    raw_events = kwargs.get("events", ())
    kwargs["events"] = frozenset(
        event if isinstance(event, HookEvent) else HookEvent.from_string(str(event))
        for event in raw_events
    )
    if kwargs.get("timeout_s") is None:
        kwargs["timeout_s"] = 0.0
    return HookRegistration(**kwargs)


__all__ = [
    "PendingHook",
    "PluginContext",
    "PluginContributions",
    "PluginInfo",
    "PluginLoadReport",
    "PluginStatus",
    "PluginValidation",
    "PluginValidationReport",
    "default_hook_registration_factory",
    "hook_source",
    "maybe_await",
    "module_name_for",
    "namespaced_hook_command_id",
    "namespaced_mcp_server_name",
]
