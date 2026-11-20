"""Repositories for continuity state: checkpoints, compactions, anchors.

These tables are append-mostly. A checkpoint is immutable once written; a
compaction is immutable; anchors and context state are small mutable control
rows. All writes that can be fenced accept a :class:`LeaseFence` and verify the
session lease inside the same SQLite transaction as the write.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from bear_agent.core.errors import StorageError
from bear_agent.core.fencing import LeaseFence, resolve_fence
from bear_agent.storage.database import Database, transaction
from bear_agent.storage.repositories import (
    _iso,
    _parse_dt,
    assert_session_lease_row,
    utcnow,
)

CHECKPOINT_SCHEMA_VERSION = 1


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def compute_checkpoint_checksum(
    *,
    session_id: str,
    kind: str,
    event_seq: int,
    message_seq: int,
    state: dict[str, Any],
    schema_version: int = CHECKPOINT_SCHEMA_VERSION,
) -> str:
    """Checksum binds the checkpoint to its session and transcript position.

    Copying a valid state blob to another row (different session, sequence or
    kind) invalidates the checksum, so a partial or forged row is detected.
    """
    payload = canonical_json(
        {
            "session_id": session_id,
            "kind": kind,
            "event_seq": event_seq,
            "message_seq": message_seq,
            "state": state,
            "schema_version": schema_version,
        }
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    id: str
    session_id: str
    sha256: str
    path: str
    size: int
    mime: str | None
    kind: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    id: str
    session_id: str
    run_id: str | None
    kind: str
    event_seq: int
    message_seq: int
    state: dict[str, Any]
    checksum: str
    schema_version: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CompactionRecord:
    id: str
    session_id: str
    level: int
    covered_from_message_seq: int
    covered_to_message_seq: int
    summary: dict[str, Any]
    artifact_id: str | None
    before_tokens: int
    after_tokens: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AnchorRecord:
    id: str
    session_id: str
    kind: str
    text: str
    source_message_seq: int | None
    salience: float
    active: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ContextStateRecord:
    session_id: str
    generation: int
    last_compaction_id: str | None
    consecutive_failures: int
    disabled_until_turn: int | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    id: str
    session_id: str
    tool_call_id: str | None
    lease_owner: str
    pid: int
    pgid: int | None
    command_preview: str | None
    created_at: datetime
    finished_at: datetime | None
    status: str


class ArtifactRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self, record: ArtifactRecord, *, fence: LeaseFence | None = None
    ) -> ArtifactRecord:
        fence = resolve_fence(fence)
        return await self.db.run(lambda conn: self._create(conn, record, fence))

    def _create(
        self,
        conn: sqlite3.Connection,
        record: ArtifactRecord,
        fence: LeaseFence | None = None,
    ) -> ArtifactRecord:
        if fence is not None:
            with transaction(conn):
                assert_session_lease_row(conn, record.session_id, fence)
                self._insert(conn, record)
            return record
        self._insert(conn, record)
        return record

    def _insert(self, conn: sqlite3.Connection, record: ArtifactRecord) -> None:
        conn.execute(
            """
            INSERT INTO artifacts (
                id, session_id, sha256, path, size, mime, kind, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.session_id,
                record.sha256,
                record.path,
                record.size,
                record.mime,
                record.kind,
                canonical_json(record.metadata) if record.metadata else None,
                _iso(record.created_at),
            ),
        )

    async def get(self, artifact_id: str) -> ArtifactRecord | None:
        return await self.db.run(lambda conn: self._get(conn, artifact_id))

    def _get(self, conn: sqlite3.Connection, artifact_id: str) -> ArtifactRecord | None:
        row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        return _row_to_artifact(row) if row else None

    async def list_for_session(
        self, session_id: str, *, kind: str | None = None, limit: int = 200
    ) -> list[ArtifactRecord]:
        return await self.db.run(lambda conn: self._list_for_session(conn, session_id, kind, limit))

    def _list_for_session(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        kind: str | None,
        limit: int,
    ) -> list[ArtifactRecord]:
        if kind:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE session_id = ? AND kind = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (session_id, kind, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [_row_to_artifact(row) for row in rows]


def _row_to_artifact(row: sqlite3.Row) -> ArtifactRecord:
    metadata: dict[str, Any] = {}
    # sqlite3.Row membership tests values, not column names.
    columns = row.keys()
    if "metadata_json" in columns and row["metadata_json"]:
        try:
            value = json.loads(row["metadata_json"])
            if isinstance(value, dict):
                metadata = value
        except (TypeError, ValueError):
            metadata = {}
    return ArtifactRecord(
        id=row["id"],
        session_id=row["session_id"],
        sha256=row["sha256"],
        path=row["path"],
        size=int(row["size"]),
        mime=row["mime"],
        kind=row["kind"] if "kind" in columns else None,
        metadata=metadata,
        created_at=_parse_dt(row["created_at"]) or utcnow(),
    )


class CheckpointRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self, record: CheckpointRecord, *, fence: LeaseFence | None = None
    ) -> CheckpointRecord:
        fence = resolve_fence(fence)
        return await self.db.run(lambda conn: self._create(conn, record, fence))

    def _create(
        self,
        conn: sqlite3.Connection,
        record: CheckpointRecord,
        fence: LeaseFence | None,
    ) -> CheckpointRecord:
        with transaction(conn):
            if fence is not None:
                assert_session_lease_row(conn, record.session_id, fence)
            conn.execute(
                """
                INSERT INTO checkpoints (
                    id, session_id, run_id, kind, event_seq, message_seq,
                    state_json, checksum, schema_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.session_id,
                    record.run_id,
                    record.kind,
                    record.event_seq,
                    record.message_seq,
                    canonical_json(record.state),
                    record.checksum,
                    record.schema_version,
                    _iso(record.created_at),
                ),
            )
        return record

    async def get(self, checkpoint_id: str) -> CheckpointRecord | None:
        return await self.db.run(lambda conn: self._get(conn, checkpoint_id))

    def _get(self, conn: sqlite3.Connection, checkpoint_id: str) -> CheckpointRecord | None:
        row = conn.execute("SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,)).fetchone()
        return _row_to_checkpoint(row) if row else None

    async def latest(self, session_id: str) -> CheckpointRecord | None:
        return await self.db.run(lambda conn: self._latest(conn, session_id))

    def _latest(self, conn: sqlite3.Connection, session_id: str) -> CheckpointRecord | None:
        row = conn.execute(
            "SELECT * FROM checkpoints WHERE session_id = ? "
            "ORDER BY event_seq DESC, created_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return _row_to_checkpoint(row) if row else None

    async def list_for_session(self, session_id: str, limit: int = 50) -> list[CheckpointRecord]:
        return await self.db.run(lambda conn: self._list_for_session(conn, session_id, limit))

    def _list_for_session(
        self, conn: sqlite3.Connection, session_id: str, limit: int
    ) -> list[CheckpointRecord]:
        rows = conn.execute(
            "SELECT * FROM checkpoints WHERE session_id = ? "
            "ORDER BY event_seq DESC, created_at DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [_row_to_checkpoint(row) for row in rows]

    async def prune(
        self, session_id: str, keep: int, *, fence: LeaseFence | None = None
    ) -> list[str]:
        fence = resolve_fence(fence)
        return await self.db.run(lambda conn: self._prune(conn, session_id, keep, fence))

    def _prune(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        keep: int,
        fence: LeaseFence | None = None,
    ) -> list[str]:
        if keep <= 0:
            return []
        with transaction(conn):
            if fence is not None:
                assert_session_lease_row(conn, session_id, fence)
            rows = conn.execute(
                "SELECT id FROM checkpoints WHERE session_id = ? "
                "ORDER BY event_seq DESC, created_at DESC",
                (session_id,),
            ).fetchall()
            stale = [str(row["id"]) for row in rows[keep:]]
            if stale:
                conn.executemany(
                    "DELETE FROM checkpoints WHERE id = ?", [(item,) for item in stale]
                )
        return stale


def _row_to_checkpoint(row: sqlite3.Row) -> CheckpointRecord:
    try:
        state = json.loads(row["state_json"])
    except (TypeError, ValueError) as exc:
        raise StorageError(f"checkpoint {row['id']} state is not valid JSON") from exc
    if not isinstance(state, dict):
        raise StorageError(f"checkpoint {row['id']} state is not an object")
    return CheckpointRecord(
        id=row["id"],
        session_id=row["session_id"],
        run_id=row["run_id"],
        kind=row["kind"],
        event_seq=int(row["event_seq"]),
        message_seq=int(row["message_seq"]),
        state=state,
        checksum=row["checksum"],
        schema_version=int(row["schema_version"]),
        created_at=_parse_dt(row["created_at"]) or utcnow(),
    )


class CompactionRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self, record: CompactionRecord, *, fence: LeaseFence | None = None
    ) -> CompactionRecord:
        fence = resolve_fence(fence)
        return await self.db.run(lambda conn: self._create(conn, record, fence))

    def _create(
        self,
        conn: sqlite3.Connection,
        record: CompactionRecord,
        fence: LeaseFence | None,
    ) -> CompactionRecord:
        if record.level not in {1, 2, 3, 4}:
            raise ValueError(f"invalid compaction level: {record.level}")
        if record.covered_to_message_seq < record.covered_from_message_seq:
            raise ValueError("compaction range is inverted")
        with transaction(conn):
            if fence is not None:
                assert_session_lease_row(conn, record.session_id, fence)
            conn.execute(
                """
                INSERT INTO compactions (
                    id, session_id, level, covered_from_message_seq,
                    covered_to_message_seq, summary_json, artifact_id,
                    before_tokens, after_tokens, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.session_id,
                    record.level,
                    record.covered_from_message_seq,
                    record.covered_to_message_seq,
                    canonical_json(record.summary),
                    record.artifact_id,
                    record.before_tokens,
                    record.after_tokens,
                    _iso(record.created_at),
                ),
            )
        return record

    async def get(self, compaction_id: str) -> CompactionRecord | None:
        return await self.db.run(lambda conn: self._get(conn, compaction_id))

    def _get(self, conn: sqlite3.Connection, compaction_id: str) -> CompactionRecord | None:
        row = conn.execute("SELECT * FROM compactions WHERE id = ?", (compaction_id,)).fetchone()
        return _row_to_compaction(row) if row else None

    async def latest(self, session_id: str) -> CompactionRecord | None:
        return await self.db.run(lambda conn: self._latest(conn, session_id))

    def _latest(self, conn: sqlite3.Connection, session_id: str) -> CompactionRecord | None:
        row = conn.execute(
            "SELECT * FROM compactions WHERE session_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return _row_to_compaction(row) if row else None

    async def list_for_session(self, session_id: str, limit: int = 100) -> list[CompactionRecord]:
        """Return the most recent ``limit`` compactions in chronological order.

        Loading the oldest N rows would silently drop the newest (and therefore
        effective) summaries once a long session exceeds the limit, so the
        query selects newest-first and reverses the result for application.
        """
        return await self.db.run(lambda conn: self._list_for_session(conn, session_id, limit))

    def _list_for_session(
        self, conn: sqlite3.Connection, session_id: str, limit: int
    ) -> list[CompactionRecord]:
        rows = conn.execute(
            "SELECT * FROM compactions WHERE session_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [_row_to_compaction(row) for row in reversed(rows)]


def _row_to_compaction(row: sqlite3.Row) -> CompactionRecord:
    try:
        summary = json.loads(row["summary_json"])
    except (TypeError, ValueError) as exc:
        raise StorageError(f"compaction {row['id']} summary is not valid JSON") from exc
    if not isinstance(summary, dict):
        raise StorageError(f"compaction {row['id']} summary is not an object")
    return CompactionRecord(
        id=row["id"],
        session_id=row["session_id"],
        level=int(row["level"]),
        covered_from_message_seq=int(row["covered_from_message_seq"]),
        covered_to_message_seq=int(row["covered_to_message_seq"]),
        summary=summary,
        artifact_id=row["artifact_id"],
        before_tokens=int(row["before_tokens"]),
        after_tokens=int(row["after_tokens"]),
        created_at=_parse_dt(row["created_at"]) or utcnow(),
    )


class AnchorRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def upsert(
        self, record: AnchorRecord, *, fence: LeaseFence | None = None
    ) -> AnchorRecord:
        fence = resolve_fence(fence)
        return await self.db.run(lambda conn: self._upsert(conn, record, fence))

    def _upsert(
        self,
        conn: sqlite3.Connection,
        record: AnchorRecord,
        fence: LeaseFence | None,
    ) -> AnchorRecord:
        with transaction(conn):
            if fence is not None:
                assert_session_lease_row(conn, record.session_id, fence)
            conn.execute(
                """
                INSERT INTO context_anchors (
                    id, session_id, kind, text, source_message_seq,
                    salience, active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    text = excluded.text,
                    salience = excluded.salience,
                    active = excluded.active,
                    updated_at = excluded.updated_at
                """,
                (
                    record.id,
                    record.session_id,
                    record.kind,
                    record.text,
                    record.source_message_seq,
                    record.salience,
                    1 if record.active else 0,
                    _iso(record.created_at),
                    _iso(record.updated_at),
                ),
            )
        return record

    async def get(self, anchor_id: str) -> AnchorRecord | None:
        return await self.db.run(lambda conn: self._get(conn, anchor_id))

    def _get(self, conn: sqlite3.Connection, anchor_id: str) -> AnchorRecord | None:
        row = conn.execute("SELECT * FROM context_anchors WHERE id = ?", (anchor_id,)).fetchone()
        return _row_to_anchor(row) if row else None

    async def list_active(self, session_id: str, limit: int = 50) -> list[AnchorRecord]:
        return await self.db.run(lambda conn: self._list_active(conn, session_id, limit))

    def _list_active(
        self, conn: sqlite3.Connection, session_id: str, limit: int
    ) -> list[AnchorRecord]:
        rows = conn.execute(
            "SELECT * FROM context_anchors WHERE session_id = ? AND active = 1 "
            "ORDER BY salience DESC, created_at ASC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [_row_to_anchor(row) for row in rows]

    async def find_by_text(self, session_id: str, text: str) -> AnchorRecord | None:
        return await self.db.run(lambda conn: self._find_by_text(conn, session_id, text))

    def _find_by_text(
        self, conn: sqlite3.Connection, session_id: str, text: str
    ) -> AnchorRecord | None:
        row = conn.execute(
            "SELECT * FROM context_anchors WHERE session_id = ? AND text = ? "
            "ORDER BY created_at ASC LIMIT 1",
            (session_id, text),
        ).fetchone()
        return _row_to_anchor(row) if row else None

    async def deactivate(
        self, session_id: str, anchor_id: str, *, fence: LeaseFence | None = None
    ) -> bool:
        fence = resolve_fence(fence)
        return await self.db.run(lambda conn: self._deactivate(conn, session_id, anchor_id, fence))

    def _deactivate(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        anchor_id: str,
        fence: LeaseFence | None,
    ) -> bool:
        with transaction(conn):
            if fence is not None:
                assert_session_lease_row(conn, session_id, fence)
            cursor = conn.execute(
                "UPDATE context_anchors SET active = 0, updated_at = ? "
                "WHERE id = ? AND session_id = ?",
                (_iso(utcnow()), anchor_id, session_id),
            )
        return cursor.rowcount > 0


def _row_to_anchor(row: sqlite3.Row) -> AnchorRecord:
    return AnchorRecord(
        id=row["id"],
        session_id=row["session_id"],
        kind=row["kind"],
        text=row["text"],
        source_message_seq=(
            int(row["source_message_seq"]) if row["source_message_seq"] is not None else None
        ),
        salience=float(row["salience"]),
        active=bool(row["active"]),
        created_at=_parse_dt(row["created_at"]) or utcnow(),
        updated_at=_parse_dt(row["updated_at"]) or utcnow(),
    )


class ContextStateRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, session_id: str) -> ContextStateRecord | None:
        return await self.db.run(lambda conn: self._get(conn, session_id))

    def _get(self, conn: sqlite3.Connection, session_id: str) -> ContextStateRecord | None:
        row = conn.execute(
            "SELECT * FROM context_state WHERE session_id = ?", (session_id,)
        ).fetchone()
        return _row_to_context_state(row) if row else None

    async def save(
        self, record: ContextStateRecord, *, fence: LeaseFence | None = None
    ) -> ContextStateRecord:
        fence = resolve_fence(fence)
        return await self.db.run(lambda conn: self._save(conn, record, fence))

    def _save(
        self,
        conn: sqlite3.Connection,
        record: ContextStateRecord,
        fence: LeaseFence | None,
    ) -> ContextStateRecord:
        with transaction(conn):
            if fence is not None:
                assert_session_lease_row(conn, record.session_id, fence)
            conn.execute(
                """
                INSERT INTO context_state (
                    session_id, generation, last_compaction_id,
                    consecutive_failures, disabled_until_turn, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    generation = excluded.generation,
                    last_compaction_id = excluded.last_compaction_id,
                    consecutive_failures = excluded.consecutive_failures,
                    disabled_until_turn = excluded.disabled_until_turn,
                    updated_at = excluded.updated_at
                """,
                (
                    record.session_id,
                    record.generation,
                    record.last_compaction_id,
                    record.consecutive_failures,
                    record.disabled_until_turn,
                    _iso(record.updated_at),
                ),
            )
        return record


def _row_to_context_state(row: sqlite3.Row) -> ContextStateRecord:
    return ContextStateRecord(
        session_id=row["session_id"],
        generation=int(row["generation"]),
        last_compaction_id=row["last_compaction_id"],
        consecutive_failures=int(row["consecutive_failures"]),
        disabled_until_turn=(
            int(row["disabled_until_turn"]) if row["disabled_until_turn"] is not None else None
        ),
        updated_at=_parse_dt(row["updated_at"]) or utcnow(),
    )


class ProcessRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self, record: ProcessRecord, *, fence: LeaseFence | None = None
    ) -> ProcessRecord:
        fence = resolve_fence(fence)
        return await self.db.run(lambda conn: self._create(conn, record, fence))

    def _create(
        self,
        conn: sqlite3.Connection,
        record: ProcessRecord,
        fence: LeaseFence | None,
    ) -> ProcessRecord:
        with transaction(conn):
            if fence is not None:
                assert_session_lease_row(conn, record.session_id, fence)
            conn.execute(
                """
                INSERT INTO process_records (
                    id, session_id, tool_call_id, lease_owner, pid, pgid,
                    command_preview, created_at, finished_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.session_id,
                    record.tool_call_id,
                    record.lease_owner,
                    record.pid,
                    record.pgid,
                    record.command_preview,
                    _iso(record.created_at),
                    _iso(record.finished_at) if record.finished_at else None,
                    record.status,
                ),
            )
        return record

    async def claim_termination(
        self,
        record_id: str,
        session_id: str,
        lease_owner: str,
        *,
        fence: LeaseFence | None = None,
    ) -> bool:
        """Atomically claim an orphan for termination.

        The fence check and the conditional status transition share one
        transaction. A stale recovery cannot claim, and the current owner's own
        process is never claimed. Only one recoverer can win the transition.
        """
        fence = resolve_fence(fence)
        return await self.db.run(
            lambda conn: self._claim_termination(conn, record_id, session_id, lease_owner, fence)
        )

    def _claim_termination(
        self,
        conn: sqlite3.Connection,
        record_id: str,
        session_id: str,
        lease_owner: str,
        fence: LeaseFence | None,
    ) -> bool:
        with transaction(conn):
            if fence is not None:
                assert_session_lease_row(conn, session_id, fence)
            cursor = conn.execute(
                """
                UPDATE process_records
                   SET status = 'unknown', finished_at = ?
                 WHERE id = ? AND session_id = ? AND status = 'running'
                   AND lease_owner != ?
                """,
                (_iso(utcnow()), record_id, session_id, lease_owner),
            )
            return cursor.rowcount > 0

    async def record_outcome(
        self, record_id: str, status: str, *, fence: LeaseFence | None = None
    ) -> None:
        """Record the result of an already-claimed termination attempt."""
        if status not in {"terminated", "unknown"}:
            raise ValueError(f"invalid termination outcome: {status}")
        fence = resolve_fence(fence)
        await self.db.run(lambda conn: self._record_outcome(conn, record_id, status, fence))

    def _record_outcome(
        self,
        conn: sqlite3.Connection,
        record_id: str,
        status: str,
        fence: LeaseFence | None,
    ) -> None:
        with transaction(conn):
            if fence is not None:
                row = conn.execute(
                    "SELECT session_id FROM process_records WHERE id = ?", (record_id,)
                ).fetchone()
                if row is None:
                    raise StorageError(f"unknown process record: {record_id}")
                assert_session_lease_row(conn, str(row["session_id"]), fence)
            conn.execute(
                "UPDATE process_records SET status = ?, finished_at = ? WHERE id = ?",
                (status, _iso(utcnow()), record_id),
            )

    async def finish(self, record_id: str, status: str, *, fence: LeaseFence | None = None) -> None:
        fence = resolve_fence(fence)
        await self.db.run(lambda conn: self._finish(conn, record_id, status, fence))

    def _finish(
        self,
        conn: sqlite3.Connection,
        record_id: str,
        status: str,
        fence: LeaseFence | None = None,
    ) -> None:
        if status not in {"exited", "terminated", "unknown"}:
            raise ValueError(f"invalid terminal process status: {status}")
        with transaction(conn):
            if fence is not None:
                row = conn.execute(
                    "SELECT session_id FROM process_records WHERE id = ?", (record_id,)
                ).fetchone()
                if row is None:
                    raise StorageError(f"unknown process record: {record_id}")
                assert_session_lease_row(conn, str(row["session_id"]), fence)
            conn.execute(
                "UPDATE process_records SET status = ?, finished_at = ? "
                "WHERE id = ? AND status = 'running'",
                (status, _iso(utcnow()), record_id),
            )

    async def list_running(self, session_id: str | None = None) -> list[ProcessRecord]:
        return await self.db.run(lambda conn: self._list_running(conn, session_id))

    def _list_running(
        self, conn: sqlite3.Connection, session_id: str | None
    ) -> list[ProcessRecord]:
        if session_id is None:
            rows = conn.execute(
                "SELECT * FROM process_records WHERE status = 'running' ORDER BY created_at ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM process_records WHERE status = 'running' AND session_id = ? "
                "ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
        return [_row_to_process(row) for row in rows]


def _row_to_process(row: sqlite3.Row) -> ProcessRecord:
    return ProcessRecord(
        id=row["id"],
        session_id=row["session_id"],
        tool_call_id=row["tool_call_id"],
        lease_owner=row["lease_owner"],
        pid=int(row["pid"]),
        pgid=int(row["pgid"]) if row["pgid"] is not None else None,
        command_preview=row["command_preview"],
        created_at=_parse_dt(row["created_at"]) or utcnow(),
        finished_at=_parse_dt(row["finished_at"]),
        status=row["status"],
    )
