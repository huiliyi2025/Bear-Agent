"""Unified diagnostics.

Checks are split into two explicit modes:

* ``static`` reads configuration, files, SQLite and manifests. It never starts
  a server, executes a hook, calls a model or mutates harness state.
* ``active`` (``bear doctor --active``) performs bounded connectivity checks:
  one small provider request and MCP server startup. It may incur a small
  provider charge and executes configured MCP commands, so it is opt-in.

Every check is isolated: one failing probe becomes a FAIL/WARN row, never an
exception that hides the remaining diagnostics.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import platform
import shutil
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from bear_agent import __version__
from bear_agent.bootstrap import Maintenance, build_maintenance
from bear_agent.config.models import Settings
from bear_agent.core.cancel import CancelToken
from bear_agent.core.messages import SystemMessage, UserMessage
from bear_agent.core.resources import disk_usage, file_size
from bear_agent.diagnostics.models import (
    CheckMode,
    CheckStatus,
    DiagnosticCheck,
    DiagnosticReport,
)
from bear_agent.hooks.manager import HookManager
from bear_agent.mcp.manager import MCPManager
from bear_agent.observability.metrics.query import MetricsService
from bear_agent.plugins.manager import PluginManager
from bear_agent.providers.base import ModelRequest
from bear_agent.providers.router import build_fallback_provider
from bear_agent.runtime.model_caller import ModelCaller
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.secrets.store import secret_store_from_config
from bear_agent.shell.sandbox.base import SandboxManager
from bear_agent.storage.migrations import SCHEMA_VERSION
from bear_agent.tools.registry import ToolRegistry

MIN_FREE_BYTES = 100 * 1024 * 1024


class DiagnosticsService:
    def __init__(
        self,
        settings: Settings,
        *,
        cwd: str | Path,
        config_path: str | Path | None = None,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self.settings = settings
        self.cwd = Path(cwd).resolve()
        self.config_path = str(config_path) if config_path else None
        self.redactor = redactor or SecretRedactor()
        # Doctor can see provider/MCP error bodies; register configured secret
        # values so they are redacted even when they do not match a token shape.
        secret_store = secret_store_from_config(settings.secrets.backend)
        for provider in settings.providers.values():
            try:
                key = provider.resolve_api_key(secret_store)
            except Exception:
                key = ""
            if key:
                self.redactor.register("provider:doctor", key)
            for header in provider.headers.values():
                if header:
                    self.redactor.register("provider:header", header)
        for server in settings.mcp.servers.values():
            for value in server.env.values():
                if value:
                    self.redactor.register("mcp:env", value)
            for value in server.headers.values():
                if value:
                    self.redactor.register("mcp:header", value)

    async def run(
        self,
        *,
        active: bool = False,
        categories: set[str] | None = None,
    ) -> DiagnosticReport:
        checks: list[DiagnosticCheck] = []
        checks.extend(self._static_environment_checks())
        maintenance: Maintenance | None = None
        try:
            maintenance = build_maintenance(self.settings, cwd=self.cwd)
        except Exception as exc:
            checks.append(
                _check(
                    "storage.database",
                    "storage",
                    CheckStatus.FAIL,
                    "database could not be opened",
                    self._safe(exc),
                )
            )
        if maintenance is not None:
            try:
                checks.extend(await self._maintenance_checks(maintenance))
            finally:
                await maintenance.close()
        active_provider = active and (categories is None or "provider" in categories)
        active_mcp = active and (categories is None or "mcp" in categories)
        checks.extend(await self._provider_checks(active_provider))
        checks.extend(await self._mcp_checks(active_mcp))
        checks.extend(self._plugin_checks())
        checks.extend(self._hook_checks())
        checks.extend(self._sandbox_checks())
        checks.extend(self._retention_checks())
        if categories:
            checks = [check for check in checks if check.category in categories]
        return DiagnosticReport(
            app_version=__version__,
            schema_version=SCHEMA_VERSION,
            generated_at=_now(),
            checks=checks,
            config_path=self.config_path,
        )

    # ------------------------------------------------------------ maintenance

    async def _maintenance_checks(self, maintenance: Maintenance) -> list[DiagnosticCheck]:
        checks: list[DiagnosticCheck] = []
        checks.extend(await self._storage_checks(maintenance))
        checks.extend(await self._database_checks(maintenance))
        checks.extend(await self._audit_checks(maintenance))
        checks.extend(await self._checkpoint_checks(maintenance))
        checks.extend(await self._recovery_checks(maintenance))
        checks.extend(await self._observability_checks(maintenance))
        checks.extend(await self._benchmark_checks(maintenance))
        checks.append(await self._secret_store_check())
        return checks

    async def _storage_checks(self, maintenance: Maintenance) -> list[DiagnosticCheck]:
        checks: list[DiagnosticCheck] = []
        data_dir = self.settings.resolved_data_dir()
        try:
            await asyncio.to_thread(_write_probe, data_dir)
            checks.append(
                _check("storage.data-dir", "storage", CheckStatus.PASS, "writable", str(data_dir))
            )
        except OSError as exc:
            checks.append(
                _check("storage.data-dir", "storage", CheckStatus.FAIL, "not writable", str(exc))
            )
        usage = disk_usage(data_dir)
        if usage is None:
            checks.append(
                _check(
                    "storage.disk",
                    "storage",
                    CheckStatus.WARN,
                    "free space unavailable on this platform",
                )
            )
        elif usage["free_bytes"] < MIN_FREE_BYTES:
            checks.append(
                _check(
                    "storage.disk",
                    "storage",
                    CheckStatus.WARN,
                    "low free disk space",
                    f"{usage['free_bytes']} bytes free",
                    metadata=usage,
                )
            )
        else:
            checks.append(
                _check(
                    "storage.disk",
                    "storage",
                    CheckStatus.PASS,
                    "disk space available",
                    f"{usage['free_bytes']} bytes free",
                    metadata=usage,
                )
            )
        artifact_dir = data_dir / "artifacts"
        checks.append(
            _check(
                "storage.artifacts",
                "storage",
                CheckStatus.PASS,
                "artifact directory present"
                if artifact_dir.exists()
                else "artifact directory empty",
                str(artifact_dir),
                metadata={"database_bytes": file_size(maintenance.db.path)},
            )
        )
        return checks

    async def _database_checks(self, maintenance: Maintenance) -> list[DiagnosticCheck]:
        db = maintenance.db
        checks: list[DiagnosticCheck] = []
        try:
            quick = await db.run(
                lambda conn: [str(row[0]) for row in conn.execute("PRAGMA quick_check").fetchall()]
            )
            ok = quick == ["ok"]
            checks.append(
                _check(
                    "database.integrity",
                    "database",
                    CheckStatus.PASS if ok else CheckStatus.FAIL,
                    "quick_check ok" if ok else "quick_check reported problems",
                    "; ".join(quick[:5]),
                )
            )
        except Exception as exc:
            checks.append(
                _check(
                    "database.integrity",
                    "database",
                    CheckStatus.FAIL,
                    "quick_check failed",
                    self._safe(exc),
                )
            )
        try:
            pragmas = await db.run(_pragmas)
            version = int(pragmas["schema_version"])
            state = CheckStatus.PASS if version == SCHEMA_VERSION else CheckStatus.FAIL
            checks.append(
                _check(
                    "database.schema",
                    "database",
                    state,
                    f"schema v{version} (build v{SCHEMA_VERSION})",
                    metadata=pragmas,
                )
            )
            wal = str(pragmas.get("journal_mode", "")).lower() == "wal"
            checks.append(
                _check(
                    "database.sqlite",
                    "database",
                    CheckStatus.PASS if wal else CheckStatus.WARN,
                    f"journal={pragmas.get('journal_mode')} sqlite={sqlite3.sqlite_version}",
                    f"{pragmas.get('database_bytes', 0)} bytes",
                    metadata=pragmas,
                )
            )
            db_bytes = file_size(db.path) or 0
            wal_bytes = file_size(str(db.path) + "-wal") or 0
            checks.append(
                _check(
                    "database.files",
                    "database",
                    CheckStatus.PASS,
                    "database and WAL readable",
                    f"db={db_bytes} wal={wal_bytes}",
                    metadata={"database_bytes": db_bytes, "wal_bytes": wal_bytes},
                )
            )
        except Exception as exc:
            checks.append(
                _check(
                    "database.schema",
                    "database",
                    CheckStatus.FAIL,
                    "schema check failed",
                    self._safe(exc),
                )
            )
        return checks

    async def _audit_checks(self, maintenance: Maintenance) -> list[DiagnosticCheck]:
        if not self.settings.audit.enabled:
            return [
                _check(
                    "audit.chain",
                    "audit",
                    CheckStatus.WARN,
                    "audit disabled by configuration; security actions have no hash chain",
                )
            ]
        try:
            verification = await maintenance.audit.verify()
        except Exception as exc:
            return [
                _check(
                    "audit.chain",
                    "audit",
                    CheckStatus.FAIL,
                    "audit verification failed",
                    self._safe(exc),
                )
            ]
        if verification.ok:
            return [
                _check(
                    "audit.chain",
                    "audit",
                    CheckStatus.PASS,
                    "hash chain verified",
                    f"{verification.records} record(s)",
                )
            ]
        return [
            _check(
                "audit.chain",
                "audit",
                CheckStatus.FAIL,
                "hash chain is broken",
                f"first bad id={verification.first_bad_id}: {verification.reason}",
            )
        ]

    async def _checkpoint_checks(self, maintenance: Maintenance) -> list[DiagnosticCheck]:
        checks: list[DiagnosticCheck] = [
            _check(
                "checkpoint.config",
                "checkpoints",
                CheckStatus.PASS if self.settings.checkpoints.enabled else CheckStatus.WARN,
                f"enabled={self.settings.checkpoints.enabled} "
                f"keep={self.settings.checkpoints.keep} "
                f"every={self.settings.checkpoints.interval_turns}",
            )
        ]
        if not self.settings.checkpoints.enabled:
            return checks
        checked = 0
        invalid: list[str] = []
        try:
            sessions = await maintenance.sessions.list_recent(20)
            for session in sessions:
                records = await maintenance.checkpoints.list(session.id, limit=3)
                for record in records:
                    checked += 1
                    try:
                        await maintenance.checkpoints.validate(record)
                    except Exception as exc:
                        invalid.append(f"{record.id}: {self._safe(exc)}")
        except Exception as exc:
            checks.append(
                _check(
                    "checkpoint.integrity",
                    "checkpoints",
                    CheckStatus.WARN,
                    "could not inspect checkpoints",
                    self._safe(exc),
                )
            )
            return checks
        if invalid:
            checks.append(
                _check(
                    "checkpoint.integrity",
                    "checkpoints",
                    CheckStatus.FAIL,
                    f"{len(invalid)} invalid checkpoint(s)",
                    "; ".join(invalid[:5]),
                )
            )
        else:
            checks.append(
                _check(
                    "checkpoint.integrity",
                    "checkpoints",
                    CheckStatus.PASS,
                    f"{checked} recent checkpoint(s) verified",
                )
            )
        return checks

    async def _recovery_checks(self, maintenance: Maintenance) -> list[DiagnosticCheck]:
        checks: list[DiagnosticCheck] = [
            _check(
                "recovery.config",
                "recovery",
                CheckStatus.PASS if self.settings.recovery.enabled else CheckStatus.WARN,
                f"enabled={self.settings.recovery.enabled} "
                f"kill_orphans={self.settings.recovery.kill_orphan_processes} "
                f"require_audit={self.settings.recovery.require_audit}",
            )
        ]
        try:
            counts = await maintenance.db.run(_recovery_backlog)
        except Exception as exc:
            checks.append(
                _check(
                    "recovery.backlog",
                    "recovery",
                    CheckStatus.WARN,
                    "could not query backlog",
                    self._safe(exc),
                )
            )
            return checks
        backlog = counts["stale_sessions"] + counts["running_attempts"] + counts["unknown_tools"]
        checks.append(
            _check(
                "recovery.backlog",
                "recovery",
                CheckStatus.PASS if backlog == 0 else CheckStatus.WARN,
                "no crashed state detected"
                if backlog == 0
                else "crashed state detected; run `bear sessions recover <id>` before new turns",
                metadata=counts,
            )
        )
        return checks

    async def _observability_checks(self, maintenance: Maintenance) -> list[DiagnosticCheck]:
        checks: list[DiagnosticCheck] = []
        config = self.settings.observability
        checks.append(
            _check(
                "observability.config",
                "observability",
                CheckStatus.PASS if config.enabled else CheckStatus.WARN,
                f"traces={config.traces_enabled} metrics={config.metrics_enabled} "
                f"sample={config.trace_sample_ratio}",
            )
        )
        try:
            stats = await maintenance.tracer.diagnose()
            problem_fields = (
                "incomplete",
                "orphan_spans",
                "stale_running",
                "invalid_attribute_json",
                "spans_missing_end",
            )
            problems = {
                field: int(stats.get(field) or 0)
                for field in problem_fields
                if int(stats.get(field) or 0) > 0
            }
            unavailable = [field for field in problem_fields if int(stats.get(field) or 0) < 0]
            if not config.traces_enabled:
                status = CheckStatus.WARN
                summary = "trace storage disabled; historical traces remain readable"
            elif unavailable:
                status = CheckStatus.WARN
                summary = f"trace integrity queries unavailable: {unavailable}"
            elif problems:
                status = CheckStatus.WARN
                summary = f"{stats['traces']} trace(s), issues={problems}"
            else:
                status = CheckStatus.PASS
                summary = f"{stats['traces']} trace(s), {stats['spans']} span(s)"
            checks.append(
                _check(
                    "observability.traces",
                    "observability",
                    status,
                    summary,
                    metadata=stats,
                )
            )
        except Exception as exc:
            checks.append(
                _check(
                    "observability.traces",
                    "observability",
                    CheckStatus.FAIL,
                    "trace storage query failed",
                    self._safe(exc),
                )
            )
        if not config.enabled or not config.metrics_enabled:
            checks.append(
                _check(
                    "observability.metrics",
                    "observability",
                    CheckStatus.SKIP,
                    "metrics disabled by configuration",
                )
            )
        else:
            try:
                report = await MetricsService(
                    maintenance.db, config=config, settings=self.settings
                ).report(since=_now() - timedelta(days=1))
                checks.append(
                    _check(
                        "observability.metrics",
                        "observability",
                        CheckStatus.PASS,
                        "metrics queries execute",
                        f"runs={report['runs']['total']} tasks={report['tasks']['total']}",
                    )
                )
            except Exception as exc:
                checks.append(
                    _check(
                        "observability.metrics",
                        "observability",
                        CheckStatus.FAIL,
                        "metrics query failed",
                        self._safe(exc),
                    )
                )
        return checks

    async def _benchmark_checks(self, maintenance: Maintenance) -> list[DiagnosticCheck]:
        try:
            count = int(
                (
                    await maintenance.db.run(
                        lambda conn: conn.execute("SELECT COUNT(*) FROM benchmark_runs").fetchone()
                    )
                )[0]
            )
        except Exception as exc:
            return [
                _check(
                    "benchmarks.storage",
                    "benchmarks",
                    CheckStatus.FAIL,
                    "benchmark storage query failed",
                    self._safe(exc),
                )
            ]
        config = self.settings.benchmarks
        return [
            _check(
                "benchmarks.storage",
                "benchmarks",
                CheckStatus.PASS if config.enabled else CheckStatus.WARN,
                f"enabled={config.enabled} runs={count} "
                f"default_iterations={config.default_iterations} "
                f"retention={config.retention_days}d/{config.max_runs} runs",
            )
        ]

    async def _secret_store_check(self) -> DiagnosticCheck:
        try:
            store = secret_store_from_config(self.settings.secrets.backend)
            if store.available():
                return _check(
                    "secrets.backend",
                    "secrets",
                    CheckStatus.PASS,
                    f"{self.settings.secrets.backend} backend available",
                )
            return _check(
                "secrets.backend",
                "secrets",
                CheckStatus.WARN,
                f"{self.settings.secrets.backend} backend unavailable; secret:// references fail closed",
            )
        except Exception as exc:
            return _check(
                "secrets.backend",
                "secrets",
                CheckStatus.FAIL,
                "secret backend check failed",
                self._safe(exc),
            )

    # --------------------------------------------------------------- provider

    async def _provider_checks(self, active: bool) -> list[DiagnosticCheck]:
        checks: list[DiagnosticCheck] = []
        store = secret_store_from_config(self.settings.secrets.backend)
        try:
            self.settings.validate_ready(secret_store=store)
            provider = self.settings.provider()
            key_source = _key_source(provider)
            checks.append(
                _check(
                    "provider.config",
                    "provider",
                    CheckStatus.PASS,
                    f"{self.settings.agent.default_provider} -> {_safe_url(provider.base_url)}",
                    f"model={self.settings.agent.default_model} key={key_source}",
                )
            )
        except Exception as exc:
            checks.append(
                _check(
                    "provider.config",
                    "provider",
                    CheckStatus.FAIL,
                    "provider is not ready",
                    self._safe(exc),
                )
            )
        try:
            router = build_fallback_provider(self.settings, secret_store=store)
            try:
                errors = list(router.configuration_errors)
                status = CheckStatus.PASS if not errors else CheckStatus.WARN
                checks.append(
                    _check(
                        "provider.fallback",
                        "provider",
                        status,
                        f"{len(router.routes)} route(s)"
                        + (f", {len(errors)} skipped" if errors else ""),
                        self._safe("; ".join(str(item.get("error", item)) for item in errors[:3])),
                    )
                )
                if active:
                    checks.append(
                        await _active_provider_check(self.settings, router, self.redactor)
                    )
            finally:
                await router.aclose()
        except Exception as exc:
            checks.append(
                _check(
                    "provider.fallback",
                    "provider",
                    CheckStatus.WARN,
                    "fallback router unavailable",
                    self._safe(exc),
                )
            )
        return checks

    # -------------------------------------------------------------------- MCP

    async def _mcp_checks(self, active: bool) -> list[DiagnosticCheck]:
        if not self.settings.mcp.enabled:
            return [_check("mcp.config", "mcp", CheckStatus.WARN, "MCP disabled")]
        servers = self.settings.mcp.servers
        missing: list[str] = []
        for name, server in servers.items():
            if not server.enabled:
                continue
            if server.trusted is not True:
                missing.append(f"{name}: not trusted")
        for name, server in servers.items():
            if not server.enabled or server.transport != "stdio":
                continue
            command = server.command
            if _stdio_command_missing(command):
                missing.append(f"{name}: {command!r} not found")
        state = CheckStatus.PASS if not missing else CheckStatus.WARN
        checks = [
            _check(
                "mcp.config",
                "mcp",
                state,
                f"{len(servers)} configured, {sum(1 for s in servers.values() if s.enabled)} enabled",
                "; ".join(missing),
                metadata={"servers": sorted(servers)},
            )
        ]
        if active and servers:
            checks.append(await self._active_mcp_check())
        return checks

    async def _active_mcp_check(self) -> DiagnosticCheck:
        """Start configured trusted servers once and report their state.

        This is explicitly active: it executes the configured MCP commands. The
        registry is temporary, so no tool leaks into a later runtime.
        """
        store = secret_store_from_config(self.settings.secrets.backend)
        manager = MCPManager(
            self.settings.mcp,
            ToolRegistry(),
            workspace=self.cwd,
            server_cwd=self.settings.ensure_data_dirs() / "mcp",
            redactor=self.redactor,
            secret_store=store,
        )
        try:
            statuses = await asyncio.wait_for(manager.start(), timeout=30.0)
            ready = [status for status in statuses.values() if status.state == "ready"]
            failed = [status for status in statuses.values() if status.state == "error"]
            return _check(
                "mcp.connectivity",
                "mcp",
                CheckStatus.PASS if not failed else CheckStatus.WARN,
                f"{len(ready)} server(s) ready, {len(failed)} failed",
                self._safe("; ".join(f"{item.name}: {item.last_error}" for item in failed[:3])),
                mode=CheckMode.ACTIVE,
                metadata={
                    "servers": [
                        {
                            "name": item.name,
                            "state": item.state,
                            "tools": len(item.tools),
                            "error": self._safe(item.last_error or ""),
                        }
                        for item in statuses.values()
                    ]
                },
            )
        except Exception as exc:
            return _check(
                "mcp.connectivity",
                "mcp",
                CheckStatus.FAIL,
                "MCP startup failed",
                self._safe(f"{type(exc).__name__}: {exc}"),
                mode=CheckMode.ACTIVE,
            )
        finally:
            with contextlib.suppress(Exception):
                await manager.stop()

    # ----------------------------------------------------------------- plugins

    def _plugin_checks(self) -> list[DiagnosticCheck]:
        if not self.settings.plugins.enabled:
            return [_check("plugins.validate", "plugins", CheckStatus.WARN, "plugins disabled")]
        try:
            manager = PluginManager(
                self.settings.plugins,
                data_dir=self.settings.ensure_data_dirs(),
                hooks_allow_external=self.settings.hooks.allow_external,
                hooks_enabled=self.settings.hooks.enabled,
            )
            report = manager.validate_all()
            valid = sum(1 for item in report.plugins if item.ok)
            if report.ok:
                return [
                    _check(
                        "plugins.validate",
                        "plugins",
                        CheckStatus.PASS,
                        f"{len(report.plugins)} plugin(s) discovered, {valid} valid",
                    )
                ]
            failed = [
                f"{item.plugin_id}: {'; '.join(item.errors)}"
                for item in report.plugins
                if not item.ok
            ]
            return [
                _check(
                    "plugins.validate",
                    "plugins",
                    CheckStatus.FAIL,
                    f"{valid}/{len(report.plugins)} plugin(s) valid",
                    "; ".join(failed[:5]),
                )
            ]
        except Exception as exc:
            return [
                _check(
                    "plugins.validate",
                    "plugins",
                    CheckStatus.FAIL,
                    "plugin validation failed",
                    self._safe(exc),
                )
            ]

    # ------------------------------------------------------------------- hooks

    def _hook_checks(self) -> list[DiagnosticCheck]:
        checks: list[DiagnosticCheck] = []
        try:
            manager = HookManager(
                config=self.settings.hooks,
                workspace=self.cwd,
            )
            status = manager.status()
        except Exception as exc:
            return [
                _check(
                    "hooks.config",
                    "hooks",
                    CheckStatus.FAIL,
                    "hook configuration rejected",
                    self._safe(exc),
                )
            ]
        checks.append(
            _check(
                "hooks.config",
                "hooks",
                CheckStatus.PASS if self.settings.hooks.enabled else CheckStatus.WARN,
                f"{len(status)} hook(s) registered, "
                f"external={'on' if self.settings.hooks.allow_external else 'off'}",
                metadata={"hooks": [item.get("id") for item in status]},
            )
        )
        if self.settings.hooks.commands and not self.settings.hooks.allow_external:
            checks.append(
                _check(
                    "hooks.external",
                    "hooks",
                    CheckStatus.WARN,
                    "command hooks are configured but allow_external=false",
                )
            )
        elif self.settings.hooks.allow_external:
            missing = [
                command.id
                for command in self.settings.hooks.commands
                if command.enabled
                and not Path(command.command).is_absolute()
                and shutil.which(command.command) is None
            ]
            checks.append(
                _check(
                    "hooks.external",
                    "hooks",
                    CheckStatus.PASS if not missing else CheckStatus.WARN,
                    "external hooks enabled"
                    if not missing
                    else f"command(s) not found on PATH: {', '.join(missing)}",
                )
            )
        return checks

    # ----------------------------------------------------------------- sandbox

    def _sandbox_checks(self) -> list[DiagnosticCheck]:
        checks: list[DiagnosticCheck] = []
        sandbox = SandboxManager(self.settings.sandbox, self.cwd)
        description = sandbox.describe()
        try:
            sandbox.ensure_available()
        except Exception as exc:
            state = (
                CheckStatus.WARN if self.settings.sandbox.mode == "require" else CheckStatus.FAIL
            )
            checks.append(
                _check(
                    "sandbox.capability",
                    "sandbox",
                    state,
                    "OS sandbox unavailable; shell is fail-closed"
                    if self.settings.sandbox.mode == "require"
                    else "sandbox initialization failed",
                    self._safe(exc),
                    metadata=description,
                )
            )
        else:
            mode = self.settings.sandbox.mode
            if mode == "off":
                checks.append(
                    _check(
                        "sandbox.capability",
                        "sandbox",
                        CheckStatus.WARN,
                        "sandbox explicitly disabled; shell is NOT isolated",
                        metadata=description,
                    )
                )
            elif mode == "process":
                checks.append(
                    _check(
                        "sandbox.capability",
                        "sandbox",
                        CheckStatus.WARN,
                        "process-only containment; no filesystem isolation",
                        metadata=description,
                    )
                )
            else:
                checks.append(
                    _check(
                        "sandbox.capability",
                        "sandbox",
                        CheckStatus.PASS,
                        f"{description.get('backend')} available",
                        metadata=description,
                    )
                )
        checks.append(
            _check(
                "shell.config",
                "shell",
                CheckStatus.PASS if self.settings.shell.enabled else CheckStatus.WARN,
                f"enabled={self.settings.shell.enabled} "
                f"timeout={self.settings.shell.default_timeout_s:g}s",
            )
        )
        return checks

    # -------------------------------------------------------------- retention

    def _retention_checks(self) -> list[DiagnosticCheck]:
        config = self.settings.observability
        checks = [
            _check(
                "observability.retention",
                "observability",
                CheckStatus.PASS,
                f"traces retained {config.trace_retention_days} day(s), "
                f"max {config.trace_max_traces} trace(s)",
            )
        ]
        checks.append(
            _check(
                "checkpoint.retention",
                "checkpoints",
                CheckStatus.PASS,
                f"keep={self.settings.checkpoints.keep} per session",
            )
        )
        unknown_prices = [
            key
            for key in config.pricing
            if key != "*"
            and not key.endswith(":*")
            and ":" in key
            and key.split(":", 1)[0] not in self.settings.providers
        ]
        checks.append(
            _check(
                "observability.pricing",
                "observability",
                CheckStatus.PASS if not unknown_prices else CheckStatus.WARN,
                f"{len(config.pricing)} price entr(ies)"
                + (
                    f"; unknown provider keys: {', '.join(unknown_prices[:5])}"
                    if unknown_prices
                    else ""
                ),
            )
        )
        return checks

    # ----------------------------------------------------------------- helpers

    def _static_environment_checks(self) -> list[DiagnosticCheck]:
        checks: list[DiagnosticCheck] = [
            _check(
                "config.version",
                "config",
                CheckStatus.PASS if self.settings.config_version == 1 else CheckStatus.FAIL,
                f"config_version={self.settings.config_version}",
            ),
            _check(
                "environment.python",
                "environment",
                CheckStatus.PASS if sys.version_info >= (3, 13) else CheckStatus.FAIL,
                f"Python {platform.python_version()}",
            ),
            _check(
                "environment.platform",
                "environment",
                CheckStatus.PASS,
                f"{platform.system()} {platform.release()} ({sys.platform})",
            ),
            _check(
                "environment.dependencies",
                "environment",
                CheckStatus.FAIL
                if "=missing" in (dependency_versions := _dependency_versions())
                else CheckStatus.PASS,
                "runtime dependencies importable"
                if "=missing" not in dependency_versions
                else "one or more runtime dependencies are missing",
                dependency_versions,
            ),
        ]
        return checks

    def _safe(self, value: object) -> str:
        return self.redactor.redact(str(value))[:1_000]


def _safe_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except ValueError:
        return "<invalid-url>"


def _stdio_command_missing(command: str) -> bool:
    path = Path(command)
    if path.is_absolute():
        return not path.exists()
    return shutil.which(command) is None


def _write_probe(data_dir: Path) -> None:
    probe = data_dir / ".doctor-write-probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()


def _check(
    check_id: str,
    category: str,
    status: CheckStatus,
    summary: str,
    detail: str = "",
    *,
    mode: CheckMode = CheckMode.STATIC,
    metadata: dict[str, Any] | None = None,
) -> DiagnosticCheck:
    # Metadata is diagnostic detail; never let a provider error payload carry a
    # secret into JSON/export. The check messages are redacted before this call
    # where they contain free-form errors.
    return DiagnosticCheck(
        id=check_id,
        category=category,
        status=status,
        summary=summary,
        detail=detail,
        mode=mode,
        metadata=metadata or {},
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _pragmas(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    return {
        "schema_version": int(row[0]) if row else 0,
        "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]),
        "page_count": page_count,
        "page_size": page_size,
        "database_bytes": page_count * page_size,
    }


def _recovery_backlog(conn: sqlite3.Connection) -> dict[str, int]:
    now = _now().isoformat()
    stale_sessions = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT t.session_id)
              FROM tool_calls t
              JOIN sessions s ON s.id = t.session_id
             WHERE t.status = 'running'
               AND (s.lease_owner IS NULL OR s.lease_expires_at IS NULL
                    OR s.lease_expires_at <= ?)
            """,
            (now,),
        ).fetchone()[0]
    )
    running_attempts = int(
        conn.execute(
            """
            SELECT COUNT(*)
              FROM task_attempts a
              JOIN sessions s ON s.id = a.session_id
             WHERE a.status = 'running'
               AND (s.lease_owner IS NULL OR s.lease_expires_at IS NULL
                    OR s.lease_expires_at <= ?)
            """,
            (now,),
        ).fetchone()[0]
    )
    unknown_tools = int(
        conn.execute("SELECT COUNT(*) FROM tool_calls WHERE status = 'unknown'").fetchone()[0]
    )
    return {
        "stale_sessions": stale_sessions,
        "running_attempts": running_attempts,
        "unknown_tools": unknown_tools,
    }


def _key_source(provider: Any) -> str:
    if provider.api_key:
        if provider.api_key.startswith(("secret://", "keyring://")):
            return "secret-store reference"
        return "configured value"
    if provider.api_key_env:
        return f"env:{provider.api_key_env}"
    if provider.is_local:
        return "local endpoint (no key required)"
    return "missing"


def _dependency_versions() -> str:
    names = ("pydantic", "httpx", "typer", "rich", "mcp", "jsonschema", "packaging", "keyring")
    parts: list[str] = []
    for name in names:
        try:
            parts.append(f"{name}={importlib.metadata.version(name)}")
        except importlib.metadata.PackageNotFoundError:
            parts.append(f"{name}=missing")
    return ", ".join(parts)


async def _active_provider_check(
    settings: Settings, router: Any, redactor: SecretRedactor
) -> DiagnosticCheck:
    """Run one tiny non-stream request; callers must have opted into --active."""
    try:
        caller = ModelCaller(router)
        response = await asyncio.wait_for(
            caller.call(
                ModelRequest(
                    model=settings.agent.default_model,
                    messages=[
                        SystemMessage.of("You are a health check."),
                        UserMessage.of("Reply with: pong"),
                    ],
                    tools=[],
                    temperature=0.0,
                    max_output_tokens=8,
                    stream=False,
                ),
                CancelToken(),
            ),
            timeout=30.0,
        )
        route = router.last_route
        target = f"{route[0]}/{route[1]}" if route else "unknown"
        text = response.text[:40]
        usage = response.usage
    except Exception as exc:
        return _check(
            "provider.connectivity",
            "provider",
            CheckStatus.FAIL,
            "provider request failed",
            redactor.redact(f"{type(exc).__name__}: {exc}")[:1_000],
            mode=CheckMode.ACTIVE,
        )
    return _check(
        "provider.connectivity",
        "provider",
        CheckStatus.PASS,
        f"provider responded via {target}",
        f"reply={redactor.redact(text[:40])!r} usage={usage.total_tokens} token(s); "
        "this active check may be billed",
        mode=CheckMode.ACTIVE,
    )
