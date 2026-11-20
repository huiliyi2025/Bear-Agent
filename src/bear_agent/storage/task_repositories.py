"""SQLite persistence for Task / Plan / Todo state.

The service owns state-machine decisions; this repository owns SQL and row
mapping. Multi-row mutations are performed by the service inside one
``BEGIN IMMEDIATE`` transaction using the ``_sync`` helpers here.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from bear_agent.storage.database import Database
from bear_agent.storage.repositories import _iso, _parse_dt, utcnow
from bear_agent.tasks.models import (
    AttemptStatus,
    PlanRecord,
    PlanRevisionRecord,
    PlanStatus,
    TaskAttemptRecord,
    TaskRecord,
    TaskStatus,
    VerificationMode,
)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _load_dict(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _row_to_plan(row: sqlite3.Row) -> PlanRecord:
    return PlanRecord(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        goal=str(row["goal"]),
        status=PlanStatus(row["status"]),
        version=int(row["version"]),
        created_at=_parse_dt(row["created_at"]) or utcnow(),
        updated_at=_parse_dt(row["updated_at"]) or utcnow(),
    )


def _row_to_task(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        id=str(row["id"]),
        plan_id=str(row["plan_id"]),
        session_id=str(row["session_id"]),
        task_key=str(row["task_key"]),
        title=str(row["title"]),
        description=str(row["description"]),
        acceptance_criteria=str(row["acceptance_criteria"] or ""),
        status=TaskStatus(row["status"]),
        priority=int(row["priority"]),
        order_index=int(row["order_index"]),
        attempt_count=int(row["attempt_count"]),
        max_attempts=int(row["max_attempts"]),
        verification=VerificationMode(row["verification"]),
        active=bool(row["active"]),
        result_summary=row["result_summary"],
        last_error=row["last_error"],
        created_at=_parse_dt(row["created_at"]) or utcnow(),
        updated_at=_parse_dt(row["updated_at"]) or utcnow(),
        started_at=_parse_dt(row["started_at"]),
        finished_at=_parse_dt(row["finished_at"]),
        metadata=_load_dict(row["metadata_json"]),
    )


def _row_to_attempt(row: sqlite3.Row) -> TaskAttemptRecord:
    evidence: list[str] = []
    try:
        raw_evidence = json.loads(row["evidence_json"] or "[]")
        if isinstance(raw_evidence, list):
            evidence = [str(item) for item in raw_evidence]
    except (TypeError, ValueError):
        evidence = []
    return TaskAttemptRecord(
        id=str(row["id"]),
        task_id=str(row["task_id"]),
        session_id=str(row["session_id"]),
        attempt_no=int(row["attempt_no"]),
        run_id=row["run_id"],
        status=AttemptStatus(row["status"]),
        started_at=_parse_dt(row["started_at"]) or utcnow(),
        finished_at=_parse_dt(row["finished_at"]),
        completion_summary=row["completion_summary"],
        evidence=evidence,
        verification=_load_dict(row["verification_json"]),
        error_kind=row["error_kind"],
        error_message=row["error_message"],
        metadata=_load_dict(row["metadata_json"]),
    )


def _row_to_revision(row: sqlite3.Row) -> PlanRevisionRecord:
    return PlanRevisionRecord(
        id=str(row["id"]),
        plan_id=str(row["plan_id"]),
        session_id=str(row["session_id"]),
        version=int(row["version"]),
        goal=str(row["goal"]),
        reason=row["reason"],
        source=row["source"],
        snapshot=_load_dict(row["snapshot_json"]),
        created_by_run_id=row["created_by_run_id"],
        created_at=_parse_dt(row["created_at"]) or utcnow(),
    )


class TaskRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ reads

    async def get_plan(self, session_id: str) -> PlanRecord | None:
        return await self.db.run(lambda conn: self._plan_for_session(conn, session_id))

    def _plan_for_session(self, conn: sqlite3.Connection, session_id: str) -> PlanRecord | None:
        row = conn.execute("SELECT * FROM plans WHERE session_id = ?", (session_id,)).fetchone()
        return _row_to_plan(row) if row else None

    async def get_plan_by_id(self, plan_id: str) -> PlanRecord | None:
        return await self.db.run(lambda conn: self._plan_by_id(conn, plan_id))

    def _plan_by_id(self, conn: sqlite3.Connection, plan_id: str) -> PlanRecord | None:
        row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        return _row_to_plan(row) if row else None

    async def get_task(self, task_id: str) -> TaskRecord | None:
        return await self.db.run(lambda conn: self._task(conn, task_id))

    def _task(self, conn: sqlite3.Connection, task_id: str) -> TaskRecord | None:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    async def list_tasks(self, plan_id: str, *, active_only: bool = True) -> list[TaskRecord]:
        return await self.db.run(lambda conn: self._tasks(conn, plan_id, active_only))

    def _tasks(self, conn: sqlite3.Connection, plan_id: str, active_only: bool) -> list[TaskRecord]:
        query = "SELECT * FROM tasks WHERE plan_id = ?"
        if active_only:
            query += " AND active = 1"
        query += " ORDER BY order_index ASC, priority DESC, created_at ASC"
        rows = conn.execute(query, (plan_id,)).fetchall()
        return [_row_to_task(row) for row in rows]

    async def list_tasks_for_session(
        self, session_id: str, *, active_only: bool = False
    ) -> list[TaskRecord]:
        return await self.db.run(
            lambda conn: self._tasks_for_session(conn, session_id, active_only)
        )

    def _tasks_for_session(
        self, conn: sqlite3.Connection, session_id: str, active_only: bool
    ) -> list[TaskRecord]:
        query = "SELECT * FROM tasks WHERE session_id = ?"
        if active_only:
            query += " AND active = 1"
        query += " ORDER BY order_index ASC, priority DESC, created_at ASC"
        rows = conn.execute(query, (session_id,)).fetchall()
        return [_row_to_task(row) for row in rows]

    async def dependencies(self, plan_id: str) -> dict[str, list[str]]:
        return await self.db.run(lambda conn: self._dependencies(conn, plan_id))

    def _dependencies(self, conn: sqlite3.Connection, plan_id: str) -> dict[str, list[str]]:
        rows = conn.execute(
            "SELECT task_id, depends_on_task_id FROM task_dependencies WHERE plan_id = ?",
            (plan_id,),
        ).fetchall()
        result: dict[str, list[str]] = {}
        for row in rows:
            result.setdefault(str(row["task_id"]), []).append(str(row["depends_on_task_id"]))
        return result

    async def dependency_keys(self, task_id: str) -> list[str]:
        return await self.db.run(lambda conn: self._dependency_keys(conn, task_id))

    def _dependency_keys(self, conn: sqlite3.Connection, task_id: str) -> list[str]:
        rows = conn.execute(
            """
            SELECT t.task_key
              FROM task_dependencies d
              JOIN tasks t ON t.id = d.depends_on_task_id
             WHERE d.task_id = ?
             ORDER BY t.order_index ASC, t.task_key ASC
            """,
            (task_id,),
        ).fetchall()
        return [str(row["task_key"]) for row in rows]

    async def attempts(self, task_id: str) -> list[TaskAttemptRecord]:
        return await self.db.run(lambda conn: self._attempts(conn, task_id))

    def _attempts(self, conn: sqlite3.Connection, task_id: str) -> list[TaskAttemptRecord]:
        rows = conn.execute(
            "SELECT * FROM task_attempts WHERE task_id = ? ORDER BY attempt_no ASC",
            (task_id,),
        ).fetchall()
        return [_row_to_attempt(row) for row in rows]

    async def active_attempt(self, task_id: str) -> TaskAttemptRecord | None:
        return await self.db.run(lambda conn: self._active_attempt(conn, task_id))

    def _active_attempt(self, conn: sqlite3.Connection, task_id: str) -> TaskAttemptRecord | None:
        row = conn.execute(
            "SELECT * FROM task_attempts WHERE task_id = ? AND status = 'running' "
            "ORDER BY attempt_no DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return _row_to_attempt(row) if row else None

    async def running_attempts(self, session_id: str) -> list[TaskAttemptRecord]:
        return await self.db.run(lambda conn: self._running_attempts(conn, session_id))

    def _running_attempts(
        self, conn: sqlite3.Connection, session_id: str
    ) -> list[TaskAttemptRecord]:
        rows = conn.execute(
            "SELECT * FROM task_attempts WHERE session_id = ? AND status = 'running' "
            "ORDER BY started_at ASC",
            (session_id,),
        ).fetchall()
        return [_row_to_attempt(row) for row in rows]

    async def revisions(self, plan_id: str) -> list[PlanRevisionRecord]:
        return await self.db.run(lambda conn: self._revisions(conn, plan_id))

    def _revisions(self, conn: sqlite3.Connection, plan_id: str) -> list[PlanRevisionRecord]:
        rows = conn.execute(
            "SELECT * FROM plan_revisions WHERE plan_id = ? ORDER BY version ASC",
            (plan_id,),
        ).fetchall()
        return [_row_to_revision(row) for row in rows]

    def _max_attempt_no(self, conn: sqlite3.Connection, task_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(attempt_no), 0) AS n FROM task_attempts WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return int(row["n"])

    def _revision_count(self, conn: sqlite3.Connection, plan_id: str) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM plan_revisions WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------ sync writes

    def insert_plan(self, conn: sqlite3.Connection, plan: PlanRecord) -> PlanRecord:
        conn.execute(
            """
            INSERT INTO plans (id, session_id, goal, status, version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan.id,
                plan.session_id,
                plan.goal,
                plan.status.value,
                plan.version,
                _iso(plan.created_at),
                _iso(plan.updated_at),
            ),
        )
        return plan

    def update_plan(self, conn: sqlite3.Connection, plan: PlanRecord) -> None:
        conn.execute(
            "UPDATE plans SET goal = ?, status = ?, version = ?, updated_at = ? WHERE id = ?",
            (plan.goal, plan.status.value, plan.version, _iso(plan.updated_at), plan.id),
        )

    def insert_task(self, conn: sqlite3.Connection, task: TaskRecord) -> TaskRecord:
        conn.execute(
            """
            INSERT INTO tasks (
                id, plan_id, session_id, task_key, title, description,
                acceptance_criteria, status, priority, order_index, attempt_count,
                max_attempts, verification, active, result_summary, last_error,
                created_at, updated_at, started_at, finished_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.id,
                task.plan_id,
                task.session_id,
                task.task_key,
                task.title,
                task.description,
                task.acceptance_criteria,
                task.status.value,
                task.priority,
                task.order_index,
                task.attempt_count,
                task.max_attempts,
                task.verification.value,
                1 if task.active else 0,
                task.result_summary,
                task.last_error,
                _iso(task.created_at),
                _iso(task.updated_at),
                _iso(task.started_at) if task.started_at else None,
                _iso(task.finished_at) if task.finished_at else None,
                _json(task.metadata),
            ),
        )
        return task

    def update_task(self, conn: sqlite3.Connection, task: TaskRecord) -> None:
        conn.execute(
            """
            UPDATE tasks SET
                title = ?, description = ?, acceptance_criteria = ?, status = ?,
                priority = ?, order_index = ?, attempt_count = ?, max_attempts = ?,
                verification = ?, active = ?, result_summary = ?, last_error = ?,
                updated_at = ?, started_at = ?, finished_at = ?, metadata_json = ?
             WHERE id = ?
            """,
            (
                task.title,
                task.description,
                task.acceptance_criteria,
                task.status.value,
                task.priority,
                task.order_index,
                task.attempt_count,
                task.max_attempts,
                task.verification.value,
                1 if task.active else 0,
                task.result_summary,
                task.last_error,
                _iso(task.updated_at),
                _iso(task.started_at) if task.started_at else None,
                _iso(task.finished_at) if task.finished_at else None,
                _json(task.metadata),
                task.id,
            ),
        )

    def replace_dependencies(
        self,
        conn: sqlite3.Connection,
        plan_id: str,
        task_id: str,
        depends_on_ids: list[str],
    ) -> None:
        conn.execute("DELETE FROM task_dependencies WHERE task_id = ?", (task_id,))
        conn.executemany(
            "INSERT INTO task_dependencies (plan_id, task_id, depends_on_task_id) VALUES (?, ?, ?)",
            [(plan_id, task_id, dependency) for dependency in depends_on_ids],
        )

    def insert_attempt(
        self, conn: sqlite3.Connection, attempt: TaskAttemptRecord
    ) -> TaskAttemptRecord:
        conn.execute(
            """
            INSERT INTO task_attempts (
                id, task_id, session_id, attempt_no, run_id, status, started_at,
                finished_at, completion_summary, evidence_json, verification_json,
                error_kind, error_message, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt.id,
                attempt.task_id,
                attempt.session_id,
                attempt.attempt_no,
                attempt.run_id,
                attempt.status.value,
                _iso(attempt.started_at),
                _iso(attempt.finished_at) if attempt.finished_at else None,
                attempt.completion_summary,
                _json(attempt.evidence),
                _json(attempt.verification),
                attempt.error_kind,
                attempt.error_message,
                _json(attempt.metadata),
            ),
        )
        return attempt

    def update_attempt(self, conn: sqlite3.Connection, attempt: TaskAttemptRecord) -> None:
        conn.execute(
            """
            UPDATE task_attempts SET
                run_id = ?, status = ?, finished_at = ?, completion_summary = ?,
                evidence_json = ?, verification_json = ?, error_kind = ?,
                error_message = ?, metadata_json = ?
             WHERE id = ?
            """,
            (
                attempt.run_id,
                attempt.status.value,
                _iso(attempt.finished_at) if attempt.finished_at else None,
                attempt.completion_summary,
                _json(attempt.evidence),
                _json(attempt.verification),
                attempt.error_kind,
                attempt.error_message,
                _json(attempt.metadata),
                attempt.id,
            ),
        )

    def insert_revision(
        self, conn: sqlite3.Connection, revision: PlanRevisionRecord
    ) -> PlanRevisionRecord:
        conn.execute(
            """
            INSERT INTO plan_revisions (
                id, plan_id, session_id, version, goal, reason, source,
                snapshot_json, created_by_run_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision.id,
                revision.plan_id,
                revision.session_id,
                revision.version,
                revision.goal,
                revision.reason,
                revision.source,
                _json(revision.snapshot),
                revision.created_by_run_id,
                _iso(revision.created_at),
            ),
        )
        return revision

    # -------------------------------------------------------------- snapshots

    def snapshot(self, conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
        plan = self._plan_for_session(conn, session_id)
        if plan is None:
            return {"plan": None, "tasks": [], "dependencies": {}, "attempts": []}
        tasks = self._tasks(conn, plan.id, active_only=False)
        dependencies = self._dependencies(conn, plan.id)
        attempts = conn.execute(
            "SELECT * FROM task_attempts WHERE session_id = ? "
            "ORDER BY started_at DESC, attempt_no DESC LIMIT ?",
            (session_id, 200),
        ).fetchall()
        return {
            "plan": plan.model_dump(mode="json"),
            "tasks": [task.model_dump(mode="json") for task in tasks],
            "dependencies": dependencies,
            "attempts": [dict(row) for row in attempts],
        }

    def snapshot_for_plan(self, conn: sqlite3.Connection, plan: PlanRecord) -> dict[str, Any]:
        tasks = self._tasks(conn, plan.id, active_only=False)
        dependencies = self._dependencies(conn, plan.id)
        return {
            "plan": plan.model_dump(mode="json"),
            "tasks": [task.model_dump(mode="json") for task in tasks],
            "dependencies": dependencies,
        }
