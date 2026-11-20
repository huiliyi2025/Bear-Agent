"""Metric primitives and durable aggregation over authoritative facts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bear_agent.config.models import ModelPrice, ObservabilityConfig
from bear_agent.core.clock import FakeClock
from bear_agent.core.events import Event, EventType
from bear_agent.observability.bus import EventBus
from bear_agent.observability.metrics import (
    Distribution,
    MetricsRegistry,
    MetricsService,
    percentile,
)
from bear_agent.observability.metrics.live import LiveMetricsCollector
from bear_agent.sessions.service import SessionService
from bear_agent.storage.database import Database


def test_percentile_and_distribution() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(values, 0.5) == 30.0
    assert percentile(values, 0.0) == 10.0
    assert percentile(values, 1.0) == 50.0
    assert percentile([], 0.95) == 0.0
    summary = Distribution.of(values)
    assert summary.count == 5
    assert summary.mean == 30.0
    assert summary.min == 10.0
    assert summary.max == 50.0
    assert summary.p95 == 48.0
    assert summary.to_dict()["p99"] == 49.6


@pytest.mark.asyncio
async def test_live_metrics_observe_events_without_ids() -> None:
    registry = MetricsRegistry()
    collector = LiveMetricsCollector(registry)
    bus = EventBus()
    bus.subscribe(collector.on_event)
    await bus.publish(
        Event(
            session_id="session-secret",
            run_id="run-secret",
            type=EventType.RUN_STARTED,
            payload={"provider": "p", "model": "m"},
        )
    )
    await bus.publish(
        Event(
            session_id="session-secret",
            run_id="run-secret",
            type=EventType.MODEL_RESPONSE,
            payload={
                "provider": "p",
                "model": "m",
                "latency_ms": 25,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )
    )
    await bus.publish(
        Event(
            session_id="session-secret",
            run_id="run-secret",
            type=EventType.TOOL_CALL_COMPLETED,
            payload={
                "tool_call_id": "call-secret",
                "name": "read_file",
                "status": "completed",
                "duration_ms": 4,
            },
        )
    )
    await bus.publish(
        Event(
            session_id="session-secret",
            run_id="run-secret",
            type=EventType.RUN_COMPLETED,
            payload={"status": "completed"},
        )
    )
    snapshot = collector.snapshot()
    assert snapshot["bear.runs"]["series"][0]["value"] == 1
    assert snapshot["bear.model.calls"]["series"][0]["value"] == 1
    assert snapshot["bear.tool.calls"]["series"][0]["value"] == 1
    assert snapshot["bear.active_runs"]["series"][0]["value"] == 0
    assert "run-secret" not in json.dumps(snapshot)
    assert "session-secret" not in json.dumps(snapshot)
    assert "call-secret" not in json.dumps(snapshot)


def test_registry_counter_gauge_histogram_and_overflow() -> None:
    registry = MetricsRegistry(max_series_per_metric=2)
    registry.counter("runs", {"status": "ok"}, 2)
    registry.counter("runs", {"status": "ok"})
    assert registry.value("runs", {"status": "ok"}) == 3

    registry.gauge("active", 4.0)
    assert registry.value("active") == 4.0

    registry.observe("latency", 12.0)
    registry.observe("latency", 250.0)
    snapshot = registry.snapshot()
    histogram = snapshot["latency"]["series"][0]["histogram"]
    assert histogram["count"] == 2
    assert histogram["p50"] >= 12
    assert histogram["p95"] >= 200

    # A third, fourth and fifth label value collapse into one overflow series.
    registry.counter("tools", {"tool": "a"})
    registry.counter("tools", {"tool": "b"})
    registry.counter("tools", {"tool": "c"})
    registry.counter("tools", {"tool": "d"})
    series = registry.snapshot()["tools"]["series"]
    assert len(series) == 3
    overflow = next(item for item in series if item["labels"].get("_overflow") == "true")
    assert overflow["value"] == 2.0

    with pytest.raises(ValueError):
        registry.counter("bad", {"tool": "a"}, -1)


async def _seed(db: Database) -> tuple[str, str]:
    clock = FakeClock(datetime.now(UTC) - timedelta(minutes=10))
    sessions = SessionService(db, clock)
    first = await sessions.create(cwd=Path.cwd(), model="m", provider="p")
    second = await sessions.create(cwd=Path.cwd(), model="m", provider="p")
    await sessions.append_event(first.id, EventType.RUN_STARTED, run_id="run-1")
    clock.advance(2.0)
    await sessions.append_event(
        first.id,
        EventType.RUN_COMPLETED,
        run_id="run-1",
        payload={"status": "completed"},
    )
    await sessions.append_event(first.id, EventType.RUN_STARTED, run_id="run-2")
    clock.advance(1.0)
    await sessions.append_event(
        first.id,
        EventType.RUN_FAILED,
        run_id="run-2",
        payload={"status": "failed"},
    )
    for event_type, payload in (
        (EventType.MODEL_RETRY, {"provider": "p", "model": "m"}),
        (EventType.PROVIDER_RETRY, {"provider": "p", "model": "m", "category": "timeout"}),
        (EventType.PROVIDER_FALLBACK, {"from": "p:m", "to": "q:m", "category": "rate_limit"}),
        (EventType.PROVIDER_CIRCUIT_OPEN, {"provider": "p", "model": "m"}),
        (EventType.POLICY_DECISION, {"decision": "deny", "tool": "bash"}),
        (EventType.POLICY_DECISION, {"decision": "ask", "tool": "bash"}),
        (EventType.SANDBOX_DECISION, {"backend": "test", "filesystem_isolated": True}),
        (
            EventType.HOOK_COMPLETED,
            {"hooks": [{"id": "h1", "status": "ok", "duration_ms": 4}]},
        ),
        (EventType.HOOK_DENIED, {"hooks": [{"id": "h2", "status": "denied", "duration_ms": 3}]}),
        (EventType.RECOVERY_STARTED, {"running_tools": 1}),
        (EventType.RECOVERY_COMPLETED, {"running_tools": 1}),
        (EventType.RECOVERY_TOOL_RECONCILED, {"tool_call_id": "c1"}),
        (EventType.CHECKPOINT_CREATED, {"kind": "turn"}),
        (EventType.SESSION_FORKED, {"source_session_id": first.id}),
        (EventType.CLAIM_CONFLICT, {"path": "x"}),
    ):
        await sessions.append_event(first.id, event_type, payload=payload)
    await sessions.append_event(
        first.id, EventType.APPROVAL_REQUESTED, payload={"tool_call_id": "approval-1"}
    )
    clock.advance(0.5)
    await sessions.append_event(
        first.id,
        EventType.APPROVAL_DECIDED,
        payload={"tool_call_id": "approval-1", "approved": True},
    )

    now = datetime.now(UTC)
    conn = db.connection
    conn.execute(
        "INSERT INTO model_calls (id, session_id, turn_id, provider, model, "
        "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
        "latency_ms, status, created_at) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "mc1",
            first.id,
            "p",
            "m",
            1_000_000,
            500_000,
            100_000,
            0,
            100,
            "completed",
            now.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO model_calls (id, session_id, turn_id, provider, model, "
        "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
        "latency_ms, status, created_at) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("mc2", first.id, "p", "m", 0, 0, 0, 0, 0, "completed", now.isoformat()),
    )
    conn.execute(
        "INSERT INTO model_calls (id, session_id, turn_id, provider, model, "
        "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
        "latency_ms, status, created_at) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("mc3", first.id, "q", "other", 10, 5, 0, 0, 50, "failed", now.isoformat()),
    )
    conn.execute(
        "INSERT INTO model_calls (id, session_id, turn_id, provider, model, "
        "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
        "latency_ms, status, created_at) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("mc4", second.id, "p", "m", 1000, 1000, 0, 0, 30, "completed", now.isoformat()),
    )
    conn.execute(
        "INSERT INTO tool_calls (id, session_id, turn_id, name, args_json, status, "
        "idempotent, started_at, finished_at, result_summary, error_kind, error_message) "
        "VALUES (?, ?, NULL, ?, '{}', 'completed', 1, ?, ?, 'ok', NULL, NULL)",
        (
            "tc1",
            first.id,
            "read_file",
            (now - timedelta(milliseconds=120)).isoformat(),
            now.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO tool_calls (id, session_id, turn_id, name, args_json, status, "
        "idempotent, started_at, finished_at, result_summary, error_kind, error_message) "
        "VALUES (?, ?, NULL, ?, '{}', 'failed', 0, ?, ?, 'timeout', 'timeout', 'slow')",
        ("tc2", first.id, "bash", (now - timedelta(milliseconds=300)).isoformat(), now.isoformat()),
    )
    conn.execute(
        "INSERT INTO tool_calls (id, session_id, turn_id, name, args_json, status, "
        "idempotent, started_at, finished_at, result_summary, error_kind, error_message) "
        "VALUES (?, ?, NULL, ?, '{}', 'completed', 0, ?, ?, 'ok', NULL, NULL)",
        (
            "tc3",
            first.id,
            "mcp__srv__echo",
            (now - timedelta(milliseconds=50)).isoformat(),
            now.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO tool_calls (id, session_id, turn_id, name, args_json, status, "
        "idempotent, started_at, finished_at, result_summary, error_kind, error_message) "
        "VALUES (?, ?, NULL, ?, '{}', 'unknown', 0, ?, NULL, NULL, NULL, NULL)",
        ("tc4", first.id, "write_file", now.isoformat()),
    )
    conn.execute(
        "INSERT INTO plans (id, session_id, goal, status, version, created_at, updated_at) "
        "VALUES ('plan-1', ?, 'goal', 'active', 1, ?, ?)",
        (first.id, now.isoformat(), now.isoformat()),
    )
    for index, status in enumerate(("completed", "failed", "blocked", "cancelled", "pending")):
        conn.execute(
            "INSERT INTO tasks (id, plan_id, session_id, task_key, title, description, "
            "status, priority, order_index, attempt_count, max_attempts, verification, "
            "active, created_at, updated_at, started_at, finished_at) "
            "VALUES (?, 'plan-1', ?, ?, ?, 'd', ?, 0, ?, ?, 3, 'evidence', 1, ?, ?, ?, ?)",
            (
                f"task-{index}",
                first.id,
                f"T{index}",
                f"task {index}",
                status,
                index,
                index,
                now.isoformat(),
                now.isoformat(),
                (now - timedelta(seconds=1)).isoformat() if status != "pending" else None,
                now.isoformat() if status != "pending" else None,
            ),
        )
    for version in (1, 2):
        conn.execute(
            "INSERT INTO plan_revisions (id, plan_id, session_id, version, goal, reason, source, "
            "snapshot_json, created_at) VALUES (?, 'plan-1', ?, ?, 'goal', 'r', 'model', '{}', ?)",
            (f"rev-{version}", first.id, version, now.isoformat()),
        )
    conn.execute(
        "INSERT INTO compactions (id, session_id, level, covered_from_message_seq, "
        "covered_to_message_seq, summary_json, artifact_id, before_tokens, after_tokens, created_at) "
        "VALUES ('comp-1', ?, 1, 0, 3, '{}', 'art-1', 1000, 200, ?)",
        (first.id, now.isoformat()),
    )
    conn.execute(
        "INSERT INTO compactions (id, session_id, level, covered_from_message_seq, "
        "covered_to_message_seq, summary_json, artifact_id, before_tokens, after_tokens, created_at) "
        "VALUES ('comp-2', ?, 2, 0, 4, '{}', NULL, 500, 100, ?)",
        (first.id, now.isoformat()),
    )
    conn.execute(
        "INSERT INTO context_anchors (id, session_id, kind, text, salience, active, created_at, updated_at) "
        "VALUES ('anchor-1', ?, 'goal', 'goal', 1.0, 1, ?, ?)",
        (first.id, now.isoformat(), now.isoformat()),
    )
    conn.execute(
        "INSERT INTO checkpoints (id, session_id, run_id, kind, event_seq, message_seq, "
        "state_json, checksum, schema_version, created_at) "
        "VALUES ('ckpt-1', ?, NULL, 'turn', 0, 0, '{}', 'x', 1, ?)",
        (first.id, now.isoformat()),
    )
    return first.id, second.id


@pytest.mark.asyncio
async def test_metrics_report_aggregates_facts_and_cost(tmp_path: Path) -> None:
    db = Database(tmp_path / "metrics.db")
    try:
        first, _second = await _seed(db)
        config = ObservabilityConfig(
            pricing={"p:m": ModelPrice(input_per_million=1.0, output_per_million=2.0)}
        )
        service = MetricsService(db, config=config)
        report = await service.report(since=datetime.now(UTC) - timedelta(hours=1))

        runs = report["runs"]
        assert runs["total"] == 2
        assert runs["completed"] == 1
        assert runs["failed"] == 1
        assert runs["success_rate"] == 0.5
        assert runs["duration_ms"]["mean"] > 0

        models = report["models"]
        assert models["calls"] == 4
        assert models["failures"] == 1
        assert models["input_tokens"] == 1_001_010
        assert models["output_tokens"] == 501_005
        assert models["cache_read_tokens"] == 100_000
        assert models["usage_missing"] == 1
        assert models["retries"] == 2
        assert models["fallbacks"] == 1
        assert models["circuit_opens"] == 1
        assert models["provider_failures"] == 0
        assert models["pricing_missing"] == 1
        # uncached input: 900k * 1/M + cache-read 100k * 0 + output 500k * 2/M
        assert models["cost_by_currency"]["USD"] == pytest.approx(1.903)

        tools = report["tools"]
        assert tools["calls"] == 4
        assert tools["completed"] == 2
        assert tools["failed"] == 1
        assert tools["unknown"] == 1
        assert tools["timeouts"] == 1
        assert tools["permission"] == {"deny": 1, "ask": 1, "allow": 0}
        assert tools["approvals"]["approved"] == 1
        assert tools["mcp"]["calls"] == 1
        assert tools["mcp"]["by_server"] == {"srv": 1}
        assert tools["hooks"]["status_counts"] == {"ok": 1, "denied": 1}

        tasks = report["tasks"]
        assert tasks["total"] == 5
        assert tasks["completed"] == 1
        assert tasks["failed"] == 1
        assert tasks["blocked"] == 1
        assert tasks["retries"] == 6
        assert tasks["plan_revisions"] == 1

        context = report["context"]
        assert context["compactions"] == 2
        assert context["levels"] == {"1": 1, "2": 1}
        assert context["saved_tokens"] == 1_200
        assert context["artifacts"] == 1
        assert context["anchors"]["active"] == 1

        recovery = report["recovery"]
        assert recovery["started"] == 1
        assert recovery["completed"] == 1
        assert recovery["tools_reconciled"] == 1
        assert recovery["checkpoints_created"] == 1
        assert recovery["forks"] == 1
        assert recovery["claim_conflicts"] == 1
        assert recovery["unknown_tool_calls"] == 1

        system = report["system"]
        assert system["sessions"]["active"] == 2
        assert system["benchmarks"] == 0

        filtered = await service.report(
            since=datetime.now(UTC) - timedelta(hours=1), session_id=first
        )
        assert filtered["models"]["calls"] == 3
        assert filtered["tools"]["calls"] == 4

        future = await service.report(
            since=datetime.now(UTC) + timedelta(days=1),
            until=datetime.now(UTC) + timedelta(days=2),
        )
        assert future["runs"]["total"] == 0
        assert future["models"]["calls"] == 0

        serialized = json.dumps(report)
        assert "sk-" not in serialized
        assert report["notes"]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_metrics_missing_pricing_is_explicit(tmp_path: Path) -> None:
    db = Database(tmp_path / "metrics2.db")
    try:
        await _seed(db)
        service = MetricsService(db, config=ObservabilityConfig())
        report = await service.report(since=datetime.now(UTC) - timedelta(hours=1))
        assert report["models"]["pricing_missing"] == 3
        assert report["models"]["cost_by_currency"] == {}
    finally:
        db.close()


@pytest.mark.asyncio
async def test_metrics_disabled_config_guard(tmp_path: Path) -> None:
    from bear_agent.core.errors import ConfigError

    db = Database(tmp_path / "disabled.db")
    try:
        service = MetricsService(
            db,
            config=ObservabilityConfig(enabled=False),
        )
        with pytest.raises(ConfigError, match="disabled"):
            await service.report()
    finally:
        db.close()
