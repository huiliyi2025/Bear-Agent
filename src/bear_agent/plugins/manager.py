"""Plugin discovery, dependency resolution, loading and unloading.

The manager owns plugin state and the staging/commit boundary. A plugin's
contributions are collected first and applied only after every conflict check
passes; a plugin that fails never leaves half-registered hooks or tools behind.
Python entrypoints are imported only when the manifest declares the ``python``
permission *and* ``plugins.allow_python`` is true.

The manager deliberately does not construct HookManager, MCPManager or a
runtime. Callers pass callbacks through :class:`PluginContext`; when a callback
is absent the manager simply returns :class:`PluginContributions` so bootstrap
can wire the remaining components. Python plugins run in-process as trusted
code; there is no sandbox.
"""

from __future__ import annotations

import contextlib
import heapq
import importlib.util
import inspect
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from bear_agent import __version__
from bear_agent.config.models import HookCommandConfig, MCPServerConfig, PluginsConfig
from bear_agent.core.errors import PluginLoadError, PluginManifestError
from bear_agent.plugins.api import (
    PendingHook,
    PluginContext,
    PluginContributions,
    PluginInfo,
    PluginLoadReport,
    PluginStatus,
    PluginValidation,
    PluginValidationReport,
    default_hook_registration_factory,
    hook_source,
    maybe_await,
    module_name_for,
    namespaced_hook_command_id,
    namespaced_mcp_server_name,
)
from bear_agent.plugins.manifest import PLUGIN_TOML, PluginManifest, load_manifest
from bear_agent.tools.base import Tool

_MCP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class _ContributionConflict(PluginLoadError):
    """A name collision that must be reported as a conflict."""


@dataclass(slots=True)
class _Record:
    plugin_id: str
    path: Path
    manifest: PluginManifest
    status: PluginStatus = PluginStatus.DISCOVERED
    error: str | None = None
    contributions: PluginContributions | None = None
    module_name: str | None = None
    applied_hook_commands: tuple[str, ...] = ()
    applied_hook_registrations: bool = False
    applied_tools: tuple[str, ...] = ()
    applied_mcp_servers: tuple[str, ...] = ()


class PluginManager:
    def __init__(
        self,
        config: PluginsConfig,
        *,
        data_dir: Path,
        bear_version: str | None = None,
        hooks_allow_external: bool = False,
        hooks_enabled: bool = True,
    ) -> None:
        self.config = config
        self.data_dir = Path(data_dir).expanduser()
        self.bear_version = bear_version or __version__
        self.hooks_allow_external = hooks_allow_external
        self.hooks_enabled = hooks_enabled
        self.state_path = self.data_dir / "plugins" / "state.json"

        self._state: dict[str, dict[str, bool]] = {}
        self._state_error: str | None = None
        self._read_state()

        self._records: dict[str, _Record] = {}
        self._discovery_errors: list[str] = []
        self._discovery_conflicts: list[str] = []
        self._scanned = False
        self._active_context: PluginContext | None = None

        # Names claimed by successfully loaded plugins. They are released on
        # unload so another plugin may use the name afterwards.
        self._claimed_hook_ids: set[str] = set()
        self._claimed_mcp_servers: set[str] = set()
        self._claimed_tools: set[str] = set()
        self._claimed_modules: set[str] = set()

    # --------------------------------------------------------------- discovery

    def _discovery_dirs(self) -> list[Path]:
        directories: list[Path] = []
        seen: set[Path] = set()
        for raw in self.config.dirs:
            directory = Path(raw).expanduser()
            try:
                resolved = directory.resolve()
            except OSError:
                resolved = directory.absolute()
            if resolved in seen:
                continue
            seen.add(resolved)
            directories.append(resolved)
        default_dir = self.data_dir / "plugins"
        try:
            resolved_default = default_dir.resolve()
        except OSError:
            resolved_default = default_dir.absolute()
        if resolved_default not in seen and default_dir.is_dir():
            directories.append(default_dir)
        return directories

    def _discover(self, *, force: bool = False) -> None:
        if self._scanned and not force:
            return
        self._records = {}
        self._discovery_errors = []
        self._discovery_conflicts = []
        self._scanned = True

        candidates: list[Path] = []
        seen: set[Path] = set()
        for directory in self._discovery_dirs():
            if not directory.is_dir():
                continue
            try:
                children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
            except OSError as exc:
                self._discovery_errors.append(f"cannot scan plugin directory {directory}: {exc}")
                continue
            for child in children:
                if not child.is_dir() or not (child / PLUGIN_TOML).is_file():
                    continue
                try:
                    resolved = child.resolve()
                except OSError:
                    resolved = child.absolute()
                if resolved in seen:
                    continue
                seen.add(resolved)
                candidates.append(resolved)

        limit = max(0, self.config.max_plugins)
        if len(candidates) > limit:
            for extra in candidates[limit:]:
                self._discovery_errors.append(
                    f"{extra}: plugin limit reached (plugins.max_plugins={limit})"
                )
            candidates = candidates[:limit]

        by_id: dict[str, list[tuple[Path, PluginManifest]]] = {}
        for path in candidates:
            try:
                manifest = load_manifest(path)
            except PluginManifestError as exc:
                self._discovery_errors.append(str(exc))
                continue
            by_id.setdefault(manifest.id, []).append((path, manifest))

        for plugin_id in sorted(by_id):
            entries = by_id[plugin_id]
            if len(entries) > 1:
                locations = ", ".join(str(path) for path, _manifest in entries)
                message = (
                    f"duplicate plugin id {plugin_id!r} in {locations}; "
                    "refusing to load any of them"
                )
                self._discovery_conflicts.append(message)
                self._discovery_errors.append(message)
                continue
            path, manifest = entries[0]
            self._records[plugin_id] = _Record(plugin_id=plugin_id, path=path, manifest=manifest)

    def _ensure_discovered(self) -> None:
        self._discover(force=False)

    # -------------------------------------------------------------------- state

    def _read_state(self) -> None:
        self._state = {}
        self._state_error = None
        if not self.state_path.is_file():
            return
        try:
            raw: Any = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._state_error = f"cannot read plugin state {self.state_path}: {exc}"
            return
        if not isinstance(raw, dict):
            self._state_error = f"plugin state {self.state_path} must be a JSON object; ignoring it"
            return
        for plugin_id, value in raw.items():
            if not isinstance(plugin_id, str) or not isinstance(value, dict):
                self._state_error = (
                    f"plugin state {self.state_path} has malformed entries; valid entries were kept"
                )
                continue
            enabled = value.get("enabled")
            if isinstance(enabled, bool):
                self._state[plugin_id] = {"enabled": enabled}
            else:
                self._state_error = (
                    f"plugin state {self.state_path} entry {plugin_id!r} has no "
                    "boolean 'enabled'; it was ignored"
                )

    def _write_state(self) -> None:
        payload = {
            plugin_id: {"enabled": bool(value.get("enabled", False))}
            for plugin_id, value in sorted(self._state.items())
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            handle_fd, temporary = tempfile.mkstemp(
                prefix=".state-", suffix=".tmp", dir=self.state_path.parent
            )
            try:
                with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.state_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)
                raise
        except OSError as exc:
            raise PluginLoadError(f"cannot write plugin state {self.state_path}: {exc}") from exc
        self._state_error = None

    def _effective_enabled(self, record: _Record) -> bool:
        override = self._state.get(record.plugin_id)
        if override is not None:
            return override["enabled"]
        if self._state_error:
            # Fail closed: a disabled plugin must not come back because the
            # state file was corrupted or unreadable.
            return False
        return record.manifest.enabled_by_default

    # ---------------------------------------------------------------- validate

    def _manifest_failure(self, manifest: PluginManifest) -> str | None:
        if manifest.requires_bear:
            try:
                current = Version(self.bear_version)
            except InvalidVersion:
                return (
                    f"requires Bear-Agent {manifest.requires_bear}, but the current "
                    f"version {self.bear_version!r} is not a valid PEP 440 version"
                )
            if not manifest.bear_specifier.contains(current, prereleases=True):
                return (
                    f"requires Bear-Agent {manifest.requires_bear}; current version "
                    f"is {self.bear_version}"
                )
        if manifest.permissions.python and not self.config.allow_python:
            return (
                "declares a Python entrypoint but plugins.allow_python is false; "
                "only enable this for code you trust to run in-process"
            )
        if manifest.hooks and not self.hooks_enabled:
            return "declares hook commands but hooks are disabled by configuration"
        if manifest.hooks and not self.hooks_allow_external:
            return (
                "declares external hook commands but hooks.allow_external is false; "
                "external hooks run with the user's OS privileges"
            )
        return None

    def _cycle_nodes(self, records: Mapping[str, _Record]) -> set[str]:
        nodes = set(records)
        indegree = dict.fromkeys(nodes, 0)
        children: dict[str, list[str]] = {plugin_id: [] for plugin_id in nodes}
        for plugin_id, record in records.items():
            for dependency in record.manifest.dependency_specifiers:
                if dependency in nodes:
                    indegree[plugin_id] += 1
                    children[dependency].append(plugin_id)
        ready = [plugin_id for plugin_id in nodes if indegree[plugin_id] == 0]
        heapq.heapify(ready)
        visited: set[str] = set()
        while ready:
            plugin_id = heapq.heappop(ready)
            visited.add(plugin_id)
            for child in children[plugin_id]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    heapq.heappush(ready, child)
        return nodes - visited

    def validate_all(self) -> PluginValidationReport:
        # Do not force a rescan: it would discard loaded runtime state in the
        # same manager. A fresh manager (the CLI case) performs the scan here.
        self._ensure_discovered()
        report = PluginValidationReport(
            discovery_errors=list(self._discovery_errors),
            conflicts=list(self._discovery_conflicts),
        )
        records = self._records
        errors_by_id: dict[str, list[str]] = {plugin_id: [] for plugin_id in records}

        for plugin_id in sorted(records):
            record = records[plugin_id]
            failure = self._manifest_failure(record.manifest)
            if failure:
                errors_by_id[plugin_id].append(failure)
            for dependency, specifier in record.manifest.dependency_specifiers.items():
                dependency_record = records.get(dependency)
                if dependency_record is None:
                    errors_by_id[plugin_id].append(f"missing dependency {dependency!r}")
                    continue
                try:
                    version = Version(dependency_record.manifest.version)
                except InvalidVersion:  # pragma: no cover - validated by manifest
                    errors_by_id[plugin_id].append(
                        f"dependency {dependency!r} has an invalid version"
                    )
                    continue
                if not specifier.contains(version, prereleases=True):
                    errors_by_id[plugin_id].append(
                        f"dependency {dependency!r} requires {specifier}, found "
                        f"{dependency_record.manifest.version}"
                    )

        cycle = self._cycle_nodes(records)
        if cycle:
            message = f"dependency cycle involving: {', '.join(sorted(cycle))}"
            for plugin_id in cycle:
                errors_by_id[plugin_id].append(message)

        owners: dict[str, list[str]] = {}
        for plugin_id in sorted(records):
            for server_name in records[plugin_id].manifest.mcp_servers:
                namespaced = namespaced_mcp_server_name(plugin_id, server_name)
                if not _MCP_NAME_RE.match(namespaced):
                    errors_by_id[plugin_id].append(
                        f"namespaced MCP server name {namespaced!r} is invalid; "
                        "names must be 1..64 characters from [A-Za-z0-9_-]"
                    )
                    continue
                owners.setdefault(namespaced, []).append(plugin_id)
        for namespaced, plugin_ids in sorted(owners.items()):
            if len(plugin_ids) < 2:
                continue
            message = (
                f"MCP server name collision {namespaced!r} from plugins {', '.join(plugin_ids)}"
            )
            report.conflicts.append(message)
            for plugin_id in plugin_ids:
                errors_by_id[plugin_id].append(message)

        module_owners: dict[str, list[str]] = {}
        for plugin_id in sorted(records):
            if records[plugin_id].manifest.permissions.python:
                module_owners.setdefault(module_name_for(plugin_id), []).append(plugin_id)
        for module_name, plugin_ids in sorted(module_owners.items()):
            if len(plugin_ids) < 2:
                continue
            message = (
                f"Python module name collision {module_name!r} from plugins {', '.join(plugin_ids)}"
            )
            report.conflicts.append(message)
            for plugin_id in plugin_ids:
                errors_by_id[plugin_id].append(message)

        report.plugins = [
            PluginValidation(
                plugin_id=plugin_id,
                path=str(records[plugin_id].path),
                errors=errors_by_id[plugin_id],
            )
            for plugin_id in sorted(records)
        ]
        return report

    # -------------------------------------------------------------- loading

    def _resolve(
        self,
        records: Mapping[str, _Record],
        preflight: Mapping[str, str],
    ) -> tuple[list[str], dict[str, str]]:
        enabled = {
            plugin_id for plugin_id, record in records.items() if self._effective_enabled(record)
        }
        failures = {
            plugin_id: reason for plugin_id, reason in preflight.items() if plugin_id in enabled
        }

        changed = True
        while changed:
            changed = False
            for plugin_id in sorted(enabled - failures.keys()):
                manifest = records[plugin_id].manifest
                for dependency, specifier in manifest.dependency_specifiers.items():
                    if dependency not in enabled:
                        reason = (
                            f"missing dependency {dependency!r}"
                            if dependency not in records
                            else f"dependency {dependency!r} is disabled"
                        )
                        failures[plugin_id] = reason
                        changed = True
                        break
                    if dependency in failures:
                        failures[plugin_id] = (
                            f"dependency {dependency!r} failed: {failures[dependency]}"
                        )
                        changed = True
                        break
                    dependency_version = records[dependency].manifest.version
                    if not specifier.contains(Version(dependency_version), prereleases=True):
                        failures[plugin_id] = (
                            f"dependency {dependency!r} requires {specifier}, found "
                            f"{dependency_version}"
                        )
                        changed = True
                        break

        valid = enabled - failures.keys()
        indegree = dict.fromkeys(valid, 0)
        children: dict[str, list[str]] = {plugin_id: [] for plugin_id in valid}
        for plugin_id in valid:
            for dependency in records[plugin_id].manifest.dependency_specifiers:
                if dependency in valid:
                    indegree[plugin_id] += 1
                    children[dependency].append(plugin_id)

        ready = [plugin_id for plugin_id in valid if indegree[plugin_id] == 0]
        heapq.heapify(ready)
        order: list[str] = []
        while ready:
            plugin_id = heapq.heappop(ready)
            order.append(plugin_id)
            for child in children[plugin_id]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    heapq.heappush(ready, child)

        remaining = valid - set(order)
        if remaining:
            cycle = ", ".join(sorted(remaining))
            for plugin_id in sorted(remaining):
                failures[plugin_id] = f"dependency cycle involving: {cycle}"
        return order, failures

    async def load(self, context: PluginContext) -> PluginLoadReport:
        report = PluginLoadReport()
        if not self.config.enabled:
            # If plugins were loaded before the config was toggled, remove
            # their contributions instead of leaving them active.
            await self.unload_all()
            report.disabled = True
            return report

        await self.unload_all()
        self._discover(force=True)
        self._active_context = context
        report.discovery_errors.extend(self._discovery_errors)
        report.conflicts.extend(self._discovery_conflicts)

        records = self._records
        preflight: dict[str, str] = {}
        for plugin_id in sorted(records):
            record = records[plugin_id]
            if not self._effective_enabled(record):
                report.skipped.append(plugin_id)
                record.status = PluginStatus.DISABLED
                continue
            reason = self._manifest_failure(record.manifest)
            if reason:
                preflight[plugin_id] = reason

        order, failures = self._resolve(records, preflight)
        report.order = list(order)
        for plugin_id, reason in sorted(failures.items()):
            record = records[plugin_id]
            record.status = PluginStatus.FAILED
            record.error = reason
            report.failed[plugin_id] = reason
        load_failed = dict(failures)

        for plugin_id in order:
            record = records[plugin_id]
            dependency_failure = next(
                (
                    dependency
                    for dependency in record.manifest.dependency_specifiers
                    if dependency in load_failed
                ),
                None,
            )
            if dependency_failure is not None:
                reason = f"dependency {dependency_failure!r} failed to load"
                record.status = PluginStatus.FAILED
                record.error = reason
                report.failed[plugin_id] = reason
                load_failed[plugin_id] = reason
                continue

            try:
                contributions = await self._stage_and_commit(plugin_id, record, context)
            except _ContributionConflict as exc:
                reason = str(exc)
                record.status = PluginStatus.FAILED
                record.error = reason
                report.failed[plugin_id] = reason
                report.conflicts.append(reason)
                load_failed[plugin_id] = reason
                self._drop_module(record)
            except PluginLoadError as exc:
                reason = str(exc)
                record.status = PluginStatus.FAILED
                record.error = reason
                report.failed[plugin_id] = reason
                load_failed[plugin_id] = reason
                self._drop_module(record)
            except Exception as exc:  # plugin code must not abort other plugins
                reason = f"{type(exc).__name__}: {exc}"
                record.status = PluginStatus.FAILED
                record.error = reason
                report.failed[plugin_id] = reason
                load_failed[plugin_id] = reason
                self._drop_module(record)
            else:
                record.status = PluginStatus.LOADED
                record.error = None
                record.contributions = contributions
                report.loaded.append(plugin_id)
                report.contributions[plugin_id] = contributions

        return report

    async def _stage_and_commit(
        self,
        plugin_id: str,
        record: _Record,
        context: PluginContext,
    ) -> PluginContributions:
        manifest = record.manifest
        source = hook_source(plugin_id)

        # Stage manifest hook commands. Namespace them so a plugin cannot
        # silently replace a user-configured hook.
        hook_commands: list[HookCommandConfig] = []
        seen_hook_ids: set[str] = set()
        for hook in manifest.hooks:
            command_id = namespaced_hook_command_id(plugin_id, hook.id)
            if (
                command_id in seen_hook_ids
                or command_id in context.reserved_hook_ids
                or command_id in self._claimed_hook_ids
            ):
                raise _ContributionConflict(
                    f"hook id conflict for plugin {plugin_id!r}: {command_id!r}"
                )
            seen_hook_ids.add(command_id)
            hook_commands.append(hook.model_copy(update={"id": command_id}))

        # Stage manifest MCP servers. MCP names allow no ':', so use
        # '<plugin>_<server>' and reject the rare cross-plugin collision.
        mcp_servers: dict[str, MCPServerConfig] = {}
        for server_name, server_config in manifest.mcp_servers.items():
            self._stage_mcp_server(
                plugin_id,
                server_name,
                server_config,
                mcp_servers,
                context,
            )

        pending_hooks: list[PendingHook] = []
        tools: list[Tool] = []
        module_name: str | None = None

        if manifest.permissions.python:
            if not self.config.allow_python:
                raise PluginLoadError(
                    f"plugin {plugin_id!r} requires a Python entrypoint but "
                    "plugins.allow_python is false"
                )
            staging = context.for_plugin(plugin_id)
            if module_name_for(plugin_id) in self._claimed_modules:
                raise _ContributionConflict(
                    f"Python module name collision for plugin {plugin_id!r}: "
                    f"{module_name_for(plugin_id)!r} is already loaded"
                )
            try:
                module, module_name = self._import_entrypoint(record)
                record.module_name = module_name
                register = getattr(module, "register", None)
                if not callable(register):
                    raise PluginLoadError(
                        f"plugin {plugin_id!r} entrypoint {manifest.entrypoint!r} "
                        "does not define a callable register(ctx)"
                    )
                await maybe_await(register(staging))
            except BaseException as exc:
                if module_name is not None:
                    self._drop_module(record)
                if isinstance(exc, PluginLoadError):
                    raise
                if isinstance(exc, Exception):
                    raise PluginLoadError(
                        f"plugin {plugin_id!r} failed to initialize: {type(exc).__name__}: {exc}"
                    ) from exc
                raise

            pending_hooks = list(staging.pending_hooks)
            tools = list(staging.pending_tools)

            if pending_hooks and not manifest.permissions.hooks:
                raise PluginLoadError(
                    f"plugin {plugin_id!r} registered hooks but did not declare "
                    "permissions.hooks = true"
                )
            if staging.pending_mcp_servers and not manifest.permissions.mcp:
                raise PluginLoadError(
                    f"plugin {plugin_id!r} added MCP servers but did not declare "
                    "permissions.mcp = true"
                )
            for server_name, server_config in staging.pending_mcp_servers.items():
                self._stage_mcp_server(
                    plugin_id,
                    server_name,
                    server_config,
                    mcp_servers,
                    context,
                )

        seen_tools: set[str] = set()
        for tool in tools:
            name = tool.name
            if name in seen_tools:
                raise _ContributionConflict(
                    f"plugin {plugin_id!r} contributes duplicate tool name {name!r}"
                )
            seen_tools.add(name)
            if (
                name in context.reserved_tools
                or name in self._claimed_tools
                or (context.tool_registry is not None and context.tool_registry.has(name))
            ):
                raise _ContributionConflict(
                    f"tool name conflict for plugin {plugin_id!r}: {name!r} already exists"
                )

        # Build concrete hook registrations before touching any component.
        registrations: list[Any] = []
        if pending_hooks:
            factory = context.hook_registration_factory or default_hook_registration_factory
            for index, pending in enumerate(pending_hooks):
                try:
                    registrations.append(
                        self._build_hook_registration(
                            factory, pending, source, f"{source}:hook:{index}"
                        )
                    )
                except PluginLoadError:
                    raise
                except Exception as exc:
                    raise PluginLoadError(
                        f"plugin {plugin_id!r} could not build a HookRegistration: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc

        applied_tools: list[str] = []
        applied_mcp: list[str] = []
        applied_hook_commands: list[str] = []
        applied_hook_registrations = False
        try:
            if context.register_hook_command is not None:
                for command in hook_commands:
                    await maybe_await(context.register_hook_command(command))
                    applied_hook_commands.append(command.id)
            if context.register_hook is not None:
                for registration in registrations:
                    await maybe_await(context.register_hook(registration))
                    applied_hook_registrations = True
            if context.register_mcp_server is not None:
                for server_name, server_config in mcp_servers.items():
                    await maybe_await(context.register_mcp_server(server_name, server_config))
                    applied_mcp.append(server_name)
            for tool in tools:
                if context.register_tool is not None:
                    await maybe_await(context.register_tool(tool))
                elif context.tool_registry is not None:
                    context.tool_registry.register(tool)
                else:
                    continue
                applied_tools.append(tool.name)
        except BaseException as exc:
            rollback_issues = await self._rollback(
                context,
                source,
                applied_hook_commands,
                applied_hook_registrations,
                applied_tools,
                applied_mcp,
            )
            if not isinstance(exc, Exception):
                raise
            detail = (
                str(exc) if isinstance(exc, PluginLoadError) else f"{type(exc).__name__}: {exc}"
            )
            if rollback_issues:
                detail += "; rollback issues: " + "; ".join(rollback_issues)
            raise PluginLoadError(
                f"failed to apply contributions for plugin {plugin_id!r}: {detail}"
            ) from exc

        self._claimed_hook_ids.update(command.id for command in hook_commands)
        self._claimed_mcp_servers.update(mcp_servers)
        self._claimed_tools.update(tool.name for tool in tools)
        record.applied_hook_commands = tuple(applied_hook_commands)
        record.applied_hook_registrations = applied_hook_registrations
        record.applied_tools = tuple(applied_tools)
        record.applied_mcp_servers = tuple(applied_mcp)
        if module_name is not None:
            record.module_name = module_name
            self._claimed_modules.add(module_name)
        return PluginContributions(
            plugin_id=plugin_id,
            hook_commands=tuple(hook_commands),
            mcp_servers=dict(mcp_servers),
            hook_registrations=tuple(registrations),
            tools=tuple(tools),
            module_name=module_name,
        )

    def _stage_mcp_server(
        self,
        plugin_id: str,
        server_name: str,
        server_config: MCPServerConfig,
        staged: dict[str, MCPServerConfig],
        context: PluginContext,
    ) -> None:
        namespaced = namespaced_mcp_server_name(plugin_id, server_name)
        if not _MCP_NAME_RE.match(namespaced):
            raise PluginLoadError(
                f"plugin {plugin_id!r}: namespaced MCP server name {namespaced!r} "
                "is invalid; names must be 1..64 characters from [A-Za-z0-9_-]"
            )
        if (
            namespaced in staged
            or namespaced in context.reserved_mcp_servers
            or namespaced in self._claimed_mcp_servers
        ):
            raise _ContributionConflict(
                f"MCP server name conflict for plugin {plugin_id!r}: {namespaced!r}"
            )
        if server_config.enabled and not server_config.trusted:
            raise PluginLoadError(
                f"plugin {plugin_id!r}: MCP server {server_name!r} is enabled but not trusted"
            )
        staged[namespaced] = server_config

    def _build_hook_registration(
        self,
        factory: Callable[..., Any],
        pending: PendingHook,
        source: str,
        registration_id: str,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "id": registration_id,
            "handler": pending.handler,
            "events": list(pending.events),
            "priority": pending.priority,
            # HookRegistration uses 0.0 for "manager default"; never pass None
            # to a custom factory that expects a float comparison.
            "timeout_s": pending.timeout_s if pending.timeout_s is not None else 0.0,
            "fail_mode": pending.fail_mode,
            "can_deny": pending.can_deny,
            "can_append_context": pending.can_append_context,
            "source": source,
        }
        try:
            signature = inspect.signature(factory)
        except (TypeError, ValueError):
            return factory(**kwargs)
        parameters = signature.parameters
        if not any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
        ):
            kwargs = {key: value for key, value in kwargs.items() if key in parameters}
        return factory(**kwargs)

    def _import_entrypoint(self, record: _Record) -> tuple[Any, str]:
        manifest = record.manifest
        plugin_dir = record.path.resolve()
        entrypoint = (plugin_dir / manifest.entrypoint).resolve()
        if not entrypoint.is_relative_to(plugin_dir):
            raise PluginLoadError(
                f"plugin {record.plugin_id!r}: entrypoint escapes the plugin directory"
            )
        if not entrypoint.is_file():
            raise PluginLoadError(
                f"plugin {record.plugin_id!r}: entrypoint {entrypoint} does not exist"
            )
        module_name = module_name_for(record.plugin_id)
        sys.modules.pop(module_name, None)
        spec = importlib.util.spec_from_file_location(module_name, entrypoint)
        if spec is None or spec.loader is None:
            raise PluginLoadError(
                f"plugin {record.plugin_id!r}: cannot create an import spec for {entrypoint}"
            )
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException as exc:
            sys.modules.pop(module_name, None)
            raise PluginLoadError(
                f"plugin {record.plugin_id!r}: failed to import {entrypoint}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return module, module_name

    def _drop_module(self, record: _Record) -> None:
        if record.module_name is not None:
            sys.modules.pop(record.module_name, None)
            record.module_name = None

    async def _rollback(
        self,
        context: PluginContext,
        source: str,
        applied_hook_commands: list[str],
        applied_hook_registrations: bool,
        applied_tools: list[str],
        applied_mcp: list[str],
    ) -> list[str]:
        issues: list[str] = []
        for command_id in reversed(applied_hook_commands):
            # HookManager.register_command names the registration
            # "cmd:<command id>" and its source "command:<command id>", so
            # unregister_source(plugin source) would not match it.
            if context.unregister_hook is None:
                issues.append(f"command hook {command_id!r} could not be removed")
                continue
            try:
                await maybe_await(context.unregister_hook(f"cmd:{command_id}"))
            except Exception as exc:
                issues.append(f"command hook {command_id!r} rollback failed: {exc}")
        if applied_hook_registrations:
            if context.unregister_hook_source is None:
                issues.append(f"hooks from {source!r} could not be removed")
            else:
                try:
                    await maybe_await(context.unregister_hook_source(source))
                except Exception as exc:
                    issues.append(f"hook rollback failed: {exc}")
        for name in reversed(applied_tools):
            try:
                if context.unregister_tool is not None:
                    await maybe_await(context.unregister_tool(name))
                elif context.tool_registry is not None:
                    context.tool_registry.unregister(name)
                else:
                    issues.append(f"tool {name!r} could not be removed")
            except Exception as exc:
                issues.append(f"tool {name!r} rollback failed: {exc}")
        for name in reversed(applied_mcp):
            if context.unregister_mcp_server is None:
                issues.append(f"MCP server {name!r} could not be removed")
                continue
            try:
                await maybe_await(context.unregister_mcp_server(name))
            except Exception as exc:
                issues.append(f"MCP server {name!r} rollback failed: {exc}")
        return issues

    # -------------------------------------------------------------- unloading

    async def unload(self, plugin_id: str) -> bool:
        self._ensure_discovered()
        record = self._records.get(plugin_id)
        if record is None:
            raise PluginLoadError(f"unknown plugin {plugin_id!r}")
        has_applied = bool(
            record.applied_hook_commands
            or record.applied_hook_registrations
            or record.applied_tools
            or record.applied_mcp_servers
        )
        if record.status is PluginStatus.UNLOADED or (
            record.status is PluginStatus.FAILED and not has_applied
        ):
            return False
        if record.status not in {PluginStatus.LOADED, PluginStatus.FAILED}:
            return False

        context = self._active_context
        issues: list[str] = []
        if context is None and (
            record.applied_hook_commands
            or record.applied_hook_registrations
            or record.applied_tools
            or record.applied_mcp_servers
        ):
            issues.append("no active plugin context; applied contributions remain")

        if context is not None:
            for command_id in reversed(record.applied_hook_commands):
                if context.unregister_hook is None:
                    issues.append(
                        f"no unregister_hook callback; command hook {command_id!r} "
                        "remains registered"
                    )
                    continue
                try:
                    await maybe_await(context.unregister_hook(f"cmd:{command_id}"))
                except Exception as exc:
                    issues.append(f"command hook {command_id!r} removal failed: {exc}")
            if record.applied_hook_registrations:
                if context.unregister_hook_source is None:
                    issues.append(
                        f"no unregister_hook_source callback; hooks from {plugin_id!r} "
                        "remain registered"
                    )
                else:
                    try:
                        await maybe_await(context.unregister_hook_source(hook_source(plugin_id)))
                    except Exception as exc:
                        issues.append(f"hook removal failed: {exc}")
            for name in reversed(record.applied_tools):
                try:
                    if context.unregister_tool is not None:
                        await maybe_await(context.unregister_tool(name))
                    elif context.tool_registry is not None:
                        context.tool_registry.unregister(name)
                    else:
                        issues.append(f"no tool unregister callback for {name!r}")
                except Exception as exc:
                    issues.append(f"tool {name!r} removal failed: {exc}")
            for name in reversed(record.applied_mcp_servers):
                if context.unregister_mcp_server is None:
                    issues.append(f"no MCP unregister callback for {name!r}")
                    continue
                try:
                    await maybe_await(context.unregister_mcp_server(name))
                except Exception as exc:
                    issues.append(f"MCP server {name!r} removal failed: {exc}")

        if issues:
            # Keep applied/claimed state so a later unload can retry instead of
            # silently leaking hooks or tools.
            record.error = "unload incomplete: " + "; ".join(issues)
            record.status = PluginStatus.FAILED
            raise PluginLoadError(
                f"plugin {plugin_id!r} was not fully unloaded: " + "; ".join(issues)
            )

        contributions = record.contributions
        if contributions is not None:
            self._claimed_hook_ids.difference_update(
                command.id for command in contributions.hook_commands
            )
            self._claimed_mcp_servers.difference_update(contributions.mcp_servers)
            self._claimed_tools.difference_update(tool.name for tool in contributions.tools)
            if contributions.module_name is not None:
                self._claimed_modules.discard(contributions.module_name)
        self._drop_module(record)
        record.contributions = None
        record.applied_hook_commands = ()
        record.applied_hook_registrations = False
        record.applied_tools = ()
        record.applied_mcp_servers = ()
        record.status = PluginStatus.UNLOADED
        record.error = None
        return True

    async def unload_all(self) -> list[str]:
        loaded = [
            plugin_id
            for plugin_id, record in self._records.items()
            if record.status is PluginStatus.LOADED
            or (
                record.status is PluginStatus.FAILED
                and (
                    record.applied_hook_commands
                    or record.applied_hook_registrations
                    or record.applied_tools
                    or record.applied_mcp_servers
                )
            )
        ]
        errors: list[str] = []
        for plugin_id in reversed(loaded):
            try:
                await self.unload(plugin_id)
            except PluginLoadError as exc:
                errors.append(str(exc))
        if errors:
            raise PluginLoadError("; ".join(errors))
        return loaded

    # ---------------------------------------------------------- enable/disable

    def enable(self, plugin_id: str) -> bool:
        self._ensure_discovered()
        record = self._records.get(plugin_id)
        if record is None:
            raise PluginLoadError(
                f"unknown plugin {plugin_id!r}; run plugin discovery/validate first"
            )
        failure = self._manifest_failure(record.manifest)
        if failure:
            raise PluginLoadError(f"cannot enable plugin {plugin_id!r}: {failure}")
        for dependency, specifier in record.manifest.dependency_specifiers.items():
            dependency_record = self._records.get(dependency)
            if dependency_record is None:
                raise PluginLoadError(
                    f"cannot enable plugin {plugin_id!r}: missing dependency {dependency!r}"
                )
            if not self._effective_enabled(dependency_record):
                raise PluginLoadError(
                    f"cannot enable plugin {plugin_id!r}: dependency {dependency!r} is disabled"
                )
            if not specifier.contains(
                Version(dependency_record.manifest.version), prereleases=True
            ):
                raise PluginLoadError(
                    f"cannot enable plugin {plugin_id!r}: dependency {dependency!r} "
                    f"requires {specifier}, found "
                    f"{dependency_record.manifest.version}"
                )
        self._state[plugin_id] = {"enabled": True}
        self._write_state()
        return True

    async def disable(self, plugin_id: str) -> bool:
        self._ensure_discovered()
        record = self._records.get(plugin_id)
        if record is None and plugin_id not in self._state:
            raise PluginLoadError(f"unknown plugin {plugin_id!r}")
        # Persist first: if unloading fails, the plugin must still stay off on
        # the next start.
        self._state[plugin_id] = {"enabled": False}
        self._write_state()
        if record is not None:
            await self.unload(plugin_id)
        return True

    # ----------------------------------------------------------------- queries

    def _info(self, record: _Record) -> PluginInfo:
        manifest = record.manifest
        enabled = self._effective_enabled(record)
        if not enabled:
            status = PluginStatus.DISABLED
        elif record.status in {PluginStatus.LOADED, PluginStatus.FAILED}:
            status = record.status
        elif record.status is PluginStatus.UNLOADED:
            status = PluginStatus.UNLOADED
        elif record.error is not None:
            status = PluginStatus.FAILED
        else:
            status = PluginStatus.DISCOVERED
        return PluginInfo(
            plugin_id=record.plugin_id,
            name=manifest.name,
            version=manifest.version,
            path=str(record.path),
            description=manifest.description,
            author=manifest.author,
            requires_bear=manifest.requires_bear,
            dependencies=list(manifest.dependency_specifiers),
            enabled=enabled,
            status=status,
            error=record.error,
            hooks=[
                namespaced_hook_command_id(record.plugin_id, hook.id) for hook in manifest.hooks
            ],
            mcp_servers=[
                namespaced_mcp_server_name(record.plugin_id, server)
                for server in manifest.mcp_servers
            ],
            python=manifest.permissions.python,
            entrypoint=manifest.entrypoint if manifest.permissions.python else None,
            loaded=record.status is PluginStatus.LOADED,
        )

    def list(self) -> list[PluginInfo]:
        self._ensure_discovered()
        return [self._info(self._records[plugin_id]) for plugin_id in sorted(self._records)]

    def info(self, plugin_id: str) -> PluginInfo | None:
        self._ensure_discovered()
        record = self._records.get(plugin_id)
        return None if record is None else self._info(record)

    def status(self) -> dict[str, Any]:
        self._ensure_discovered()
        loaded = sorted(
            plugin_id
            for plugin_id, record in self._records.items()
            if record.status is PluginStatus.LOADED
        )
        failed = {
            plugin_id: record.error or "failed"
            for plugin_id, record in sorted(self._records.items())
            if record.status is PluginStatus.FAILED
        }
        disabled = sorted(
            plugin_id
            for plugin_id, record in self._records.items()
            if not self._effective_enabled(record)
        )
        return {
            "config_enabled": self.config.enabled,
            "allow_python": self.config.allow_python,
            "hooks_enabled": self.hooks_enabled,
            "hooks_allow_external": self.hooks_allow_external,
            "bear_version": self.bear_version,
            "state_path": str(self.state_path),
            "state_error": self._state_error,
            "loaded": loaded,
            "failed": failed,
            "disabled": disabled,
            "plugins": [info.model_dump(mode="json") for info in self.list()],
            "discovery_errors": list(self._discovery_errors),
            "conflicts": list(self._discovery_conflicts),
        }


__all__ = ["PluginManager"]
