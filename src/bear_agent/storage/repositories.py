"""SQLite repositories.

Repositories are thin, explicit and transactional. They deliberately avoid an
ORM: the schema is small, the queries are part of the reliability contract, and
explicit SQL makes lease/fencing behavior auditable.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bear_agent.core.errors import SessionLeaseError, StorageError
from bear_agent.core.events import Event, EventType
from bear_agent.core.messages import Message
from bear_agent.core.usage import TokenUsage, UsageSummary
from bear_agent.sessions.models import SessionRecord
from bear_agent.storage.database import Database, transaction


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _row_to_session(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        id=row["id"],
        project_id=row["project_id"],
        cwd=row["cwd"],
        title=row["title"],
        status=row["status"],
        model=row["model"],
        provider=row["provider"],
        last_seq=int(row["last_seq"]),
        lease_owner=row["lease_owner"],
        lease_epoch=int(row["lease_epoch"]),
        lease_expires_at=_parse_dt(row["lease_expires_at"]),
        created_at=_parse_dt(row["created_at"]) or utcnow(),
        updated_at=_parse_dt(row["updated_at"]) or utcnow(),
    )


class SessionRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, session: SessionRecord) -> SessionRecord:
        return await self.db.run(lambda conn: self._create(conn, session))

    def _create(self, conn: sqlite3.Connection, session: SessionRecord) -> SessionRecord:
        try:
            conn.execute(
                """
                INSERT INTO sessions (
                    id, project_id, cwd, title, status, model, provider, last_seq,
                    lease_owner, lease_epoch, lease_expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    session.project_id,
                    session.cwd,
                    session.title,
                    session.status,
                    session.model,
                    session.provider,
                    session.last_seq,
                    session.lease_owner,
                    session.lease_epoch,
                    _iso(session.lease_expires_at) if session.lease_expires_at else None,
                    _iso(session.created_at),
                    _iso(session.updated_at),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"session already exists: {session.id}") from exc
        return session

    async def get(self, session_id: str) -> SessionRecord | None:
        return await self.db.run(lambda conn: self._get(conn, session_id))

    def _get(self, conn: sqlite3.Connection, session_id: str) -> SessionRecord | None:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return _row_to_session(row) if row else None

    async def latest_for_cwd(self, cwd: str | Path) -> SessionRecord | None:
        return await self.db.run(lambda conn: self._latest_for_cwd(conn, cwd))

    def _latest_for_cwd(self, conn: sqlite3.Connection, cwd: str | Path) -> SessionRecord | None:
        resolved = str(Path(cwd).resolve())
        row = conn.execute(
            "SELECT * FROM sessions WHERE cwd = ? ORDER BY updated_at DESC LIMIT 1",
            (resolved,),
        ).fetchone()
        return _row_to_session(row) if row else None

    async def list_recent(self, limit: int = 50) -> list[SessionRecord]:
        return await self.db.run(lambda conn: self._list_recent(conn, limit))

    def _list_recent(self, conn: sqlite3.Connection, limit: int) -> list[SessionRecord]:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_session(row) for row in rows]

    async def update_title(self, session_id: str, title: str) -> None:
        await self.db.run(lambda conn: self._update_title(conn, session_id, title))

    def _update_title(self, conn: sqlite3.Connection, session_id: str, title: str) -> None:
        conn.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            (title, _iso(utcnow()), session_id),
        )

    async def update_status(self, session_id: str, status: str) -> None:
        await self.db.run(lambda conn: self._update_status(conn, session_id, status))

    def _update_status(self, conn: sqlite3.Connection, session_id: str, status: str) -> None:
        conn.execute(
            "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
            (status, _iso(utcnow()), session_id),
        )

    async def acquire_lease(self, session_id: str, owner: str, ttl_seconds: float = 300.0) -> int:
        return await self.db.run(
            lambda conn: self._acquire_lease(conn, session_id, owner, ttl_seconds)
        )

    def _acquire_lease(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        owner: str,
        ttl_seconds: float,
    ) -> int:
        with transaction(conn):
            row = conn.execute(
                "SELECT lease_owner, lease_epoch, lease_expires_at FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise StorageError(f"unknown session: {session_id}")
            now = utcnow()
            expires = _parse_dt(row["lease_expires_at"])
            current_owner = row["lease_owner"]
            can_take = (
                current_owner is None or current_owner == owner or expires is None or expires <= now
            )
            if not can_take:
                raise SessionLeaseError(f"session {session_id} is leased by {current_owner}")
            epoch = int(row["lease_epoch"]) + 1
            new_expiry = now + timedelta(seconds=ttl_seconds)
            conn.execute(
                """
                UPDATE sessions
                   SET lease_owner = ?, lease_epoch = ?, lease_expires_at = ?, updated_at = ?
                 WHERE id = ?
                """,
                (owner, epoch, _iso(new_expiry), _iso(now), session_id),
            )
            return epoch

    async def renew_lease(
        self, session_id: str, owner: str, epoch: int, ttl_seconds: float
    ) -> bool:
        return await self.db.run(
            lambda conn: self._renew_lease(conn, session_id, owner, epoch, ttl_seconds)
        )

    def _renew_lease(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        owner: str,
        epoch: int,
        ttl_seconds: float,
    ) -> bool:
        now = utcnow()
        cursor = conn.execute(
            """
            UPDATE sessions SET lease_expires_at = ?
             WHERE id = ? AND lease_owner = ? AND lease_epoch = ?
            """,
            (_iso(now + timedelta(seconds=ttl_seconds)), session_id, owner, epoch),
        )
        return cursor.rowcount > 0

    async def release_lease(self, session_id: str, owner: str, epoch: int) -> None:
        await self.db.run(lambda conn: self._release_lease(conn, session_id, owner, epoch))

    def _release_lease(
        self, conn: sqlite3.Connection, session_id: str, owner: str, epoch: int
    ) -> None:
        conn.execute(
            """
            UPDATE sessions
               SET lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
             WHERE id = ? AND lease_owner = ? AND lease_epoch = ?
            """,
            (_iso(utcnow()), session_id, owner, epoch),
        )


class EventRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def append(
        self,
        session_id: str,
        events: list[Event],
        *,
        expected_seq: int | None = None,
    ) -> list[Event]:
        return await self.db.run(lambda conn: self._append(conn, session_id, events, expected_seq))

    def _append(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        events: list[Event],
        expected_seq: int | None,
    ) -> list[Event]:
        persisted = [event for event in events if event.persisted]
        if not persisted:
            return []
        with transaction(conn):
            row = conn.execute(
                "SELECT last_seq FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise StorageError(f"unknown session: {session_id}")
            last_seq = int(row["last_seq"])
            if expected_seq is not None and last_seq != expected_seq:
                raise StorageError(
                    f"session {session_id} sequence conflict: expected {expected_seq}, got {last_seq}"
                )
            result: list[Event] = []
            for event in persisted:
                last_seq += 1
                stored = event.with_seq(last_seq)
                conn.execute(
                    """
                    INSERT INTO events (
                        session_id, seq, event_id, type, run_id, turn_id,
                        span_id, parent_span_id, schema_version, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        stored.seq,
                        stored.event_id,
                        stored.type.value,
                        stored.run_id,
                        stored.turn_id,
                        stored.span_id,
                        stored.parent_span_id,
                        1,
                        json.dumps(stored.payload, ensure_ascii=False, default=str),
                        _iso(stored.ts),
                    ),
                )
                result.append(stored)
            conn.execute(
                "UPDATE sessions SET last_seq = ?, updated_at = ? WHERE id = ?",
                (last_seq, _iso(utcnow()), session_id),
            )
            return result

    async def load(self, session_id: str, after_seq: int = 0, limit: int = 10_000) -> list[Event]:
        return await self.db.run(lambda conn: self._load(conn, session_id, after_seq, limit))

    def _load(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        after_seq: int,
        limit: int,
    ) -> list[Event]:
        rows = conn.execute(
            """
            SELECT * FROM events
             WHERE session_id = ? AND seq > ?
             ORDER BY seq ASC LIMIT ?
            """,
            (session_id, after_seq, limit),
        ).fetchall()
        return [
            Event(
                event_id=row["event_id"],
                session_id=row["session_id"],
                seq=int(row["seq"]),
                type=EventType(row["type"]),
                ts=_parse_dt(row["created_at"]) or utcnow(),
                run_id=row["run_id"],
                turn_id=row["turn_id"],
                span_id=row["span_id"],
                parent_span_id=row["parent_span_id"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]


class MessageRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def append(
        self, session_id: str, message: Message, token_estimate: int | None = None
    ) -> int:
        return await self.db.run(
            lambda conn: self._append(conn, session_id, message, token_estimate)
        )

    def _append(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        message: Message,
        token_estimate: int | None,
    ) -> int:
        with transaction(conn):
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = int(row["max_seq"]) + 1
            now = _iso(utcnow())
            conn.execute(
                """
                INSERT INTO messages (session_id, seq, role, content_json, token_estimate, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    seq,
                    message.role,
                    message.model_dump_json(),
                    token_estimate,
                    now,
                ),
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            return seq

    async def load(self, session_id: str) -> list[Message]:
        return await self.db.run(lambda conn: self._load(conn, session_id))

    def _load(self, conn: sqlite3.Connection, session_id: str) -> list[Message]:
        rows = conn.execute(
            "SELECT content_json FROM messages WHERE session_id = ? ORDER BY seq ASC",
            (session_id,),
        ).fetchall()
        return [Message.model_validate_json(row["content_json"]) for row in rows]


class ToolCallRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def start(
        self,
        session_id: str,
        turn_id: str | None,
        call_id: str,
        name: str,
        arguments: dict[str, Any],
        idempotent: bool,
    ) -> None:
        await self.db.run(
            lambda conn: self._start(
                conn, session_id, turn_id, call_id, name, arguments, idempotent
            )
        )

    def _start(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        turn_id: str | None,
        call_id: str,
        name: str,
        arguments: dict[str, Any],
        idempotent: bool,
    ) -> None:
        conn.execute(
            """
            INSERT OR REPLACE INTO tool_calls (
                id, session_id, turn_id, name, args_json, status,
                idempotent, started_at, finished_at, result_summary,
                error_kind, error_message
            ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, NULL, NULL, NULL, NULL)
            """,
            (
                call_id,
                session_id,
                turn_id,
                name,
                json.dumps(arguments, ensure_ascii=False, default=str),
                1 if idempotent else 0,
                _iso(utcnow()),
            ),
        )

    async def finish(
        self,
        call_id: str,
        status: str,
        *,
        result_summary: str | None = None,
        error_kind: str | None = None,
        error_message: str | None = None,
    ) -> None:
        await self.db.run(
            lambda conn: self._finish(
                conn, call_id, status, result_summary, error_kind, error_message
            )
        )

    def _finish(
        self,
        conn: sqlite3.Connection,
        call_id: str,
        status: str,
        result_summary: str | None,
        error_kind: str | None,
        error_message: str | None,
    ) -> None:
        conn.execute(
            """
            UPDATE tool_calls
               SET status = ?, finished_at = ?, result_summary = ?,
                   error_kind = ?, error_message = ?
             WHERE id = ?
            """,
            (status, _iso(utcnow()), result_summary, error_kind, error_message, call_id),
        )

    async def get(self, call_id: str) -> dict[str, Any] | None:
        return await self.db.run(lambda conn: self._get(conn, call_id))

    def _get(self, conn: sqlite3.Connection, call_id: str) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM tool_calls WHERE id = ?", (call_id,)).fetchone()
        return dict(row) if row else None

    async def mark_running_unknown(self, session_id: str) -> int:
        return await self.db.run(lambda conn: self._mark_running_unknown(conn, session_id))

    def _mark_running_unknown(self, conn: sqlite3.Connection, session_id: str) -> int:
        cursor = conn.execute(
            """
            UPDATE tool_calls SET status = 'unknown', finished_at = ?
             WHERE session_id = ? AND status = 'running'
            """,
            (_iso(utcnow()), session_id),
        )
        return cursor.rowcount


class ModelCallRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def record(
        self,
        *,
        call_id: str,
        session_id: str,
        turn_id: str | None,
        provider: str,
        model: str,
        usage: TokenUsage,
        latency_ms: int,
        status: str,
    ) -> None:
        await self.db.run(
            lambda conn: self._record(
                conn,
                call_id,
                session_id,
                turn_id,
                provider,
                model,
                usage,
                latency_ms,
                status,
            )
        )

    def _record(
        self,
        conn: sqlite3.Connection,
        call_id: str,
        session_id: str,
        turn_id: str | None,
        provider: str,
        model: str,
        usage: TokenUsage,
        latency_ms: int,
        status: str,
    ) -> None:
        conn.execute(
            """
            INSERT OR REPLACE INTO model_calls (
                id, session_id, turn_id, provider, model, input_tokens,
                output_tokens, cache_read_tokens, cache_write_tokens,
                latency_ms, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                call_id,
                session_id,
                turn_id,
                provider,
                model,
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_tokens,
                usage.cache_write_tokens,
                latency_ms,
                status,
                _iso(utcnow()),
            ),
        )

    async def usage_summary(self, session_id: str) -> UsageSummary:
        return await self.db.run(lambda conn: self._usage_summary(conn, session_id))

    def _usage_summary(self, conn: sqlite3.Connection, session_id: str) -> UsageSummary:
        row = conn.execute(
            """
            SELECT COUNT(*) AS calls,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                   COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens
              FROM model_calls WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        return UsageSummary(
            calls=int(row["calls"]),
            usage=TokenUsage(
                input_tokens=int(row["input_tokens"]),
                output_tokens=int(row["output_tokens"]),
                cache_read_tokens=int(row["cache_read_tokens"]),
                cache_write_tokens=int(row["cache_write_tokens"]),
            ),
        )
