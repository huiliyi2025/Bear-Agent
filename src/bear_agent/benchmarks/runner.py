"""Benchmark runner: warmup, iterations, statistics, persistence, comparison.

Timing is wall-clock around ``scenario.run`` only. Fixture creation belongs in
``prepare`` so a benchmark measures the operation it names, not database
startup. Every iteration gets a fresh temporary data directory and workspace.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import platform
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bear_agent import __version__
from bear_agent.benchmarks.models import (
    BenchmarkContext,
    BenchmarkSample,
    BenchmarkScenario,
    BenchmarkSkipped,
    BenchmarkStatus,
)
from bear_agent.benchmarks.registry import BenchmarkRegistry
from bear_agent.config.models import Settings
from bear_agent.core.ids import new_id
from bear_agent.core.jsonio import write_json
from bear_agent.observability.metrics.registry import MetricsRegistry
from bear_agent.observability.metrics.stats import Distribution
from bear_agent.observability.trace.service import TraceService
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.storage.database import Database
from bear_agent.storage.observability_repositories import BenchmarkRepository


class BenchmarkRunner:
    def __init__(
        self,
        settings: Settings,
        *,
        registry: BenchmarkRegistry,
        cwd: str | Path,
        db: Database | None = None,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.cwd = Path(cwd).resolve()
        self.db = db
        self.repository = BenchmarkRepository(db) if db is not None else None
        self.redactor = redactor or SecretRedactor()

    async def run(
        self,
        scenario_id: str,
        *,
        iterations: int | None = None,
        warmup: int | None = None,
        params: dict[str, Any] | None = None,
        trace: bool = False,
        save: bool = True,
        allow_network: bool = False,
    ) -> dict[str, Any]:
        if not self.settings.benchmarks.enabled:
            raise BenchmarkSkipped("benchmarks are disabled in configuration")
        scenario = self.registry.get(scenario_id)
        requested_count = (
            iterations if iterations is not None else self.settings.benchmarks.default_iterations
        )
        if requested_count < 1:
            raise ValueError("benchmark iterations must be at least 1")
        count = _clamp(requested_count, self.settings.benchmarks.max_iterations)
        warmup_count = _clamp(
            warmup if warmup is not None else self.settings.benchmarks.default_warmup,
            self.settings.benchmarks.max_iterations,
        )
        merged = {**scenario.default_params, **(params or {})}
        run_id = new_id("bench")
        started = datetime.now(UTC)
        samples: list[dict[str, Any]] = []
        notes: list[str] = []
        status: BenchmarkStatus = "completed"
        skipped_reason: str | None = None
        if scenario.requires_network and not allow_network:
            status = "skipped"
            skipped_reason = (
                "scenario requires a real provider/network; rerun with --allow-network "
                "and a configured provider"
            )
            notes.append("real-provider benchmarks are separated from scripted runs")
        selected = self.registry.scenario_info(scenario_id)
        env = _environment(self.cwd)
        if warmup_count:
            notes.append(f"{warmup_count} warmup iteration(s) discarded")
        if count != requested_count:
            notes.append(
                f"requested {requested_count} iterations; clamped to {count} by benchmarks.max_iterations"
            )
        if trace:
            notes.append("--trace was enabled; timings include trace span persistence")
        if status == "completed":
            try:
                for index in range(warmup_count):
                    await self._one_iteration(
                        scenario,
                        merged,
                        iteration=-(index + 1),
                        trace=trace,
                        measured=False,
                    )
                for index in range(count):
                    sample = await self._one_iteration(
                        scenario,
                        merged,
                        iteration=index,
                        trace=trace,
                        measured=True,
                    )
                    samples.append(sample)
                if samples and all(sample["status"] == "error" for sample in samples):
                    status = "failed"
            except BenchmarkSkipped as exc:
                status = "skipped"
                skipped_reason = str(exc)
            except BaseException:
                status = "failed"
                raise
        finished = datetime.now(UTC)
        stats = _aggregate(samples, 0 if status == "skipped" else count)
        settings_snapshot = self.redactor.redact_obj(_settings_snapshot(self.settings))
        settings_hash = hashlib.sha256(
            json.dumps(settings_snapshot, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        env["settings_hash"] = settings_hash
        result = {
            "schema_version": 1,
            "run_id": run_id,
            "scenario": self.redactor.redact_obj(selected.to_dict()),
            "parameters": self.redactor.redact_obj(merged),
            "kind": scenario.kind,
            "status": status,
            "skip_reason": skipped_reason,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "requested_iterations": count,
            "requested_iterations_raw": requested_count,
            "warmup_iterations": warmup_count,
            "samples": samples,
            "stats": stats,
            "environment": env,
            "settings": settings_snapshot,
            "settings_hash": settings_hash,
            "notes": [
                *notes,
                "statistics use only successful samples",
                "throughput = successful iterations / sum(successful durations)",
                "results are specific to this machine, dependency lock and scenario parameters",
                "git_dirty=true means the measured working tree differs from the recorded commit",
            ],
        }
        if save and self.repository is not None:
            try:
                await self.repository.insert(
                    run_id=run_id,
                    scenario_id=scenario.id,
                    scenario_name=scenario.name,
                    category=scenario.category,
                    kind=scenario.kind,
                    status=status,
                    started_at=started,
                    finished_at=finished,
                    result=result,
                    app_version=__version__,
                    git_commit=env.get("git_commit"),
                )
            except Exception as exc:
                result["persistence_error"] = self.redactor.redact(str(exc))[:1_000]
                notes.append("result persistence failed; the benchmark result was not saved")
        return result

    async def _one_iteration(
        self,
        scenario: BenchmarkScenario,
        params: dict[str, Any],
        *,
        iteration: int,
        trace: bool,
        measured: bool,
    ) -> dict[str, Any]:
        root = Path(tempfile.mkdtemp(prefix="bear-bench-"))
        workspace = root / "workspace"
        data_dir = root / "data"
        workspace.mkdir()
        data_dir.mkdir()
        isolated_settings = self.settings.model_copy(deep=True)
        isolated_settings.storage.data_dir = data_dir
        ctx = BenchmarkContext(
            settings=isolated_settings,
            root=root,
            workspace=workspace,
            data_dir=data_dir,
            params=dict(params),
            redactor=self.redactor,
            registry=MetricsRegistry(),
        )
        database: Database | None = None
        tracer: TraceService | None = None
        started = time.perf_counter()
        try:
            if trace:
                database = Database(data_dir / "trace.sqlite")
                tracer = TraceService(
                    database,
                    config=self.settings.observability,
                    redactor=self.redactor,
                )
                ctx.tracer = tracer
            await scenario.prepare(ctx)
            started = time.perf_counter()
            if trace and tracer is not None:
                async with tracer.trace(
                    "benchmark.iteration",
                    kind="benchmark",
                    session_id="benchmark",
                    attributes={"scenario": scenario.id, "iteration": iteration},
                ):
                    outcome = await scenario.run(ctx)
            else:
                outcome = await scenario.run(ctx)
            elapsed_ms = (time.perf_counter() - started) * 1000
            if not isinstance(outcome, BenchmarkSample):
                raise TypeError("benchmark scenario returned an invalid sample")
            sample = {
                "iteration": iteration,
                "duration_ms": round(elapsed_ms, 3),
                "status": "ok" if measured else "warmup",
                "operations": outcome.operations,
                "tokens": outcome.tokens,
                "cost": outcome.cost,
                "note": self.redactor.redact(outcome.note),
                "metadata": self.redactor.redact_obj(outcome.metadata),
            }
            snapshot = ctx.registry.snapshot()
            if snapshot:
                sample["metrics"] = self.redactor.redact_obj(snapshot)
            return sample
        except BenchmarkSkipped:
            raise
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000
            return {
                "iteration": iteration,
                "duration_ms": round(elapsed_ms, 3),
                "status": "error",
                "error": self.redactor.redact(f"{type(exc).__name__}: {exc}")[:1_000],
                "metadata": {},
            }
        finally:
            with contextlib.suppress(Exception):
                await scenario.cleanup(ctx)
            container = ctx.state.pop("container", None)
            if container is not None:
                with contextlib.suppress(Exception):
                    await container.close()
            if tracer is not None:
                await tracer.close()
            if database is not None:
                database.close()
            shutil.rmtree(root, ignore_errors=True)

    # --------------------------------------------------------------- queries

    async def history(
        self, *, scenario_id: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        if self.repository is None:
            return []
        return await self.repository.list_runs(scenario_id=scenario_id, limit=limit)

    async def get(self, run_id: str) -> dict[str, Any] | None:
        if self.repository is None:
            return None
        return await self.repository.get(run_id)

    async def prune(self) -> int:
        if self.repository is None:
            return 0
        config = self.settings.benchmarks
        cutoff = datetime.now(UTC) - timedelta(days=config.retention_days)
        return await self.repository.prune(cutoff=cutoff, max_runs=config.max_runs)

    async def compare(
        self,
        run_a: str,
        run_b: str,
        *,
        threshold_pct: float | None = None,
    ) -> dict[str, Any]:
        if self.repository is None:
            raise ValueError("benchmark persistence is not configured")
        first = await self.repository.get(run_a)
        second = await self.repository.get(run_b)
        if first is None or second is None:
            missing = run_a if first is None else run_b
            raise ValueError(f"benchmark run not found: {missing}")
        if first["scenario_id"] != second["scenario_id"]:
            raise ValueError("cannot compare different scenarios; run the same scenario twice")
        if first["result"].get("parameters") != second["result"].get("parameters"):
            raise ValueError(
                "cannot compare runs with different scenario parameters; "
                "rerun both with identical --param values"
            )
        for label, record in (("a", first), ("b", second)):
            if record["status"] != "completed":
                raise ValueError(
                    f"run {label} is {record['status']}; only completed runs can be compared"
                )
            result = record["result"]
            if not result.get("stats") or "duration_ms" not in result["stats"]:
                raise ValueError(f"run {label} has no usable statistics")
            if int(result["stats"].get("successful", 0)) < 1:
                raise ValueError(f"run {label} has no successful samples")
            if result.get("requested_iterations") != first["result"].get(
                "requested_iterations"
            ) or (result.get("warmup_iterations") != first["result"].get("warmup_iterations")):
                raise ValueError("cannot compare runs with different iteration counts")
        threshold = (
            threshold_pct
            if threshold_pct is not None
            else self.settings.benchmarks.compare_threshold_pct
        )
        if not math.isfinite(threshold) or threshold < 0:
            raise ValueError("compare threshold must be a finite non-negative percentage")
        stats_a = first["result"]["stats"]
        stats_b = second["result"]["stats"]
        metrics: dict[str, Any] = {}
        for key in ("mean", "p50", "p95", "p99", "max"):
            before = float(stats_a["duration_ms"].get(key, 0.0))
            after = float(stats_b["duration_ms"].get(key, 0.0))
            metrics[f"duration_{key}"] = _delta(before, after, threshold_pct=threshold)
        metrics["throughput"] = _delta(
            float(stats_a.get("throughput_per_s", 0.0)),
            float(stats_b.get("throughput_per_s", 0.0)),
            threshold_pct=threshold,
            higher_is_better=True,
        )
        metrics["error_rate"] = _delta(
            float(stats_a.get("error_rate", 0.0)),
            float(stats_b.get("error_rate", 0.0)),
            threshold_pct=threshold,
            higher_is_better=False,
        )
        env_a = first["result"].get("environment", {})
        env_b = second["result"].get("environment", {})
        compatibility_fields = (
            "platform",
            "python",
            "app_version",
            "settings_hash",
            "uv_lock_sha256_16",
            "kind",
        )
        incompatible = [
            field
            for field in compatibility_fields
            if (env_a.get(field) if field != "kind" else first["kind"])
            != (env_b.get(field) if field != "kind" else second["kind"])
        ]
        return {
            "schema_version": 1,
            "scenario_id": first["scenario_id"],
            "scenario_name": first["scenario_name"],
            "run_a": run_a,
            "run_b": run_b,
            "started_a": first["started_at"],
            "started_b": second["started_at"],
            "kind": first["kind"],
            "compatible_environment": not incompatible,
            "incompatible_fields": incompatible,
            "threshold_pct": threshold,
            "metrics": metrics,
            "verdict": _overall_verdict(metrics),
        }


def _aggregate(samples: list[dict[str, Any]], requested: int) -> dict[str, Any]:
    successful = [sample for sample in samples if sample.get("status") == "ok"]
    durations = [float(sample["duration_ms"]) for sample in successful]
    errors = sum(1 for sample in samples if sample.get("status") == "error")
    total_errors = errors + max(0, requested - len(samples))
    tokens = sum(int(sample.get("tokens", 0)) for sample in successful)
    cost = sum(float(sample.get("cost", 0.0)) for sample in successful)
    operations = sum(int(sample.get("operations", 1)) for sample in successful)
    successful_ms = sum(durations)
    all_ms = sum(float(sample.get("duration_ms", 0.0)) for sample in samples)
    return {
        "iterations": requested,
        "successful": len(successful),
        "errors": total_errors,
        "error_rate": round(total_errors / requested, 4) if requested else 0.0,
        "operations": operations,
        "tokens": tokens,
        "cost": round(cost, 6),
        "duration_ms": Distribution.of(durations).to_dict(),
        # Wall-clock throughput includes failed iterations; the successful-only
        # value is retained for diagnosing reliability/performance trade-offs.
        "throughput_per_s": round(len(successful) / (all_ms / 1000), 3) if all_ms else 0.0,
        "successful_throughput_per_s": round(len(successful) / (successful_ms / 1000), 3)
        if successful_ms
        else 0.0,
        "ops_per_s": round(operations / (all_ms / 1000), 3) if all_ms else 0.0,
    }


def _delta(
    before: float,
    after: float,
    *,
    threshold_pct: float,
    higher_is_better: bool = False,
) -> dict[str, Any]:
    pct = (0.0 if after == 0 else 100.0) if before == 0 else (after - before) / before * 100
    if pct == 0 or abs(pct) <= threshold_pct:
        verdict = "stable"
    elif higher_is_better:
        verdict = "improvement" if pct > 0 else "regression"
    else:
        verdict = "regression" if pct > 0 else "improvement"
    return {
        "before": round(before, 3),
        "after": round(after, 3),
        "delta_pct": round(pct, 2),
        "verdict": verdict,
    }


def _overall_verdict(metrics: dict[str, Any]) -> str:
    verdicts = {value["verdict"] for value in metrics.values()}
    if "regression" in verdicts:
        return "regression"
    if "improvement" in verdicts:
        return "improvement"
    return "stable"


def _clamp(value: int, maximum: int) -> int:
    if value < 0:
        raise ValueError("iterations/warmup must not be negative")
    return min(value, maximum)


def _environment(cwd: Path) -> dict[str, Any]:
    lock_hash = None
    lock = cwd / "uv.lock"
    if lock.exists():
        try:
            lock_hash = hashlib.sha256(lock.read_bytes()).hexdigest()[:16]
        except OSError:
            lock_hash = None
    return {
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "sqlite": sqlite3.sqlite_version,
        "app_version": __version__,
        "git_commit": _git_commit(cwd),
        "git_dirty": _git_dirty(cwd),
        "uv_lock_sha256_16": lock_hash,
    }


def _git_dirty(cwd: Path) -> bool | None:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def _git_commit(cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _settings_snapshot(settings: Settings) -> dict[str, Any]:
    return {
        "default_provider": settings.agent.default_provider,
        "default_model": settings.agent.default_model,
        "permission_mode": str(settings.agent.permission_mode),
        "sandbox_mode": settings.sandbox.mode,
        "context": settings.context.model_dump(),
        "tasks": settings.tasks.model_dump(),
        "observability": {
            "enabled": settings.observability.enabled,
            "traces_enabled": settings.observability.traces_enabled,
            "metrics_enabled": settings.observability.metrics_enabled,
            "trace_sample_ratio": settings.observability.trace_sample_ratio,
        },
        "hooks": {
            "enabled": settings.hooks.enabled,
            "allow_external": settings.hooks.allow_external,
            "commands": len(settings.hooks.commands),
        },
        "plugins": {
            "enabled": settings.plugins.enabled,
            "allow_python": settings.plugins.allow_python,
            "dirs": len(settings.plugins.dirs),
        },
        "mcp": {
            "enabled": settings.mcp.enabled,
            "servers": sorted(settings.mcp.servers),
        },
        "checkpoints": {
            "enabled": settings.checkpoints.enabled,
            "interval_turns": settings.checkpoints.interval_turns,
        },
        "recovery": {"enabled": settings.recovery.enabled},
        "providers": {
            name: {
                "base_url": _safe_url(provider.base_url),
                "models": provider.models,
                "max_retries": provider.max_retries,
            }
            for name, provider in settings.providers.items()
        },
    }


def _safe_url(url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except ValueError:
        return "<invalid-url>"


def parse_params(values: list[str]) -> dict[str, Any]:
    """Parse repeated ``key=value`` CLI parameters with JSON scalar coercion."""
    result: dict[str, Any] = {}
    for item in values:
        key, separator, raw = item.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"invalid parameter {item!r}; use key=value")
        value = raw.strip()
        if value.lower() in {"true", "false"}:
            result[key.strip()] = value.lower() == "true"
        else:
            try:
                result[key.strip()] = int(value)
            except ValueError:
                try:
                    result[key.strip()] = float(value)
                except ValueError:
                    result[key.strip()] = value
    return result


__all__ = ["BenchmarkRunner", "parse_params", "write_json"]
