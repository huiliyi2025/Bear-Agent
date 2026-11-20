"""Plugin framework: declarative manifests with an optional trusted entrypoint."""

from bear_agent.plugins.api import (
    PendingHook,
    PluginContext,
    PluginContributions,
    PluginInfo,
    PluginLoadReport,
    PluginStatus,
    PluginValidation,
    PluginValidationReport,
    hook_source,
    namespaced_hook_command_id,
    namespaced_mcp_server_name,
)
from bear_agent.plugins.manager import PluginManager
from bear_agent.plugins.manifest import (
    PluginManifest,
    PluginPermissions,
    load_manifest,
)

__all__ = [
    "PendingHook",
    "PluginContext",
    "PluginContributions",
    "PluginInfo",
    "PluginLoadReport",
    "PluginManager",
    "PluginManifest",
    "PluginPermissions",
    "PluginStatus",
    "PluginValidation",
    "PluginValidationReport",
    "hook_source",
    "load_manifest",
    "namespaced_hook_command_id",
    "namespaced_mcp_server_name",
]
