"""Structured diagnostics: statuses, JSON, redaction and category filters."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bear_agent.diagnostics import CheckStatus, DiagnosticsService


@pytest.mark.asyncio
async def test_static_diagnostics_report(workspace: Path, settings) -> None:
    service = DiagnosticsService(settings, cwd=workspace, config_path=workspace / "config.toml")
    report = await service.run()
    ids = {check.id for check in report.checks}
    assert {
        "config.version",
        "environment.python",
        "storage.data-dir",
        "database.integrity",
        "database.schema",
        "audit.chain",
        "provider.config",
        "provider.fallback",
        "mcp.config",
        "plugins.validate",
        "hooks.config",
        "sandbox.capability",
        "observability.traces",
        "observability.metrics",
        "benchmarks.storage",
        "observability.retention",
    } <= ids
    assert report.ok, [check for check in report.checks if check.status is CheckStatus.FAIL]
    assert report.counts["pass"] > 0
    assert report.exit_code == 0
    serialized = json.dumps(report.to_dict())
    assert "api_key" not in serialized
    assert "test-secret" not in serialized


@pytest.mark.asyncio
async def test_provider_missing_key_fails(workspace: Path, settings) -> None:
    settings.providers["fake"].api_key = None
    settings.providers["fake"].api_key_env = None
    settings.providers["fake"].base_url = "https://api.example.invalid/v1"
    report = await DiagnosticsService(settings, cwd=workspace).run()
    check = next(item for item in report.checks if item.id == "provider.config")
    assert check.status is CheckStatus.FAIL
    assert report.ok is False
    assert report.exit_code == 1


@pytest.mark.asyncio
async def test_active_checks_are_opt_in(workspace: Path, settings) -> None:
    service = DiagnosticsService(settings, cwd=workspace)
    static = await service.run(active=False)
    assert not any(check.mode.value == "active" for check in static.checks)
    # No MCP servers are configured, so an active run must not spawn anything;
    # point the fake provider at a closed local port so the probe fails fast.
    settings.providers["fake"].base_url = "http://127.0.0.1:1/v1"
    active = await service.run(active=True)
    active_checks = [check for check in active.checks if check.mode.value == "active"]
    assert active_checks
    assert all(check.id.startswith(("provider.", "mcp.")) for check in active_checks)


@pytest.mark.asyncio
async def test_category_filter(workspace: Path, settings) -> None:
    report = await DiagnosticsService(settings, cwd=workspace).run(
        categories={"provider", "observability"}
    )
    assert report.checks
    assert {check.category for check in report.checks} <= {"provider", "observability"}


@pytest.mark.asyncio
async def test_audit_tampering_is_reported(workspace: Path, settings) -> None:
    from bear_agent.audit.service import AuditService
    from bear_agent.storage.database import Database

    db = Database(settings.ensure_data_dirs() / "bear.db")
    try:
        audit = AuditService(db)
        await audit.append(actor="agent", action="test", resource="r", detail={"x": 1})
        db.connection.execute("UPDATE audit_log SET detail_json = '{\"tampered\": true}'")
        db.connection.commit()
    finally:
        db.close()
    report = await DiagnosticsService(settings, cwd=workspace).run()
    check = next(item for item in report.checks if item.id == "audit.chain")
    assert check.status is CheckStatus.FAIL
    assert "hash" in check.summary.lower() or "broken" in check.summary.lower()


@pytest.mark.asyncio
async def test_doctor_redacts_url_credentials_and_active_scope(workspace: Path, settings) -> None:
    settings.providers["fake"].base_url = "https://user:super-secret-password@example.invalid/v1"
    settings.providers["fake"].api_key = "plain-secret-value-123"
    report = await DiagnosticsService(settings, cwd=workspace).run(categories={"observability"})
    # Category-scoped runs must not execute active probes (none are requested
    # here), and the report must not expose the credential.
    serialized = __import__("json").dumps(report.to_dict())
    assert "super-secret-password" not in serialized
    assert "plain-secret-value-123" not in serialized
    full = await DiagnosticsService(settings, cwd=workspace).run()
    serialized = __import__("json").dumps(full.to_dict())
    assert "user:super-secret-password" not in serialized
    assert "plain-secret-value-123" not in serialized
