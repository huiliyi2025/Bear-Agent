"""Trace persistence.

Trace rows are diagnostics, not security records. They live in their own tables
so querying them never competes with the transcript/event write contract, and
they are written without a session lease: losing a lease must still leave a
complete diagnostic record of the failure.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from bear_agent.storage.database import Database, transaction


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


class TraceRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def insert_trace(
        self,
        *,
        trace_id: str,
        session_id: str,
        run_id: str | None,
        task_id: str | None,
        name: str,
        kind: str,
        status: str,
        started_at: datetime,
        attributes: dict[str, Any],
        sampled: bool,
        created_at: datetime,
    ) -> None:
        await self.db.run(
            lambda conn: self._insert_trace(
                conn,
                trace_id=trace_id,
                session_id=session_id,
                run_id=run_id,
                task_id=task_id,
                name=name,
                kind=kind,
                status=status,
                started_at=started_at,
                attributes=attributes,
                sampled=sampled,
                created_at=created_at,
            )
        )

    def _insert_trace(
        self,
        conn: sqlite3.Connection,
        *,
        trace_id: str,
        session_id: str,
        run_id: str | None,
        task_id: str | None,
        name: str,
        kind: str,
        status: str,
        started_at: datetime,
        attributes: dict[str, Any],
        sampled: bool,
        created_at: datetime,
    ) -> None:
        conn.execute(
            """
            INSERT INTO traces (
                trace_id, session_id, run_id, task_id, name, kind, status,
                started_at, ended_at, duration_ms, attributes_json,
                error_type, error_message, span_count, dropped_spans,
                incomplete, sampled, schema_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL, NULL, 0, 0, 0, ?, 1, ?)
            """,
            (
                trace_id,
                session_id,
                run_id,
                task_id,
                name,
                kind,
                status,
                _iso(started_at),
                _json(attributes),
                1 if sampled else 0,
                _iso(created_at),
            ),
        )

    async def finish_trace(
        self,
        trace_id: str,
        *,
        status: str,
        ended_at: datetime,
        duration_ms: int,
        attributes: dict[str, Any],
        error_type: str | None,
        error_message: str | None,
        span_count: int,
        dropped_spans: int,
        incomplete: bool,
    ) -> None:
        await self.db.run(
            lambda conn: self._finish_trace(
                conn,
                trace_id,
                status=status,
                ended_at=ended_at,
                duration_ms=duration_ms,
                attributes=attributes,
                error_type=error_type,
                error_message=error_message,
                span_count=span_count,
                dropped_spans=dropped_spans,
                incomplete=incomplete,
            )
        )

    def _finish_trace(
        self,
        conn: sqlite3.Connection,
        trace_id: str,
        *,
        status: str,
        ended_at: datetime,
        duration_ms: int,
        attributes: dict[str, Any],
        error_type: str | None,
        error_message: str | None,
        span_count: int,
        dropped_spans: int,
        incomplete: bool,
    ) -> None:
        conn.execute(
            """
            UPDATE traces SET
                status = ?, ended_at = ?, duration_ms = ?, attributes_json = ?,
                error_type = ?, error_message = ?, span_count = ?,
                dropped_spans = ?, incomplete = ?
             WHERE trace_id = ?
            """,
            (
                status,
                _iso(ended_at),
                duration_ms,
                _json(attributes),
                error_type,
                error_message,
                span_count,
                dropped_spans,
                1 if incomplete else 0,
                trace_id,
            ),
        )

    async def insert_spans(self, spans: list[dict[str, Any]]) -> None:
        if not spans:
            return
        await self.db.run(lambda conn: self._insert_spans(conn, spans))

    def _insert_spans(self, conn: sqlite3.Connection, spans: list[dict[str, Any]]) -> None:
        with transaction(conn):
            conn.executemany(
                """
                INSERT OR REPLACE INTO spans (
                    span_id, trace_id, parent_span_id, name, kind, status,
                    started_at, ended_at, duration_ms, attributes_json,
                    error_type, error_message, sequence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        span["span_id"],
                        span["trace_id"],
                        span["parent_span_id"],
                        span["name"],
                        span["kind"],
                        span["status"],
                        span["started_at"],
                        span["ended_at"],
                        span["duration_ms"],
                        span["attributes_json"],
                        span["error_type"],
                        span["error_message"],
                        span["sequence"],
                        span["created_at"],
                    )
                    for span in spans
                ],
            )

    async def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        return await self.db.run(lambda conn: self._get_trace(conn, trace_id))

    def _get_trace(self, conn: sqlite3.Connection, trace_id: str) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM traces WHERE trace_id = ?", (trace_id,)).fetchone()
        return _trace_row(row) if row else None

    async def list_traces(
        self,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        status: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return await self.db.run(
            lambda conn: self._list_traces(
                conn,
                session_id=session_id,
                run_id=run_id,
                task_id=task_id,
                status=status,
                since=since,
                until=until,
                limit=limit,
                offset=offset,
            )
        )

    def _list_traces(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str | None,
        run_id: str | None,
        task_id: str | None,
        status: str | None,
        since: datetime | None,
        until: datetime | None,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("session_id", session_id),
            ("run_id", run_id),
            ("task_id", task_id),
            ("status", status),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if since is not None:
            clauses.append("started_at >= ?")
            params.append(_iso(since))
        if until is not None:
            clauses.append("started_at <= ?")
            params.append(_iso(until))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(limit, 1000)))
        params.append(max(0, offset))
        rows = conn.execute(
            f"SELECT * FROM traces{where} ORDER BY started_at DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [_trace_row(row) for row in rows]

    async def spans(self, trace_id: str) -> list[dict[str, Any]]:
        return await self.db.run(lambda conn: self._spans(conn, trace_id))

    def _spans(self, conn: sqlite3.Connection, trace_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM spans WHERE trace_id = ? ORDER BY sequence ASC", (trace_id,)
        ).fetchall()
        return [_span_row(row) for row in rows]

    async def events(self, trace_id: str, *, limit: int = 2000) -> list[dict[str, Any]]:
        return await self.db.run(lambda conn: self._events(conn, trace_id, limit))

    def _events(self, conn: sqlite3.Connection, trace_id: str, limit: int) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT e.event_id, e.session_id, e.seq, e.type, e.run_id, e.turn_id,
                   e.span_id, e.parent_span_id, e.payload_json, e.created_at
              FROM events e
              JOIN spans s ON s.span_id = e.span_id
             WHERE s.trace_id = ?
             ORDER BY e.seq ASC
             LIMIT ?
            """,
            (trace_id, max(1, min(limit, 20_000))),
        ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "session_id": row["session_id"],
                "seq": int(row["seq"]),
                "type": row["type"],
                "run_id": row["run_id"],
                "turn_id": row["turn_id"],
                "span_id": row["span_id"],
                "parent_span_id": row["parent_span_id"],
                "payload": _load_json(row["payload_json"]),
                "ts": row["created_at"],
            }
            for row in rows
        ]

    async def mark_incomplete(self, cutoff: datetime) -> int:
        return await self.db.run(lambda conn: self._mark_incomplete(conn, cutoff))

    def _mark_incomplete(self, conn: sqlite3.Connection, cutoff: datetime) -> int:
        now = datetime.now(UTC)
        cursor = conn.execute(
            """
            UPDATE traces
               SET status = 'incomplete', incomplete = 1, ended_at = ?,
                   duration_ms = CAST(
                       (julianday(?) - julianday(started_at)) * 86400000 AS INTEGER
                   )
             WHERE status = 'running' AND started_at < ?
               AND NOT EXISTS (
                   SELECT 1 FROM sessions s
                    WHERE s.id = traces.session_id
                      AND s.lease_owner IS NOT NULL
                      AND s.lease_expires_at IS NOT NULL
                      AND s.lease_expires_at > ?
               )
            """,
            (_iso(now), _iso(now), _iso(cutoff), _iso(now)),
        )
        return int(cursor.rowcount)

    async def prune(self, *, cutoff: datetime | None, max_traces: int) -> int:
        return await self.db.run(lambda conn: self._prune(conn, cutoff, max_traces))

    def _prune(self, conn: sqlite3.Connection, cutoff: datetime | None, max_traces: int) -> int:
        deleted = 0
        with transaction(conn):
            if cutoff is not None:
                cursor = conn.execute("DELETE FROM traces WHERE started_at < ?", (_iso(cutoff),))
                deleted += int(cursor.rowcount)
            if max_traces > 0:
                cursor = conn.execute(
                    """
                    DELETE FROM traces WHERE trace_id IN (
                        SELECT trace_id FROM traces
                         WHERE status != 'running'
                         ORDER BY started_at DESC
                         LIMIT -1 OFFSET ?
                    )
                    """,
                    (max_traces,),
                )
                deleted += int(cursor.rowcount)
        return deleted

    async def diagnostics(self, cutoff: datetime) -> dict[str, Any]:
        return await self.db.run(lambda conn: self._diagnostics(conn, cutoff))

    def _diagnostics(self, conn: sqlite3.Connection, cutoff: datetime) -> dict[str, Any]:
        orphan_spans = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM spans s
                 LEFT JOIN spans p ON p.span_id = s.parent_span_id
                 WHERE s.parent_span_id IS NOT NULL AND p.span_id IS NULL
                """
            ).fetchone()[0]
        )
        stale_running = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM traces t
                 WHERE t.status = 'running' AND t.started_at < ?
                   AND NOT EXISTS (
                       SELECT 1 FROM sessions s
                        WHERE s.id = t.session_id
                          AND s.lease_owner IS NOT NULL
                          AND s.lease_expires_at IS NOT NULL
                          AND s.lease_expires_at > ?
                   )
                """,
                (_iso(cutoff), _iso(datetime.now(UTC))),
            ).fetchone()[0]
        )
        try:
            invalid_json = int(
                conn.execute(
                    "SELECT COUNT(*) FROM spans WHERE json_valid(attributes_json) = 0"
                ).fetchone()[0]
            )
        except sqlite3.OperationalError:
            # JSON1 is built into supported SQLite builds, but a stripped build
            # must not make diagnostics fail.
            invalid_json = -1
        missing_end = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM spans s
                  JOIN traces t ON t.trace_id = s.trace_id
                 WHERE (s.ended_at IS NULL OR s.duration_ms IS NULL)
                   AND t.status != 'running'
                """
            ).fetchone()[0]
        )
        return {
            "orphan_spans": orphan_spans,
            "stale_running": stale_running,
            "invalid_attribute_json": invalid_json,
            "spans_missing_end": missing_end,
        }

    async def stats(self) -> dict[str, Any]:
        return await self.db.run(self._stats)

    def _stats(self, conn: sqlite3.Connection) -> dict[str, Any]:
        total = int(conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0])
        spans = int(conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0])
        running = int(
            conn.execute("SELECT COUNT(*) FROM traces WHERE status = 'running'").fetchone()[0]
        )
        incomplete = int(
            conn.execute("SELECT COUNT(*) FROM traces WHERE incomplete = 1").fetchone()[0]
        )
        oldest = conn.execute("SELECT MIN(started_at) FROM traces").fetchone()[0]
        newest = conn.execute("SELECT MAX(started_at) FROM traces").fetchone()[0]
        return {
            "traces": total,
            "spans": spans,
            "running": running,
            "incomplete": incomplete,
            "oldest": oldest,
            "newest": newest,
        }


class BenchmarkRepository:
    """Structured benchmark results; one row per run, indexed by scenario/time."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def insert(
        self,
        *,
        run_id: str,
        scenario_id: str,
        scenario_name: str,
        category: str,
        kind: str,
        status: str,
        started_at: datetime,
        finished_at: datetime,
        result: dict[str, Any],
        app_version: str,
        git_commit: str | None,
    ) -> None:
        await self.db.run(
            lambda conn: conn.execute(
                """
                INSERT INTO benchmark_runs (
                    id, scenario_id, scenario_name, category, kind, status,
                    started_at, finished_at, result_json, app_version,
                    git_commit, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    scenario_id,
                    scenario_name,
                    category,
                    kind,
                    status,
                    _iso(started_at),
                    _iso(finished_at),
                    _json(result),
                    app_version,
                    git_commit,
                    _iso(finished_at),
                ),
            )
        )

    async def get(self, run_id: str) -> dict[str, Any] | None:
        return await self.db.run(lambda conn: self._get(conn, run_id))

    def _get(self, conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM benchmark_runs WHERE id = ?", (run_id,)).fetchone()
        return _benchmark_row(row) if row else None

    async def list_runs(
        self,
        *,
        scenario_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return await self.db.run(
            lambda conn: self._list(conn, scenario_id=scenario_id, limit=limit, offset=offset)
        )

    def _list(
        self,
        conn: sqlite3.Connection,
        *,
        scenario_id: str | None,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        if scenario_id:
            rows = conn.execute(
                "SELECT * FROM benchmark_runs WHERE scenario_id = ? "
                "ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (scenario_id, max(1, min(limit, 500)), max(0, offset)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM benchmark_runs ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (max(1, min(limit, 500)), max(0, offset)),
            ).fetchall()
        return [_benchmark_row(row) for row in rows]

    async def latest(self, scenario_id: str | None = None) -> dict[str, Any] | None:
        rows = await self.list_runs(scenario_id=scenario_id, limit=1)
        return rows[0] if rows else None

    async def stats(self) -> dict[str, Any]:
        return await self.db.run(self._stats)

    def _stats(self, conn: sqlite3.Connection) -> dict[str, Any]:
        total = int(conn.execute("SELECT COUNT(*) FROM benchmark_runs").fetchone()[0])
        scenarios = int(
            conn.execute("SELECT COUNT(DISTINCT scenario_id) FROM benchmark_runs").fetchone()[0]
        )
        latest = conn.execute("SELECT MAX(started_at) FROM benchmark_runs").fetchone()[0]
        return {"runs": total, "scenarios": scenarios, "latest": latest}

    async def prune(self, *, cutoff: datetime, max_runs: int) -> int:
        return await self.db.run(lambda conn: self._prune(conn, cutoff, max_runs))

    def _prune(self, conn: sqlite3.Connection, cutoff: datetime, max_runs: int) -> int:
        with transaction(conn):
            cursor = conn.execute(
                "DELETE FROM benchmark_runs WHERE started_at < ?", (_iso(cutoff),)
            )
            deleted = int(cursor.rowcount)
            if max_runs > 0:
                cursor = conn.execute(
                    """
                    DELETE FROM benchmark_runs WHERE id IN (
                        SELECT id FROM benchmark_runs ORDER BY started_at DESC
                        LIMIT -1 OFFSET ?
                    )
                    """,
                    (max_runs,),
                )
                deleted += int(cursor.rowcount)
        return deleted


def _benchmark_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": row["id"],
        "scenario_id": row["scenario_id"],
        "scenario_name": row["scenario_name"],
        "category": row["category"],
        "kind": row["kind"],
        "status": row["status"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "result": _load_json(row["result_json"]),
        "app_version": row["app_version"],
        "git_commit": row["git_commit"],
    }


def _load_json(value: str | None) -> Any:
    if not value:
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def _trace_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "trace_id": row["trace_id"],
        "session_id": row["session_id"],
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "name": row["name"],
        "kind": row["kind"],
        "status": row["status"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "duration_ms": row["duration_ms"],
        "attributes": _load_json(row["attributes_json"]),
        "error_type": row["error_type"],
        "error_message": row["error_message"],
        "span_count": int(row["span_count"]),
        "dropped_spans": int(row["dropped_spans"]),
        "incomplete": bool(row["incomplete"]),
        "sampled": bool(row["sampled"]),
        "schema_version": int(row["schema_version"]),
        "created_at": row["created_at"],
    }


def _span_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "span_id": row["span_id"],
        "trace_id": row["trace_id"],
        "parent_span_id": row["parent_span_id"],
        "name": row["name"],
        "kind": row["kind"],
        "status": row["status"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "duration_ms": row["duration_ms"],
        "attributes": _load_json(row["attributes_json"]),
        "error_type": row["error_type"],
        "error_message": row["error_message"],
        "sequence": int(row["sequence"]),
        "created_at": row["created_at"],
    }
