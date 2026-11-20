"""Benchmark registry, runner, persistence and comparison."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
from tests.support import default_settings

from bear_agent.benchmarks import (
    BenchmarkContext,
    BenchmarkRegistry,
    BenchmarkRunner,
    BenchmarkSample,
    build_registry,
    parse_params,
)
from bear_agent.observability.metrics.stats import Distribution
from bear_agent.storage.database import Database


def test_registry_lists_real_scenarios() -> None:
    registry = build_registry()
    ids = {item.id for item in registry.list()}
    assert {
        "kernel.session_create",
        "tool.read_file",
        "context.l1_compaction",
        "task.schedule",
        "provider.fallback",
        "mcp.initialize",
        "e2e.single_tool",
        "provider.real_request",
    } <= ids
    assert all(item.id for item in registry.list(category="e2e"))
    with pytest.raises(KeyError):
        registry.get("does.not.exist")
    with pytest.raises(ValueError):
        registry.register(registry.get("kernel.session_create"))


def test_parse_params_and_distribution() -> None:
    assert parse_params(["count=10", "flag=true", "ratio=0.5", "name=x"]) == {
        "count": 10,
        "flag": True,
        "ratio": 0.5,
        "name": "x",
    }
    with pytest.raises(ValueError):
        parse_params(["invalid"])
    stats = Distribution.of([1.0, 2.0, 3.0])
    assert stats.count == 3
    assert stats.median == 2.0
    assert stats.to_dict()["p95"] == pytest.approx(2.9)


class _FailingScenario:
    id = "test.failing"
    name = "Failing"
    category = "test"
    kind = "synthetic"
    description = "Always raises."
    default_params: ClassVar[dict[str, Any]] = {}
    requires_network = False

    async def prepare(self, ctx: BenchmarkContext) -> None:
        return None

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        raise RuntimeError("boom token=sk-abcdefghijklmnop")

    async def cleanup(self, ctx: BenchmarkContext) -> None:
        return None


@pytest.mark.asyncio
async def test_runner_warmup_stats_persistence_and_compare(tmp_path: Path) -> None:
    settings = default_settings(tmp_path)
    db = Database(tmp_path / "bench.db")
    try:
        registry = build_registry()
        runner = BenchmarkRunner(settings, registry=registry, cwd=Path.cwd(), db=db)
        first = await runner.run(
            "kernel.tool_registry_lookup",
            iterations=3,
            warmup=1,
            params={"count": 100},
            save=True,
        )
        assert first["status"] == "completed"
        assert first["warmup_iterations"] == 1
        assert len(first["samples"]) == 3
        assert first["samples"][0]["status"] == "ok"
        assert first["stats"]["successful"] == 3
        assert first["stats"]["duration_ms"]["count"] == 3
        assert first["environment"]["python"]
        assert first["environment"]["app_version"]
        assert first["settings"]["default_provider"] == "fake"
        assert "api_key" not in str(first["settings"])

        history = await runner.history(scenario_id="kernel.tool_registry_lookup")
        assert len(history) == 1
        assert history[0]["run_id"] == first["run_id"]
        assert await runner.get(first["run_id"]) is not None

        second = await runner.run(
            "kernel.tool_registry_lookup",
            iterations=3,
            warmup=1,
            params={"count": 100},
            save=True,
        )
        comparison = await runner.compare(first["run_id"], second["run_id"])
        assert comparison["scenario_id"] == "kernel.tool_registry_lookup"
        assert comparison["compatible_environment"] is True
        assert comparison["verdict"] in {"stable", "improvement", "regression"}
        assert "duration_p95" in comparison["metrics"]
        with pytest.raises(ValueError, match="different scenario parameters"):
            await runner.compare(
                first["run_id"],
                (
                    await runner.run(
                        "kernel.tool_registry_lookup",
                        iterations=1,
                        warmup=0,
                        params={"count": 999},
                        save=True,
                    )
                )["run_id"],
            )

        skipped = await runner.run("provider.real_request", iterations=1, warmup=0)
        assert skipped["status"] == "skipped"
        assert "allow-network" in skipped["skip_reason"]

        traced = await runner.run(
            "kernel.tool_registry_lookup",
            iterations=1,
            warmup=0,
            params={"count": 10},
            trace=True,
            save=False,
        )
        assert traced["status"] == "completed"
        assert any("trace" in note for note in traced["notes"])

        settings.benchmarks.max_runs = 1
        deleted = await runner.prune()
        assert deleted >= 1
        assert len(await runner.history()) <= 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_runner_records_errors_and_redacts(tmp_path: Path) -> None:
    settings = default_settings(tmp_path)
    db = Database(tmp_path / "bench.db")
    try:
        registry = BenchmarkRegistry()
        registry.register(_FailingScenario())
        runner = BenchmarkRunner(settings, registry=registry, cwd=Path.cwd(), db=db)
        result = await runner.run("test.failing", iterations=1, warmup=0, save=True)
        assert result["status"] == "failed"
        assert result["stats"]["error_rate"] == 1.0
        assert result["samples"][0]["status"] == "error"
        assert "sk-abcdefghijklmnop" not in result["samples"][0]["error"]
        assert "[REDACTED]" in result["samples"][0]["error"]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_compare_rejects_failed_or_mismatched_runs(tmp_path: Path) -> None:
    settings = default_settings(tmp_path)
    db = Database(tmp_path / "bench2.db")
    try:
        registry = build_registry()
        runner = BenchmarkRunner(settings, registry=registry, cwd=Path.cwd(), db=db)
        good = await runner.run(
            "kernel.tool_registry_lookup", iterations=3, warmup=0, params={"count": 10}
        )
        short = await runner.run(
            "kernel.tool_registry_lookup", iterations=2, warmup=0, params={"count": 10}
        )
        with pytest.raises(ValueError, match="iteration"):
            await runner.compare(good["run_id"], short["run_id"])
        failing_registry = BenchmarkRegistry()
        failing_registry.register(_FailingScenario())
        failing_runner = BenchmarkRunner(settings, registry=failing_registry, cwd=Path.cwd(), db=db)
        failed = await failing_runner.run("test.failing", iterations=1, warmup=0)
        failed_again = await failing_runner.run("test.failing", iterations=1, warmup=0)
        with pytest.raises(ValueError, match="completed"):
            await failing_runner.compare(failed["run_id"], failed_again["run_id"])
    finally:
        db.close()


def test_benchmark_profile_disables_harness_tracing(tmp_path: Path) -> None:
    from bear_agent.benchmarks.fixtures import bench_settings
    from bear_agent.benchmarks.models import BenchmarkContext

    settings = default_settings(tmp_path)
    ctx = BenchmarkContext(
        settings=settings,
        root=tmp_path,
        workspace=tmp_path,
        data_dir=tmp_path / "data",
    )
    profile = bench_settings(ctx)
    assert profile.observability.enabled is False
    assert profile.observability.traces_enabled is False
    assert profile.observability.metrics_enabled is False
    assert profile.agent.default_provider == "bench"
