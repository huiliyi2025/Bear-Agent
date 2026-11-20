"""Durable metrics, computed from authoritative facts.

There is deliberately no metric-sample table. Session events, model calls, tool
calls, tasks, attempts, plans, compactions, checkpoints and traces already form
a complete, indexed record of what happened. Re-writing those facts as metric
samples would double the write load and create a second source of truth. This
module aggregates them on demand, with bounded queries and explicit notes about
truncation and platform limits.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from bear_agent.config.models import ObservabilityConfig, Settings
from bear_agent.core.clock import Clock, SystemClock
from bear_agent.core.errors import ConfigError
from bear_agent.core.events import EventType
from bear_agent.core.fencing import fence_rejections_total
from bear_agent.core.resources import (
    cpu_time_seconds,
    disk_usage,
    file_size,
    process_memory_bytes,
)
from bear_agent.observability.metrics.stats import Distribution
from bear_agent.storage.database import Database

MAX_ROWS = 200_000

_EVENT_TYPES = (
    EventType.RUN_STARTED,
    EventType.RUN_COMPLETED,
    EventType.RUN_CANCELLED,
    EventType.RUN_FAILED,
    EventType.TASK_RUN_STARTED,
    EventType.TASK_RUN_COMPLETED,
    EventType.TASK_RUN_FAILED,
    EventType.MODEL_RETRY,
    EventType.PROVIDER_RETRY,
    EventType.PROVIDER_FALLBACK,
    EventType.PROVIDER_CIRCUIT_OPEN,
    EventType.PROVIDER_FAILED,
    EventType.POLICY_DECISION,
    EventType.APPROVAL_REQUESTED,
    EventType.APPROVAL_DECIDED,
    EventType.SANDBOX_DECISION,
    EventType.CLAIM_CONFLICT,
    EventType.CONTEXT_COMPACTED,
    EventType.CONTEXT_COMPACTION_FAILED,
    EventType.CONTEXT_RESET,
    EventType.CONTEXT_BUILT,
    EventType.CONTEXT_TRUNCATED,
    EventType.CONTEXT_ANCHOR_ADDED,
    EventType.CONTEXT_ANCHOR_REMOVED,
    EventType.CHECKPOINT_CREATED,
    EventType.CHECKPOINT_INVALID,
    EventType.CHECKPOINT_PRUNED,
    EventType.RECOVERY_STARTED,
    EventType.RECOVERY_COMPLETED,
    EventType.RECOVERY_FAILED,
    EventType.RECOVERY_TOOL_RECONCILED,
    EventType.RECOVERY_PROCESS_TERMINATED,
    EventType.RECOVERY_CLAIMS_RELEASED,
    EventType.SESSION_FORKED,
    EventType.TASK_COMPLETED,
    EventType.TASK_FAILED,
    EventType.TASK_BLOCKED,
    EventType.TASK_CANCELLED,
    EventType.TASK_RETRY_SCHEDULED,
    EventType.TASK_PLAN_REVISED,
    EventType.TASK_VERIFICATION,
    EventType.HOOK_COMPLETED,
    EventType.HOOK_FAILED,
    EventType.HOOK_DENIED,
    EventType.MCP_SERVER_STARTED,
    EventType.MCP_SERVER_FAILED,
    EventType.MCP_SERVER_RECONNECTED,
    EventType.MCP_SERVER_STOPPED,
    EventType.PLUGIN_LOADED,
    EventType.PLUGIN_FAILED,
)


@dataclass(frozen=True)
class EventFact:
    session_id: str
    run_id: str | None
    type: str
    ts: datetime
    payload: dict[str, Any]


@dataclass
class _RunAccumulator:
    run_id: str
    session_id: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    status: str = "unknown"
    kind: str = "request"
    model: str = ""
    provider: str = ""
    task_id: str | None = None

    @property
    def duration_ms(self) -> float | None:
        if self.started_at is None or self.ended_at is None:
            return None
        return max(0.0, (self.ended_at - self.started_at).total_seconds() * 1000)


class MetricsService:
    def __init__(
        self,
        db: Database,
        *,
        config: ObservabilityConfig | None = None,
        clock: Clock | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.db = db
        self.config = config or ObservabilityConfig()
        self.clock = clock or SystemClock()
        self.settings = settings

    async def report(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not self.config.enabled or not self.config.metrics_enabled:
            raise ConfigError("metrics are disabled by [observability] configuration")
        until = (until or self.clock.now()).astimezone(UTC)
        # ``None`` means all recorded history. The CLI applies its own default
        # window (7d) before calling this method.
        since = (since or datetime(1970, 1, 1, tzinfo=UTC)).astimezone(UTC)
        if since > until:
            raise ValueError("since must not be after until")
        events, truncated = await self._fetch_events(since, until, session_id)
        runs = _run_stats(events)
        tasks = await self._task_stats(since, until, session_id)
        models = await self._model_stats(since, until, session_id)
        provider_events = _provider_stats(events)
        models["retries"] = provider_events["retries"]
        models["provider_retries"] = provider_events["provider_retries"]
        models["model_retries"] = provider_events["model_retries"]
        models["fallbacks"] = provider_events["fallbacks"]
        models["circuit_opens"] = provider_events["circuit_opens"]
        models["provider_failures"] = provider_events["failures"]
        tools = await self._tool_stats(since, until, session_id, events)
        if self.settings is not None:
            tools["mcp"]["enabled"] = self.settings.mcp.enabled
            tools["mcp"]["configured_servers"] = sorted(self.settings.mcp.servers)
            tools["plugins"]["enabled"] = self.settings.plugins.enabled
            tools["plugins"]["allow_python"] = self.settings.plugins.allow_python
            tools["plugins"]["configured_dirs"] = len(self.settings.plugins.dirs)
        tools["mcp"]["history_available"] = False
        tools["plugins"]["history_available"] = False
        context = await self._context_stats(since, until, session_id, events)
        recovery = await self._recovery_stats(since, until, session_id, events)
        system = await self._system_stats(since, until, session_id)
        notes = [
            "metrics are aggregated from existing events and domain tables; no "
            "second sample store is written",
            "cost is an estimate from configured prices and is not a provider invoice",
            "tool latency is measured across the whole ToolExecutor call",
            "tool retries are not automatic; the reported count is always zero",
            "MCP/plugin lifecycle events are process-local and are not emitted as durable "
            "events; use `bear mcp status`, `bear plugins list` or doctor --active",
            "MODEL_RETRY is emitted only by legacy retrying providers; the router uses "
            "provider.retry",
            "fencing rejection counts are process-local and reset on restart",
            "MCP live connection state is process-local; lifecycle events are historical",
            "percentiles describe the rows returned; truncated=true means a lower bound",
        ]
        if truncated:
            notes.append(
                f"the event query reached the {MAX_ROWS} row limit; counts are lower bounds"
            )
        return {
            "schema_version": 1,
            "generated_at": self.clock.now().isoformat(),
            "window": {
                "since": since.isoformat(),
                "until": until.isoformat(),
                "session_id": session_id,
            },
            "truncated": truncated,
            "runs": runs,
            "tasks": tasks,
            "models": models,
            "tools": tools,
            "context": context,
            "recovery": recovery,
            "system": system,
            "notes": notes,
        }

    # ------------------------------------------------------------- fetching

    async def _fetch_events(
        self, since: datetime, until: datetime, session_id: str | None
    ) -> tuple[list[EventFact], bool]:
        types = tuple(event.value for event in _EVENT_TYPES)
        placeholders = ",".join("?" for _ in types)
        clauses = [f"type IN ({placeholders})", "created_at >= ?", "created_at <= ?"]
        params: list[Any] = [*types, since.isoformat(), until.isoformat()]
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        params.append(MAX_ROWS + 1)
        sql = (
            "SELECT session_id, run_id, type, payload_json, created_at FROM events "
            f"WHERE {' AND '.join(clauses)} ORDER BY created_at ASC, seq ASC LIMIT ?"
        )

        def _work(conn: sqlite3.Connection) -> list[sqlite3.Row]:
            return conn.execute(sql, params).fetchall()

        rows = await self.db.run(_work)
        truncated = len(rows) > MAX_ROWS
        facts: list[EventFact] = []
        for row in rows[:MAX_ROWS]:
            try:
                ts = datetime.fromisoformat(row["created_at"])
            except (TypeError, ValueError):
                continue
            facts.append(
                EventFact(
                    session_id=str(row["session_id"]),
                    run_id=row["run_id"],
                    type=str(row["type"]),
                    ts=ts if ts.tzinfo else ts.replace(tzinfo=UTC),
                    payload=_loads(row["payload_json"]),
                )
            )
        return facts, truncated

    async def _task_stats(
        self, since: datetime, until: datetime, session_id: str | None
    ) -> dict[str, Any]:
        session_clause = " AND session_id = ?" if session_id else ""
        params: list[Any] = [since.isoformat(), until.isoformat()]
        if session_id:
            params.append(session_id)
        tasks_sql = (
            "SELECT status, attempt_count, verification, started_at, finished_at "
            f"FROM tasks WHERE active = 1 AND updated_at >= ? AND updated_at <= ?{session_clause}"
        )
        revisions_sql = (
            # Version 1 is the initial snapshot, not a revision. Count one
            # revision per plan beyond that, clamped at zero.
            "SELECT MAX(COUNT(*) - COUNT(DISTINCT plan_id), 0) FROM plan_revisions "
            "WHERE created_at >= ? AND created_at <= ?" + session_clause
        )
        plans_sql = (
            "SELECT status, COUNT(*) FROM plans WHERE updated_at >= ? AND updated_at <= ?"
            + session_clause
            + " GROUP BY status"
        )
        attempts_sql = (
            "SELECT status, COUNT(*) FROM task_attempts "
            "WHERE started_at >= ? AND started_at <= ?" + session_clause + " GROUP BY status"
        )

        def _work(conn: sqlite3.Connection) -> tuple[list[Any], int, list[Any], list[Any]]:
            return (
                conn.execute(tasks_sql, params).fetchall(),
                int(conn.execute(revisions_sql, params).fetchone()[0]),
                conn.execute(plans_sql, params).fetchall(),
                conn.execute(attempts_sql, params).fetchall(),
            )

        rows, revisions, plan_rows, attempt_rows = await self.db.run(_work)
        statuses: dict[str, int] = {}
        durations: list[float] = []
        retries = 0
        verification: dict[str, int] = {}
        for row in rows:
            status = str(row["status"])
            statuses[status] = statuses.get(status, 0) + 1
            retries += max(0, int(row["attempt_count"]) - 1)
            verification[str(row["verification"])] = (
                verification.get(str(row["verification"]), 0) + 1
            )
            if row["started_at"] and row["finished_at"]:
                started = _parse(row["started_at"])
                finished = _parse(row["finished_at"])
                if started and finished:
                    durations.append(max(0.0, (finished - started).total_seconds() * 1000))
        terminal = sum(
            statuses.get(status, 0) for status in ("completed", "failed", "blocked", "cancelled")
        )
        completed = statuses.get("completed", 0)
        plan_statuses = {str(row[0]): int(row[1]) for row in plan_rows}
        attempt_statuses = {str(row[0]): int(row[1]) for row in attempt_rows}
        return {
            "total": len(rows),
            "status_counts": statuses,
            "completed": completed,
            "failed": statuses.get("failed", 0),
            "blocked": statuses.get("blocked", 0),
            "cancelled": statuses.get("cancelled", 0),
            "pending": statuses.get("pending", 0),
            "in_progress": statuses.get("in_progress", 0),
            "skipped": statuses.get("skipped", 0),
            "terminal": terminal,
            "success_rate": round(completed / terminal, 4) if terminal else 0.0,
            "retries": retries,
            "attempts": sum(attempt_statuses.values()),
            "attempt_status_counts": attempt_statuses,
            "plan_revisions": revisions,
            "plan_status_counts": plan_statuses,
            "verification_modes": verification,
            "duration_ms": Distribution.of(durations).to_dict(),
        }

    async def _model_stats(
        self, since: datetime, until: datetime, session_id: str | None
    ) -> dict[str, Any]:
        clauses = ["created_at >= ?", "created_at <= ?"]
        params: list[Any] = [since.isoformat(), until.isoformat()]
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        params.append(MAX_ROWS + 1)
        sql = (
            "SELECT provider, model, input_tokens, output_tokens, cache_read_tokens, "
            "cache_write_tokens, latency_ms, status FROM model_calls "
            f"WHERE {' AND '.join(clauses)} ORDER BY created_at ASC LIMIT ?"
        )
        rows = await self.db.run(lambda conn: conn.execute(sql, params).fetchall())
        truncated = len(rows) > MAX_ROWS
        calls = 0
        failures = 0
        latencies: list[float] = []
        usage_missing = 0
        pricing_missing = 0
        input_tokens = 0
        output_tokens = 0
        cache_read = 0
        cache_write = 0
        by_target: dict[tuple[str, str], dict[str, Any]] = {}
        cost_by_currency: dict[str, float] = {}
        for row in rows[:MAX_ROWS]:
            calls += 1
            provider = str(row["provider"])
            model = str(row["model"])
            status = str(row["status"])
            if status != "completed":
                failures += 1
            tokens_in = int(row["input_tokens"])
            tokens_out = int(row["output_tokens"])
            cache_read_tokens = int(row["cache_read_tokens"])
            cache_write_tokens = int(row["cache_write_tokens"])
            input_tokens += tokens_in
            output_tokens += tokens_out
            cache_read += cache_read_tokens
            cache_write += cache_write_tokens
            latency = int(row["latency_ms"])
            if status == "completed" and latency > 0:
                latencies.append(float(latency))
            if status == "completed" and tokens_in + tokens_out == 0:
                usage_missing += 1
            target = by_target.setdefault(
                (provider, model),
                {
                    "calls": 0,
                    "failures": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "cost": 0.0,
                    "currency": None,
                    "pricing_missing": 0,
                },
            )
            target["calls"] += 1
            if status != "completed":
                target["failures"] += 1
            target["input_tokens"] += tokens_in
            target["output_tokens"] += tokens_out
            target["cache_read_tokens"] += cache_read_tokens
            target["cache_write_tokens"] += cache_write_tokens
            price = self.config.price_for(provider, model)
            if price is None:
                if tokens_in + tokens_out + cache_read_tokens + cache_write_tokens > 0:
                    target["pricing_missing"] += 1
                    target["currency"] = target["currency"] or self.config.default_currency
                    pricing_missing += 1
                continue
            cost = _estimate_cost(
                price,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                cache_read=cache_read_tokens,
                cache_write=cache_write_tokens,
            )
            target["cost"] += cost
            target["currency"] = price.currency
            cost_by_currency[price.currency] = cost_by_currency.get(price.currency, 0.0) + cost
        by_provider = [
            {
                "provider": provider,
                "model": model,
                **values,
                "cost": round(float(values["cost"]), 6),
                "success_rate": round(1 - float(values["failures"]) / float(values["calls"]), 4)
                if values["calls"]
                else 0.0,
            }
            for (provider, model), values in sorted(by_target.items())
        ]
        return {
            "calls": calls,
            "failures": failures,
            "success_rate": round(1 - failures / calls, 4) if calls else 0.0,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "usage_missing": usage_missing,
            "pricing_missing": pricing_missing,
            "cost_by_currency": {
                currency: round(value, 6) for currency, value in sorted(cost_by_currency.items())
            },
            "latency_ms": Distribution.of(latencies).to_dict(),
            "by_provider": by_provider,
            "truncated": truncated,
        }

    async def _tool_stats(
        self,
        since: datetime,
        until: datetime,
        session_id: str | None,
        events: list[EventFact],
    ) -> dict[str, Any]:
        clauses = [
            "COALESCE(started_at, finished_at) >= ?",
            "COALESCE(started_at, finished_at) <= ?",
        ]
        params: list[Any] = [since.isoformat(), until.isoformat()]
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        params.append(MAX_ROWS + 1)
        sql = (
            "SELECT name, status, error_kind, started_at, finished_at, idempotent "
            f"FROM tool_calls WHERE {' AND '.join(clauses)} ORDER BY started_at ASC LIMIT ?"
        )
        rows = await self.db.run(lambda conn: conn.execute(sql, params).fetchall())
        truncated = len(rows) > MAX_ROWS
        statuses: dict[str, int] = {}
        durations: list[float] = []
        by_tool: dict[str, dict[str, Any]] = {}
        mcp_by_server: dict[str, int] = {}
        shell_runs = 0
        shell_failures = 0
        timeouts = 0
        cancellations = 0
        sandboxed = 0
        for row in rows[:MAX_ROWS]:
            name = str(row["name"])
            status = str(row["status"])
            error_kind = row["error_kind"]
            statuses[status] = statuses.get(status, 0) + 1
            if status == "timeout" or error_kind == "timeout":
                timeouts += 1
            if status == "cancelled":
                cancellations += 1
            if name == "bash":
                shell_runs += 1
                if status == "failed":
                    shell_failures += 1
            duration: float | None = None
            if row["started_at"] and row["finished_at"]:
                started = _parse(row["started_at"])
                finished = _parse(row["finished_at"])
                if started and finished:
                    duration = max(0.0, (finished - started).total_seconds() * 1000)
                    durations.append(duration)
            entry = by_tool.setdefault(
                name,
                {
                    "calls": 0,
                    "completed": 0,
                    "failed": 0,
                    "cancelled": 0,
                    "other": 0,
                    "total_ms": 0.0,
                    "durations": [],
                },
            )
            entry["calls"] += 1
            if status == "completed":
                entry["completed"] += 1
            elif status == "cancelled":
                entry["cancelled"] += 1
            elif status == "failed":
                entry["failed"] += 1
            else:
                entry["other"] += 1
            if duration is not None:
                entry["total_ms"] += duration
                entry["durations"].append(duration)
            if name.startswith("mcp__"):
                parts = name.split("__", 2)
                server = parts[1] if len(parts) > 1 else "unknown"
                mcp_by_server[server] = mcp_by_server.get(server, 0) + 1
            if status == "completed" and error_kind is None:
                # Sandbox metadata is on the completion event, not the row.
                pass
        permission = {"deny": 0, "ask": 0, "allow": 0}
        requests: dict[str, datetime] = {}
        approval_waits: list[float] = []
        approved = 0
        denied = 0
        sandbox_backends: dict[str, int] = {}
        filesystem_isolated = 0
        hooks: dict[str, int] = {}
        hook_durations: list[float] = []
        plugin_events = {"loaded": 0, "failed": 0}
        mcp_events = {"started": 0, "failed": 0, "reconnected": 0, "stopped": 0}
        for event in events:
            payload = event.payload
            if event.type == EventType.POLICY_DECISION.value:
                decision = str(payload.get("decision", ""))
                if decision in permission:
                    permission[decision] += 1
            elif event.type == EventType.SANDBOX_DECISION.value:
                backend = str(payload.get("backend") or "unknown")
                sandbox_backends[backend] = sandbox_backends.get(backend, 0) + 1
                if payload.get("filesystem_isolated"):
                    filesystem_isolated += 1
                sandboxed += 1
            elif event.type == EventType.APPROVAL_REQUESTED.value:
                call_id = str(payload.get("tool_call_id") or "")
                if call_id:
                    requests[call_id] = event.ts
            elif event.type == EventType.APPROVAL_DECIDED.value:
                call_id = str(payload.get("tool_call_id") or "")
                if payload.get("approved"):
                    approved += 1
                else:
                    denied += 1
                requested = requests.pop(call_id, None)
                if requested is not None:
                    approval_waits.append(max(0.0, (event.ts - requested).total_seconds() * 1000))
            elif event.type in (
                EventType.HOOK_COMPLETED.value,
                EventType.HOOK_FAILED.value,
                EventType.HOOK_DENIED.value,
            ):
                for item in payload.get("hooks") or []:
                    if not isinstance(item, dict):
                        continue
                    status = str(item.get("status") or "unknown")
                    hooks[status] = hooks.get(status, 0) + 1
                    duration = item.get("duration_ms")
                    if isinstance(duration, (int, float)):
                        hook_durations.append(float(duration))
            elif event.type == EventType.MCP_SERVER_STARTED.value:
                mcp_events["started"] += 1
            elif event.type == EventType.MCP_SERVER_FAILED.value:
                mcp_events["failed"] += 1
            elif event.type == EventType.MCP_SERVER_RECONNECTED.value:
                mcp_events["reconnected"] += 1
            elif event.type == EventType.MCP_SERVER_STOPPED.value:
                mcp_events["stopped"] += 1
            elif event.type == EventType.PLUGIN_LOADED.value:
                plugin_events["loaded"] += 1
            elif event.type == EventType.PLUGIN_FAILED.value:
                plugin_events["failed"] += 1
        tools = [
            {
                "name": name,
                "calls": entry["calls"],
                "completed": entry["completed"],
                "failed": entry["failed"],
                "cancelled": entry["cancelled"],
                "other": entry["other"],
                "success_rate": round(entry["completed"] / entry["calls"], 4)
                if entry["calls"]
                else 0.0,
                "mean_ms": round(entry["total_ms"] / len(entry["durations"]), 3)
                if entry["durations"]
                else 0.0,
                "duration_ms": Distribution.of(entry["durations"]).to_dict(),
            }
            for name, entry in sorted(
                by_tool.items(), key=lambda item: (-item[1]["calls"], item[0])
            )
        ]
        completed = statuses.get("completed", 0)
        total = sum(statuses.values())
        return {
            "calls": total,
            "completed": completed,
            "failed": statuses.get("failed", 0),
            "cancelled": cancellations,
            "unknown": statuses.get("unknown", 0),
            "running": statuses.get("running", 0),
            "success_rate": round(completed / total, 4) if total else 0.0,
            "timeouts": timeouts,
            "retries": 0,
            "permission": permission,
            "approvals": {
                "requested": approved + denied + len(requests),
                "approved": approved,
                "denied": denied,
                "pending": len(requests),
                "wait_ms": Distribution.of(approval_waits).to_dict(),
            },
            "sandbox": {
                "runs": sandboxed,
                "filesystem_isolated": filesystem_isolated,
                "backends": sandbox_backends,
            },
            "shell": {"runs": shell_runs, "failures": shell_failures},
            "duration_ms": Distribution.of(durations).to_dict(),
            "by_tool": tools,
            "mcp": {"calls": sum(mcp_by_server.values()), "by_server": mcp_by_server, **mcp_events},
            "hooks": {
                "status_counts": hooks,
                "duration_ms": Distribution.of(hook_durations).to_dict(),
            },
            "plugins": plugin_events,
            "truncated": truncated,
        }

    async def _context_stats(
        self,
        since: datetime,
        until: datetime,
        session_id: str | None,
        events: list[EventFact],
    ) -> dict[str, Any]:
        clauses = ["created_at >= ?", "created_at <= ?"]
        params: list[Any] = [since.isoformat(), until.isoformat()]
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        sql = (
            "SELECT level, before_tokens, after_tokens, artifact_id FROM compactions "
            f"WHERE {' AND '.join(clauses)}"
        )
        rows = await self.db.run(lambda conn: conn.execute(sql, params).fetchall())
        levels: dict[str, int] = {}
        before = 0
        after = 0
        artifacts = 0
        for row in rows:
            levels[str(row["level"])] = levels.get(str(row["level"]), 0) + 1
            before += int(row["before_tokens"])
            after += int(row["after_tokens"])
            if row["artifact_id"]:
                artifacts += 1
        failures = 0
        resets = 0
        builds = 0
        truncated_context = 0
        budget_usage: list[float] = []
        anchors_added = 0
        anchors_removed = 0
        for event in events:
            if event.type == EventType.CONTEXT_COMPACTION_FAILED.value:
                failures += 1
            elif event.type == EventType.CONTEXT_RESET.value:
                resets += 1
            elif event.type == EventType.CONTEXT_BUILT.value:
                builds += 1
                estimated = event.payload.get("estimated_tokens")
                budget = event.payload.get("budget_tokens")
                if (
                    isinstance(estimated, (int, float))
                    and isinstance(budget, (int, float))
                    and budget
                ):
                    budget_usage.append(float(estimated) / float(budget))
                if event.payload.get("truncated") or event.payload.get("dropped_messages"):
                    truncated_context += 1
            elif event.type == EventType.CONTEXT_ANCHOR_ADDED.value:
                anchors_added += 1
            elif event.type == EventType.CONTEXT_ANCHOR_REMOVED.value:
                anchors_removed += 1
        anchor_clause = "WHERE active = 1"
        anchor_params: list[Any] = []
        if session_id:
            anchor_clause += " AND session_id = ?"
            anchor_params.append(session_id)
        anchors_active = int(
            (
                await self.db.run(
                    lambda conn: conn.execute(
                        f"SELECT COUNT(*) FROM context_anchors {anchor_clause}", anchor_params
                    ).fetchone()
                )
            )[0]
        )
        return {
            "builds": builds,
            "compactions": len(rows),
            "levels": levels,
            "before_tokens": before,
            "after_tokens": after,
            "saved_tokens": max(0, before - after),
            "artifacts": artifacts,
            "failures": failures,
            "resets": resets,
            "truncated_builds": truncated_context,
            "budget_usage": Distribution.of([value * 100 for value in budget_usage]).to_dict(),
            "anchors": {
                "active": anchors_active,
                "added": anchors_added,
                "removed": anchors_removed,
            },
        }

    async def _recovery_stats(
        self,
        since: datetime,
        until: datetime,
        session_id: str | None,
        events: list[EventFact],
    ) -> dict[str, Any]:
        counts = {
            "started": 0,
            "completed": 0,
            "failed": 0,
            "tools_reconciled": 0,
            "processes_terminated": 0,
            "claims_released": 0,
            "checkpoints_created": 0,
            "checkpoints_invalid": 0,
            "checkpoints_pruned": 0,
            "forks": 0,
            "claim_conflicts": 0,
            "task_retries": 0,
            "task_plan_revisions": 0,
        }
        mapping = {
            EventType.RECOVERY_STARTED.value: "started",
            EventType.RECOVERY_COMPLETED.value: "completed",
            EventType.RECOVERY_FAILED.value: "failed",
            EventType.RECOVERY_TOOL_RECONCILED.value: "tools_reconciled",
            EventType.RECOVERY_PROCESS_TERMINATED.value: "processes_terminated",
            EventType.RECOVERY_CLAIMS_RELEASED.value: "claims_released",
            EventType.CHECKPOINT_CREATED.value: "checkpoints_created",
            EventType.CHECKPOINT_INVALID.value: "checkpoints_invalid",
            EventType.CHECKPOINT_PRUNED.value: "checkpoints_pruned",
            EventType.SESSION_FORKED.value: "forks",
            EventType.CLAIM_CONFLICT.value: "claim_conflicts",
            EventType.TASK_RETRY_SCHEDULED.value: "task_retries",
            EventType.TASK_PLAN_REVISED.value: "task_plan_revisions",
        }
        for event in events:
            key = mapping.get(event.type)
            if key:
                counts[key] += 1
        clauses = ["created_at >= ?", "created_at <= ?"]
        params: list[Any] = [since.isoformat(), until.isoformat()]
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        checkpoint_count = int(
            (
                await self.db.run(
                    lambda conn: conn.execute(
                        f"SELECT COUNT(*) FROM checkpoints WHERE {' AND '.join(clauses)}",
                        params,
                    ).fetchone()
                )
            )[0]
        )
        unknown_tools = int(
            (
                await self.db.run(
                    lambda conn: conn.execute(
                        "SELECT COUNT(*) FROM tool_calls WHERE status = 'unknown' "
                        + ("AND session_id = ?" if session_id else ""),
                        [session_id] if session_id else [],
                    ).fetchone()
                )
            )[0]
        )
        running_attempts = int(
            (
                await self.db.run(
                    lambda conn: conn.execute(
                        "SELECT COUNT(*) FROM task_attempts WHERE status = 'running' "
                        + ("AND session_id = ?" if session_id else ""),
                        [session_id] if session_id else [],
                    ).fetchone()
                )
            )[0]
        )
        return {
            **counts,
            "checkpoints_in_window": checkpoint_count,
            "unknown_tool_calls": unknown_tools,
            "running_task_attempts": running_attempts,
            "fence_rejections": fence_rejections_total(),
            "success_rate": round(
                counts["completed"] / max(1, counts["completed"] + counts["failed"]),
                4,
            ),
        }

    async def _system_stats(
        self, since: datetime, until: datetime, session_id: str | None
    ) -> dict[str, Any]:
        session_clause = "WHERE id = ?" if session_id else ""
        params = [session_id] if session_id else []
        session_status = {
            str(row[0]): int(row[1])
            for row in await self.db.run(
                lambda conn: conn.execute(
                    f"SELECT status, COUNT(*) FROM sessions {session_clause} GROUP BY status",
                    params,
                ).fetchall()
            )
        }
        running_tools = int(
            (
                await self.db.run(
                    lambda conn: conn.execute(
                        "SELECT COUNT(*) FROM tool_calls WHERE status = 'running' "
                        + ("AND session_id = ?" if session_id else ""),
                        params,
                    ).fetchone()
                )
            )[0]
        )
        active_claims = int(
            (
                await self.db.run(
                    lambda conn: conn.execute(
                        "SELECT COUNT(*) FROM file_claims "
                        "WHERE expires_at > ?" + (" AND session_id = ?" if session_id else ""),
                        [self.clock.now().isoformat(), *params],
                    ).fetchone()
                )
            )[0]
        )
        trace_stats = await self.db.run(
            lambda conn: (
                int(conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]),
                int(
                    conn.execute("SELECT COUNT(*) FROM traces WHERE status = 'running'").fetchone()[
                        0
                    ]
                ),
                int(conn.execute("SELECT COUNT(*) FROM traces WHERE incomplete = 1").fetchone()[0]),
            )
        )
        benchmark_count = int(
            (
                await self.db.run(
                    lambda conn: conn.execute("SELECT COUNT(*) FROM benchmark_runs").fetchone()
                )
            )[0]
        )
        pragmas = await self.db.run(_pragma_snapshot)
        data_dir = self.db.path.parent
        return {
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "sqlite_version": sqlite3.sqlite_version,
            "process_memory_bytes": process_memory_bytes(),
            "cpu_time_s": cpu_time_seconds(),
            "data_dir": str(data_dir),
            "database_bytes": file_size(self.db.path),
            "wal_bytes": file_size(str(self.db.path) + "-wal"),
            "disk": disk_usage(data_dir),
            "sqlite": {
                **pragmas,
                "sqlite_runtime": sqlite3.sqlite_version,
            },
            "sessions": session_status,
            "running_tools": running_tools,
            "active_claims": active_claims,
            "traces": {
                "total": trace_stats[0],
                "running": trace_stats[1],
                "incomplete": trace_stats[2],
            },
            "benchmarks": benchmark_count,
            "window_seconds": max(0, int((until - since).total_seconds())),
        }


def _run_stats(events: list[EventFact]) -> dict[str, Any]:
    accumulators: dict[str, _RunAccumulator] = {}
    for event in events:
        if event.run_id is None:
            continue
        if event.type == EventType.RUN_STARTED.value:
            acc = accumulators.setdefault(
                event.run_id,
                _RunAccumulator(run_id=event.run_id, session_id=event.session_id),
            )
            acc.started_at = event.ts
            acc.kind = "request"
            acc.model = str(event.payload.get("model") or "")
            acc.provider = str(event.payload.get("provider") or "")
            acc.task_id = (
                str(event.payload.get("task_id")) if event.payload.get("task_id") else None
            )
        elif event.type in (
            EventType.RUN_COMPLETED.value,
            EventType.RUN_CANCELLED.value,
            EventType.RUN_FAILED.value,
        ):
            acc = accumulators.setdefault(
                event.run_id,
                _RunAccumulator(run_id=event.run_id, session_id=event.session_id),
            )
            acc.ended_at = event.ts
            acc.status = str(event.payload.get("status") or _run_status_from_type(event.type))
        elif event.type in (
            EventType.TASK_RUN_STARTED.value,
            EventType.TASK_RUN_COMPLETED.value,
            EventType.TASK_RUN_FAILED.value,
        ):
            acc = accumulators.setdefault(
                event.run_id,
                _RunAccumulator(run_id=event.run_id, session_id=event.session_id, kind="plan"),
            )
            if event.type == EventType.TASK_RUN_STARTED.value:
                acc.started_at = event.ts
                acc.kind = "plan"
            else:
                acc.ended_at = event.ts
                acc.status = str(
                    event.payload.get("status")
                    or ("failed" if event.type == EventType.TASK_RUN_FAILED.value else "completed")
                )
    request_runs = [acc for acc in accumulators.values() if acc.kind == "request"]
    plan_runs = [acc for acc in accumulators.values() if acc.kind == "plan"]
    all_runs = request_runs + plan_runs
    statuses: dict[str, int] = {}
    durations: list[float] = []
    for acc in all_runs:
        statuses[acc.status] = statuses.get(acc.status, 0) + 1
        duration = acc.duration_ms
        if duration is not None:
            durations.append(duration)
    completed = statuses.get("completed", 0)
    terminal = sum(
        statuses.get(status, 0)
        for status in (
            "completed",
            "failed",
            "cancelled",
            "max_turns",
            "max_tool_calls",
            "no_progress",
            "timeout",
            "context_overflow",
            "blocked",
        )
    )
    request_statuses: dict[str, int] = {}
    for acc in request_runs:
        request_statuses[acc.status] = request_statuses.get(acc.status, 0) + 1
    plan_statuses: dict[str, int] = {}
    for acc in plan_runs:
        plan_statuses[acc.status] = plan_statuses.get(acc.status, 0) + 1
    return {
        "total": len(all_runs),
        "request_runs": len(request_runs),
        "plan_runs": len(plan_runs),
        "status_counts": statuses,
        "request_status_counts": request_statuses,
        "plan_status_counts": plan_statuses,
        "completed": completed,
        "failed": statuses.get("failed", 0),
        "cancelled": statuses.get("cancelled", 0),
        "success_rate": round(completed / terminal, 4) if terminal else 0.0,
        "duration_ms": Distribution.of(durations).to_dict(),
    }


def _provider_stats(events: list[EventFact]) -> dict[str, Any]:
    result = {
        "retries": 0,
        "model_retries": 0,
        "provider_retries": 0,
        "fallbacks": 0,
        "circuit_opens": 0,
        "failures": 0,
    }
    for event in events:
        if event.type == EventType.MODEL_RETRY.value:
            result["model_retries"] += 1
            result["retries"] += 1
        elif event.type == EventType.PROVIDER_RETRY.value:
            result["provider_retries"] += 1
            result["retries"] += 1
        elif event.type == EventType.PROVIDER_FALLBACK.value:
            result["fallbacks"] += 1
        elif event.type == EventType.PROVIDER_CIRCUIT_OPEN.value:
            result["circuit_opens"] += 1
        elif event.type == EventType.PROVIDER_FAILED.value:
            result["failures"] += 1
    return result


def _run_status_from_type(event_type: str) -> str:
    if event_type == EventType.RUN_FAILED.value:
        return "failed"
    if event_type == EventType.RUN_CANCELLED.value:
        return "cancelled"
    return "completed"


def _estimate_cost(
    price: Any,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_write: int,
) -> float:
    uncached_input = max(0, input_tokens - cache_read - cache_write)
    return (
        uncached_input / 1_000_000 * float(price.input_per_million)
        + cache_read / 1_000_000 * float(price.cache_read_per_million)
        + cache_write / 1_000_000 * float(price.cache_write_per_million)
        + output_tokens / 1_000_000 * float(price.output_per_million)
    )


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _loads(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _pragma_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    journal = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
    version_row = conn.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
    return {
        "schema_version": int(version_row[0]) if version_row else 0,
        "page_count": page_count,
        "page_size": page_size,
        "free_pages": freelist,
        "journal_mode": journal,
        "database_bytes": page_count * page_size,
        "freelist_bytes": freelist * page_size,
        "path": str(conn.execute("PRAGMA database_list").fetchone()[2] or ""),
    }
