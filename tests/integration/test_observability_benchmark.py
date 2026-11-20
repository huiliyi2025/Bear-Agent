"""Benchmark and performance checks for the observability layer."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.support import default_settings, make_container, new_session, text_script

from bear_agent.benchmarks import BenchmarkRunner, build_registry
from bear_agent.config.models import ObservabilityConfig
from bear_agent.observability.metrics import MetricsService
from bear_agent.observability.trace import span
from bear_agent.observability.trace.service import TraceService
from bear_agent.storage.database import Database


@pytest.mark.asyncio
async def test_benchmark_runs_real_runtime_and_compares(tmp_path: Path) -> None:
    settings = default_settings(tmp_path)
    db = Database(tmp_path / "bench.db")
    try:
        runner = BenchmarkRunner(settings, registry=build_registry(), cwd=Path.cwd(), db=db)
        first = await runner.run("e2e.single_tool", iterations=1, warmup=0, save=True)
        assert first["status"] == "completed", first["samples"]
        assert first["samples"][0]["status"] == "ok"
        assert first["stats"]["tokens"] > 0
        second = await runner.run("e2e.single_tool", iterations=1, warmup=0, save=True)
        assert second["status"] == "completed"
        comparison = await runner.compare(first["run_id"], second["run_id"])
        assert comparison["scenario_id"] == "e2e.single_tool"
        assert comparison["kind"] == "scripted"
        history = await runner.history(scenario_id="e2e.single_tool")
        assert len(history) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_all_builtin_scenarios_execute_once(tmp_path: Path) -> None:
    settings = default_settings(tmp_path)
    db = Database(tmp_path / "bench-all.db")
    try:
        registry = build_registry()
        runner = BenchmarkRunner(settings, registry=registry, cwd=Path.cwd(), db=db)
        failures: list[str] = []
        for info in registry.list():
            result = await runner.run(
                info.id,
                iterations=1,
                warmup=0,
                save=False,
                allow_network=False,
            )
            if result["status"] == "failed":
                failures.append(f"{info.id}: {result['samples']}")
        assert not failures, failures
    finally:
        db.close()


@pytest.mark.asyncio
async def test_trace_write_overhead_is_bounded(tmp_path: Path) -> None:
    db = Database(tmp_path / "trace.db")
    try:
        tracer = TraceService(db, config=ObservabilityConfig(trace_batch_size=32))
        started = time.perf_counter()
        async with tracer.trace("perf", session_id="bench"):
            for index in range(500):
                async with span("unit", "internal", {"index": index}):
                    pass
        elapsed = time.perf_counter() - started
        await tracer.close()
        stats = await tracer.stats()
        assert stats["spans"] == 501
        # Generous CI-safe ceiling; local measurements are far below this.
        assert elapsed < 10.0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_metrics_query_scales_to_thousands_of_rows(tmp_path: Path) -> None:
    db = Database(tmp_path / "metrics.db")
    try:
        from bear_agent.sessions.service import SessionService

        sessions = SessionService(db)
        session = await sessions.create(cwd=tmp_path, model="m", provider="p")
        now = datetime.now(UTC)
        conn = db.connection
        with conn:
            conn.executemany(
                "INSERT INTO model_calls (id, session_id, turn_id, provider, model, "
                "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
                "latency_ms, status, created_at) VALUES (?, ?, NULL, 'p', 'm', 10, 5, 0, 0, 25, 'completed', ?)",
                [(f"mc-{index}", session.id, now.isoformat()) for index in range(2_000)],
            )
            conn.executemany(
                "INSERT INTO tool_calls (id, session_id, turn_id, name, args_json, status, "
                "idempotent, started_at, finished_at, result_summary, error_kind, error_message) "
                "VALUES (?, ?, NULL, 'read_file', '{}', 'completed', 1, ?, ?, 'ok', NULL, NULL)",
                [
                    (
                        f"tc-{index}",
                        session.id,
                        (now - timedelta(milliseconds=10)).isoformat(),
                        now.isoformat(),
                    )
                    for index in range(2_000)
                ],
            )
        started = time.perf_counter()
        report = await MetricsService(db, config=ObservabilityConfig()).report(
            since=now - timedelta(hours=1), session_id=session.id
        )
        elapsed = time.perf_counter() - started
        assert report["models"]["calls"] == 2_000
        assert report["tools"]["calls"] == 2_000
        assert report["models"]["total_tokens"] == 30_000
        assert elapsed < 5.0, elapsed
        serialized = json.dumps(report)
        assert session.id not in json.dumps(report["models"]["by_provider"])
        assert "args_json" not in serialized
    finally:
        db.close()


@pytest.mark.asyncio
async def test_traced_runtime_overhead_is_bounded(workspace: Path, settings: Any) -> None:
    async def measure(traces_enabled: bool) -> float:
        local = settings.model_copy(deep=True)
        local.observability.traces_enabled = traces_enabled
        container, _ = make_container(
            local,
            workspace,
            [text_script("done") for _ in range(5)],
        )
        try:
            session = await new_session(container, workspace)
            started = time.perf_counter()
            for _ in range(5):
                result = await container.runtime.run(session.id, "hello")
                assert result.final_text == "done"
            return time.perf_counter() - started
        finally:
            await container.close()

    untraced = await measure(False)
    traced = await measure(True)
    # The absolute overhead is what matters; this bound is deliberately loose
    # for slow shared CI runners while still catching accidental O(n^2) work.
    assert traced < untraced * 8 + 3.0
    await asyncio.sleep(0)
