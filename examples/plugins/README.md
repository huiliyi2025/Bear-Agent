# Bear-Agent plugins

A plugin is a declarative extension package. It is not a synonym for MCP and it
does not expose one large "do anything" interface. A plugin directory contains
`plugin.toml`; it may also contain a Python entrypoint that is loaded only when
the user explicitly trusts it.

```text
my_plugin/
  plugin.toml      # required manifest
  plugin.py        # optional, only with permissions.python and plugins.allow_python
```

Plugins can contribute three kinds of things:

| Contribution | Declared in | Permission | Applied through |
| --- | --- | --- | --- |
| External hook commands | `[[hooks]]` | `permissions.hooks` + `hooks.allow_external` | HookManager command API / contributions |
| MCP servers | `[mcp_servers.<name>]` | `permissions.mcp` + `trusted = true` | MCPManager / contributions |
| In-process Python hooks, tools, MCP servers | `register(ctx)` | `permissions.python` + `plugins.allow_python` | HookManager, ToolRegistry, MCPManager |

Contributions are namespaced before they reach shared state:

- hook command ids: `plugin:<plugin id>:<hook id>`
- hook unload source: `plugin:<plugin id>`
- MCP server names: `<plugin id>_<server name>` (MCP names cannot contain `:`)
- Python tool names are **not** automatically prefixed; collisions with existing
  tools are rejected instead.

## Manifest

```toml
schema_version = 1                  # only schema version 1 is supported
id = "example_mcp"                  # 1..64 chars: lowercase letters, digits, _ or -
name = "Example MCP server"
version = "0.1.0"                   # PEP 440
description = "Optional"
author = "Optional"
requires_bear = ">=0.4.0,<0.7.0"    # optional PEP 440 specifier
homepage = "Optional"
dependencies = ["other_plugin>=1.0"] # optional; resolved in dependency order
enabled_by_default = false          # default is false

[permissions]
mcp = true                          # required for [mcp_servers.*]
# hooks = true                      # required for [[hooks]]
# python = true                     # required for a Python entrypoint

[mcp_servers.example_docs]
transport = "stdio"
command = "python"
args = ["-m", "example_mcp_server"]
trusted = true                      # required when enabled
enabled = true
```

`entrypoint` defaults to `plugin.py` and is only used with `permissions.python`.
The file must exist inside the plugin directory; absolute paths and `..` are
rejected. An explicit custom entrypoint without the Python permission is a
manifest error.

Dependency syntax is `plugin_id` or `plugin_id>=1.0,<2.0`. Missing, disabled,
version-incompatible or cyclic dependencies cause the dependent plugin (not the
whole manager) to fail. A duplicate plugin id found in two directories is a
conflict and neither copy is loaded.

## Trust and security

- **Python is off by default.** A Python entrypoint runs in-process with the
  user's full privileges. There is no sandbox, no capability isolation and no
  promise that a malicious plugin can be contained. Enable
  `plugins.allow_python = true` only for code you would run directly.
- **A Python plugin also needs `permissions.python = true`.** Config opt-in and
  manifest permission are both required.
- **MCP servers must be trusted.** Every enabled server requires
  `trusted = true`. Manifest-only plugins do not need Python, but a compromised
  manifest can still start an arbitrary executable; review the command.
- **External hook commands are opt-in.** `hooks.allow_external = true` is
  required because command hooks are OS subprocesses with the user's privileges.
- **No installation or network fetch.** The plugin framework never downloads or
  installs packages and never reads plugin definitions from model output.
- **No partial application.** A plugin's contributions are staged, conflict
  checked, then committed. A failure rolls back what was applied and marks only
  that plugin (and its dependents) failed.
- **Unload is best effort.** Hooks and tools can be unregistered. A Python
  module cannot be truly unloaded from the interpreter; it is removed from
  `sys.modules` so a later load imports a fresh module, but objects and side
  effects created by the old module remain. Restart the process for a hard
  reset.
- **State is persisted locally.** `<data_dir>/plugins/state.json` stores only
  `{"plugin_id": {"enabled": bool}}`, written atomically with a temporary file
  and `os.replace`. It never stores plugin code, credentials or command output.

## Python entrypoint

```python
from pydantic import BaseModel
from bear_agent.config.models import MCPServerConfig
from bear_agent.hooks.models import HookDecision, HookInvocation
from bear_agent.tools.base import Tool, ToolResult


class EchoArgs(BaseModel):
    text: str = ""


class EchoTool(Tool):
    name = "plugin_echo"
    description = "Echo text"
    args_model = EchoArgs

    async def run(self, args, ctx):
        return ToolResult(content=args.text)


async def on_event(invocation: HookInvocation) -> HookDecision | None:
    # In-process hook handlers are async and receive a HookInvocation.
    return HookDecision()


def register(ctx):
    ctx.add_tool(EchoTool())
    ctx.add_hook(
        on_event,
        ["before_tool_call"],  # event names/aliases or HookEvent members
        priority=0,
        timeout_s=None,  # None -> HookManager default
        fail_mode="open",  # or "closed"
        can_deny=False,  # requires exactly tool_before
        can_append_context=False,  # requires exactly model_before
    )
    ctx.add_mcp_server(
        "sidecar",
        MCPServerConfig(transport="stdio", command="python", trusted=True),
    )
```

The context also exposes `plugin_id`, `workspace` and `log`. Python handlers are
in-process and trusted. A plugin that adds hooks or MCP servers must declare the
matching permission in its manifest; otherwise loading fails.

## CLI flow

The plugin subcommands are a thin façade over `PluginManager`:

```text
bear plugins list                 # discovered plugins + status
bear plugins info <id>            # manifest, permissions, namespaced contributions
bear plugins enable <id>          # persists enabled=true (does not load at runtime)
bear plugins disable <id>         # persists enabled=false and unloads at runtime
bear plugins validate             # manifests, requires_bear, dependencies, cycles, names
```

`PluginManager.status()` returns the full machine-readable view (global config,
loaded/failed/disabled sets, discovery errors); the CLI surfaces the same data
through `plugins list` and `plugins validate`.

Typical flow:

```bash
bear plugins validate                 # inspect every manifest before trusting it
bear plugins enable example_mcp       # explicit opt-in; state.json is written
bear plugins list                     # confirm it will be loaded on the next start
bear plugins disable example_mcp      # fail closed; unloads when a manager is active
```

`enable` only persists intent; the next `PluginManager.load(context)` performs
the load. `disable` persists `false` first and then unloads, so a failed unload
still leaves the plugin disabled on the next start.

## Bootstrap integration

`PluginManager` does not construct HookManager, MCPManager or the runtime. Wire
the agreed HookManager API (`register`, `unregister_source`) and the tool
registry; pass extra callbacks only when the concrete owner exposes them.

```python
from bear_agent.plugins import PluginContext, PluginManager

manager = PluginManager(
    settings.plugins,
    data_dir=settings.resolved_data_dir(),
    hooks_allow_external=settings.hooks.allow_external,
    hooks_enabled=settings.hooks.enabled,
)
context = PluginContext.from_managers(
    workspace=cwd,
    # register(reg), unregister_source(source), and — when the concrete
    # manager exposes them — register_command(command), unregister(hook_id).
    hook_manager=hook_manager,
    tool_registry=container.registry,  # register(tool), unregister(name)
    # register_mcp_server=mcp_manager.add_server,      # if available
    # unregister_mcp_server=mcp_manager.remove_server,
    reserved_mcp_servers=set(settings.mcp.servers),
    reserved_hook_ids={command.id for command in settings.hooks.commands},
)
report = await manager.load(context)

# If a component-specific callback was not supplied, apply the declarative
# contributions here instead:
for contributions in report.contributions.values():
    for command in contributions.hook_commands:
        ...  # register with the HookManager command API
    for name, server in contributions.mcp_servers.items():
        ...  # add to the MCPManager
```

Load plugins after the builtin and task tools are registered so tool name
conflicts are detected before any plugin tool is applied. If the HookManager
exposes `register_command`, manifest hook commands are applied immediately and
removed with `unregister("cmd:<namespaced id>")` on unload. If it does not, keep
`report.contributions[plugin_id].hook_commands` and feed them into whatever
command-hook configuration the HookManager consumes at construction time.

The MCP manager reads its server map at construction. If it has no dynamic
`add_server` API, load plugins before constructing it and merge namespaced
`PluginContributions.mcp_servers` into `MCPConfig.servers`; otherwise wire the
optional `register_mcp_server`/`unregister_mcp_server` callbacks.