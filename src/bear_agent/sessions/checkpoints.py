"""Session checkpoints.

A checkpoint is an immutable, small snapshot of *control state* at a known
position in the transcript. It deliberately does not contain the transcript:
message rows remain the single source of truth, and the checkpoint only records
where the session was (event/message sequence) and what execution state was live
at that moment (running tools, context generation, anchors, usage).

Design rules:

* The snapshot and the row insert happen in one SQLite transaction, guarded by
  the session lease fence. A crash therefore leaves either a complete
  checkpoint or no checkpoint.
* A row is validated on every read that matters: schema version, checksum,
  shape and position bounds. Corrupt or incompatible rows are reported and
  ignored, never partially trusted, and never deleted automatically.
* Checkpoint ``kind`` is a label, not an enum enforced by the database. Known
  kinds are listed in :data:`CHECKPOINT_KINDS`; unknown kinds are stored as
  given so later phases can add kinds without a migration.
"""

from __future__ import annotations

import hmac
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Any

from bear_agent.audit.service import AuditService
from bear_agent.config.models import CheckpointConfig
from bear_agent.core.clock import Clock, SystemClock
from bear_agent.core.errors import (
    CheckpointCorrupt,
    CheckpointError,
    CheckpointIncompatible,
    StorageError,
)
from bear_agent.core.events import EventType
from bear_agent.core.fencing import LeaseFence
from bear_agent.core.ids import new_id
from bear_agent.observability.recorder import EventRecorder
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.sessions.service import SessionService
from bear_agent.storage.continuity_repositories import (
    CHECKPOINT_SCHEMA_VERSION,
    AnchorRepository,
    CheckpointRecord,
    CheckpointRepository,
    CompactionRepository,
    ContextStateRepository,
    canonical_json,
    compute_checkpoint_checksum,
)
from bear_agent.storage.database import transaction
from bear_agent.storage.repositories import _iso, assert_session_lease_row

CHECKPOINT_FORMAT = "bear-checkpoint"

# Label set used by the runtime and context layer. Stored as free text: an
# unknown label is still recorded faithfully.
CHECKPOINT_KINDS: frozenset[str] = frozenset(
    {
        "turn",
        "pre_compaction",
        "post_compaction",
        "recovery",
        "fork",
        "manual",
        "pre_l4_reset",
    }
)

_ANCHOR_LIMIT = 50
_COMPACTION_LIMIT = 5
_RECENT_TOOL_LIMIT = 20


class CheckpointService:
    def __init__(
        self,
        *,
        sessions: SessionService,
        checkpoints: CheckpointRepository,
        compactions: CompactionRepository,
        anchors: AnchorRepository,
        context_state: ContextStateRepository,
        recorder: EventRecorder,
        audit: AuditService | None = None,
        config: CheckpointConfig | None = None,
        redactor: SecretRedactor | None = None,
        task_snapshot: Callable[[sqlite3.Connection, str], dict[str, Any]] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.sessions = sessions
        self.checkpoints = checkpoints
        self.compactions = compactions
        self.anchors = anchors
        self.context_state = context_state
        self.recorder = recorder
        self.audit = audit
        self.config = config or CheckpointConfig()
        self.redactor = redactor or SecretRedactor()
        self.task_snapshot = task_snapshot
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ create

    async def create(
        self,
        session_id: str,
        *,
        fence: LeaseFence,
        kind: str = "turn",
        run_id: str | None = None,
        turn_index: int | None = None,
    ) -> CheckpointRecord:
        """Snapshot control state and insert one immutable checkpoint row.

        The fence must still own the session lease; the check, the snapshot and
        the insert all happen inside one transaction. Retention pruning and the
        ``checkpoint.created`` event/audit record happen afterwards.
        """
        if not kind:
            raise ValueError("checkpoint kind must not be empty")
        created_at = self.clock.now()
        record = await self.sessions.db.run(
            lambda conn: self._snapshot_and_insert(
                conn,
                session_id=session_id,
                fence=fence,
                kind=kind,
                run_id=run_id,
                turn_index=turn_index,
                created_at=created_at,
            )
        )
        pruned = await self.checkpoints.prune(session_id, self.config.keep, fence=fence)
        if pruned:
            await self.recorder.record(
                session_id,
                EventType.CHECKPOINT_PRUNED,
                payload={"ids": pruned, "keep": self.config.keep},
                fence=fence,
            )
        await self.recorder.record(
            session_id,
            EventType.CHECKPOINT_CREATED,
            run_id=run_id,
            payload={
                "checkpoint_id": record.id,
                "kind": record.kind,
                "event_seq": record.event_seq,
                "message_seq": record.message_seq,
            },
            fence=fence,
        )
        if self.audit is not None:
            await self.audit.append(
                actor="agent",
                action="checkpoint.created",
                resource=record.id,
                detail={
                    "session_id": session_id,
                    "kind": record.kind,
                    "event_seq": record.event_seq,
                    "message_seq": record.message_seq,
                },
                session_id=session_id,
            )
        return record

    def _snapshot_and_insert(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        fence: LeaseFence,
        kind: str,
        run_id: str | None,
        turn_index: int | None,
        created_at: datetime,
    ) -> CheckpointRecord:
        with transaction(conn):
            assert_session_lease_row(conn, session_id, fence)
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                raise StorageError(f"unknown session: {session_id}")
            last_seq = int(row["last_seq"])

            message_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            message_seq = int(message_row["max_seq"])

            anchor_rows = conn.execute(
                """
                SELECT id, kind, text, source_message_seq, salience
                  FROM context_anchors
                 WHERE session_id = ? AND active = 1
                 ORDER BY salience DESC, created_at ASC
                 LIMIT ?
                """,
                (session_id, _ANCHOR_LIMIT),
            ).fetchall()
            compaction_rows = conn.execute(
                """
                SELECT id, level, covered_from_message_seq, covered_to_message_seq, created_at
                  FROM compactions
                 WHERE session_id = ?
                 ORDER BY created_at DESC, rowid DESC
                 LIMIT ?
                """,
                (session_id, _COMPACTION_LIMIT),
            ).fetchall()
            context_row = conn.execute(
                "SELECT generation, last_compaction_id FROM context_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            running_rows = conn.execute(
                """
                SELECT id, name, turn_id, idempotent
                  FROM tool_calls
                 WHERE session_id = ? AND status = 'running'
                 ORDER BY started_at ASC
                """,
                (session_id,),
            ).fetchall()
            recent_rows = conn.execute(
                """
                SELECT id, name, status, idempotent, finished_at
                  FROM tool_calls
                 WHERE session_id = ? AND status != 'running'
                 ORDER BY COALESCE(finished_at, started_at) DESC
                 LIMIT ?
                """,
                (session_id, _RECENT_TOOL_LIMIT),
            ).fetchall()
            usage_row = conn.execute(
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

            generation = int(context_row["generation"]) if context_row is not None else 0
            last_compaction_id: str | None = (
                str(context_row["last_compaction_id"])
                if context_row is not None and context_row["last_compaction_id"] is not None
                else (str(compaction_rows[0]["id"]) if compaction_rows else None)
            )
            state: dict[str, Any] = {
                "format": CHECKPOINT_FORMAT,
                "session": {
                    "id": str(row["id"]),
                    "project_id": str(row["project_id"]),
                    "cwd": str(row["cwd"]),
                    "title": row["title"],
                    "status": str(row["status"]),
                    "model": row["model"],
                    "provider": row["provider"],
                    "parent_session_id": row["parent_session_id"],
                    "fork_message_seq": (
                        int(row["fork_message_seq"])
                        if row["fork_message_seq"] is not None
                        else None
                    ),
                },
                "position": {"event_seq": last_seq, "message_seq": message_seq},
                "run": {"run_id": run_id, "last_turn": turn_index},
                "context": {
                    "generation": generation,
                    "last_compaction_id": last_compaction_id,
                    "anchors": [
                        {
                            "id": str(anchor["id"]),
                            "kind": str(anchor["kind"]),
                            "text": str(anchor["text"]),
                            "source_message_seq": (
                                int(anchor["source_message_seq"])
                                if anchor["source_message_seq"] is not None
                                else None
                            ),
                            "salience": float(anchor["salience"]),
                        }
                        for anchor in anchor_rows
                    ],
                    "compactions": [
                        {
                            "id": str(compaction["id"]),
                            "level": int(compaction["level"]),
                            "covered_from_message_seq": int(compaction["covered_from_message_seq"]),
                            "covered_to_message_seq": int(compaction["covered_to_message_seq"]),
                            "created_at": str(compaction["created_at"]),
                        }
                        for compaction in compaction_rows
                    ],
                },
                "tools": {
                    "running": [
                        {
                            "id": str(tool["id"]),
                            "name": str(tool["name"]),
                            "turn_id": tool["turn_id"],
                            "idempotent": bool(tool["idempotent"]),
                        }
                        for tool in running_rows
                    ],
                    "recent": [
                        {
                            "id": str(tool["id"]),
                            "name": str(tool["name"]),
                            "status": str(tool["status"]),
                            "idempotent": bool(tool["idempotent"]),
                            "finished_at": tool["finished_at"],
                        }
                        for tool in recent_rows
                    ],
                },
                "usage": {
                    "calls": int(usage_row["calls"]),
                    "input_tokens": int(usage_row["input_tokens"]),
                    "output_tokens": int(usage_row["output_tokens"]),
                    "cache_read_tokens": int(usage_row["cache_read_tokens"]),
                    "cache_write_tokens": int(usage_row["cache_write_tokens"]),
                },
                "tasks": (
                    self.task_snapshot(conn, session_id) if self.task_snapshot is not None else None
                ),
            }
            # Titles and anchors are user-controlled text; never persist a
            # known secret into a checkpoint state blob or its checksum.
            state = self.redactor.redact_obj(state)
            checksum = compute_checkpoint_checksum(
                session_id=session_id,
                kind=kind,
                event_seq=last_seq,
                message_seq=message_seq,
                state=state,
                schema_version=CHECKPOINT_SCHEMA_VERSION,
            )
            record = CheckpointRecord(
                id=new_id("ckpt"),
                session_id=session_id,
                run_id=run_id,
                kind=kind,
                event_seq=last_seq,
                message_seq=message_seq,
                state=state,
                checksum=checksum,
                schema_version=CHECKPOINT_SCHEMA_VERSION,
                created_at=created_at,
            )
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
                    _iso(created_at),
                ),
            )
            return record

    # ---------------------------------------------------------------- validate

    async def validate(self, record: CheckpointRecord) -> None:
        """Validate a checkpoint before anything trusts it.

        Raises :class:`CheckpointIncompatible` for an unknown schema version and
        :class:`CheckpointCorrupt` for a bad checksum, a malformed state blob or
        a position beyond the current transcript.
        """
        if record.schema_version != CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointIncompatible(
                f"checkpoint {record.id} uses schema version {record.schema_version}; "
                f"this build supports {CHECKPOINT_SCHEMA_VERSION}"
            )
        expected = compute_checkpoint_checksum(
            session_id=record.session_id,
            kind=record.kind,
            event_seq=record.event_seq,
            message_seq=record.message_seq,
            state=record.state,
            schema_version=record.schema_version,
        )
        if not hmac.compare_digest(expected, record.checksum):
            raise CheckpointCorrupt(f"checkpoint {record.id} checksum does not match its state")
        _validate_state_shape(record)
        session = await self.sessions.get(record.session_id)
        if session is None:
            raise CheckpointCorrupt(
                f"checkpoint {record.id} references unknown session {record.session_id}"
            )
        if record.event_seq > session.last_seq:
            raise CheckpointCorrupt(
                f"checkpoint {record.id} points at event seq {record.event_seq}, "
                f"beyond the session position {session.last_seq}"
            )
        max_message_seq = await self.sessions.max_message_seq(record.session_id)
        if record.message_seq > max_message_seq:
            raise CheckpointCorrupt(
                f"checkpoint {record.id} points at message seq {record.message_seq}, "
                f"beyond the transcript position {max_message_seq}"
            )

    async def latest_valid(self, session_id: str) -> CheckpointRecord | None:
        """Return the newest checkpoint if it is valid; otherwise ``None``.

        Corrupt or incompatible rows are reported with ``checkpoint.invalid``
        and ignored. The row is never deleted: the transcript stays the source
        of truth and an operator can inspect the bad row later.
        """
        try:
            record = await self.checkpoints.latest(session_id)
        except StorageError as exc:
            await self._record_invalid(session_id, None, str(exc))
            return None
        if record is None:
            return None
        try:
            await self.validate(record)
        except CheckpointError as exc:
            await self._record_invalid(session_id, record.id, str(exc))
            return None
        return record

    async def _record_invalid(
        self, session_id: str, checkpoint_id: str | None, reason: str
    ) -> None:
        await self.recorder.record(
            session_id,
            EventType.CHECKPOINT_INVALID,
            payload={"checkpoint_id": checkpoint_id, "reason": reason},
        )

    # ------------------------------------------------------------------- reads

    async def list(self, session_id: str, limit: int = 50) -> list[CheckpointRecord]:
        return await self.checkpoints.list_for_session(session_id, limit)

    async def get_raw(self, checkpoint_id: str) -> CheckpointRecord | None:
        """Load a checkpoint row without validating it (diagnostics only)."""
        return await self.checkpoints.get(checkpoint_id)

    async def get_valid(self, checkpoint_id: str) -> CheckpointRecord | None:
        """Load a checkpoint and validate it.

        Returns ``None`` when the row does not exist. Raises
        :class:`CheckpointError` when the row exists but is corrupt or
        incompatible, and :class:`StorageError` when its state JSON is
        unreadable.
        """
        record = await self.checkpoints.get(checkpoint_id)
        if record is None:
            return None
        await self.validate(record)
        return record


def _validate_state_shape(record: CheckpointRecord) -> None:
    state = record.state
    if state.get("format") != CHECKPOINT_FORMAT:
        raise CheckpointCorrupt(f"checkpoint {record.id} has an unknown state format")

    session = state.get("session")
    if not isinstance(session, dict):
        raise CheckpointCorrupt(f"checkpoint {record.id} is missing the session block")
    if session.get("id") != record.session_id:
        raise CheckpointCorrupt(
            f"checkpoint {record.id} state belongs to session {session.get('id')!r}"
        )

    position = state.get("position")
    if not isinstance(position, dict):
        raise CheckpointCorrupt(f"checkpoint {record.id} is missing the position block")
    if _int_field(position, "event_seq", record.id) != record.event_seq:
        raise CheckpointCorrupt(f"checkpoint {record.id} event_seq does not match its state")
    if _int_field(position, "message_seq", record.id) != record.message_seq:
        raise CheckpointCorrupt(f"checkpoint {record.id} message_seq does not match its state")

    run = state.get("run")
    if not isinstance(run, dict):
        raise CheckpointCorrupt(f"checkpoint {record.id} is missing the run block")
    if run.get("run_id") is not None and not isinstance(run.get("run_id"), str):
        raise CheckpointCorrupt(f"checkpoint {record.id} has an invalid run_id")
    if run.get("last_turn") is not None:
        _int_field(run, "last_turn", record.id)

    context = state.get("context")
    if not isinstance(context, dict):
        raise CheckpointCorrupt(f"checkpoint {record.id} is missing the context block")
    _int_field(context, "generation", record.id)
    last_compaction_id = context.get("last_compaction_id")
    if last_compaction_id is not None and not isinstance(last_compaction_id, str):
        raise CheckpointCorrupt(f"checkpoint {record.id} has an invalid last_compaction_id")
    if not isinstance(context.get("anchors"), list):
        raise CheckpointCorrupt(f"checkpoint {record.id} has an invalid anchors list")
    if not isinstance(context.get("compactions"), list):
        raise CheckpointCorrupt(f"checkpoint {record.id} has an invalid compactions list")

    tools = state.get("tools")
    if not isinstance(tools, dict):
        raise CheckpointCorrupt(f"checkpoint {record.id} is missing the tools block")
    if not isinstance(tools.get("running"), list):
        raise CheckpointCorrupt(f"checkpoint {record.id} has an invalid running tools list")
    if not isinstance(tools.get("recent"), list):
        raise CheckpointCorrupt(f"checkpoint {record.id} has an invalid recent tools list")

    usage = state.get("usage")
    if not isinstance(usage, dict):
        raise CheckpointCorrupt(f"checkpoint {record.id} is missing the usage block")
    for key in (
        "calls",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    ):
        _int_field(usage, key, record.id)

    tasks = state.get("tasks")
    if tasks is not None:
        if not isinstance(tasks, dict):
            raise CheckpointCorrupt(f"checkpoint {record.id} has an invalid tasks block")
        plan = tasks.get("plan")
        if plan is not None and not isinstance(plan, dict):
            raise CheckpointCorrupt(f"checkpoint {record.id} has an invalid task plan")
        if not isinstance(tasks.get("tasks", []), list):
            raise CheckpointCorrupt(f"checkpoint {record.id} has an invalid task list")
        if not isinstance(tasks.get("dependencies", {}), dict):
            raise CheckpointCorrupt(f"checkpoint {record.id} has invalid task dependencies")


def _int_field(container: dict[str, Any], key: str, checkpoint_id: str) -> int:
    value = container.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CheckpointCorrupt(f"checkpoint {checkpoint_id} has a non-integer {key!r}")
    return value
