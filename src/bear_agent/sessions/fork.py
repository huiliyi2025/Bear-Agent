"""Session fork.

A fork is a new session whose transcript is a prefix copy of another session.
The source is never modified and never locked: the copy is taken in one SQLite
read snapshot transaction, so a running source session is safe to fork.

Only conversational state is copied: the message prefix (same sequence numbers
and identical ``content_json``) and active context anchors (new ids). Execution
state is deliberately not copied - no tool calls, model calls, process records,
file claims, approvals, checkpoints, compactions or context state. A fork starts
with a clean slate. Project-scoped approvals still apply to the fork because
approval lookup already falls back to ``project_id``; that is existing
security semantics, not something this service changes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

from bear_agent.audit.service import AuditService
from bear_agent.core.clock import Clock, SystemClock
from bear_agent.core.errors import CheckpointError, ForkError, StorageError
from bear_agent.core.events import Event, EventType
from bear_agent.core.ids import new_id
from bear_agent.core.messages import Message
from bear_agent.observability.recorder import EventRecorder
from bear_agent.sessions.checkpoints import CheckpointService
from bear_agent.sessions.models import SessionRecord
from bear_agent.sessions.service import SessionService
from bear_agent.storage.continuity_repositories import AnchorRepository
from bear_agent.storage.database import transaction
from bear_agent.storage.repositories import _iso

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ForkResult:
    session: SessionRecord
    cut_message_seq: int
    source_session_id: str
    checkpoint_id: str | None


def safe_cut(messages: list[Message], cut: int) -> int:
    """Move a cut back so a fork never ends inside a tool-call pair.

    If an assistant message at or before the cut calls a tool whose result is
    missing or lies beyond the cut, the cut moves to just before that assistant
    message. The loop handles stacked cases (for example a truncated multi-tool
    turn) by walking backwards until every assistant tool call inside the cut
    has a matching tool result inside the cut.
    """
    if cut <= 0:
        return 0
    ordered = sorted(
        (message for message in messages if message.seq is not None),
        key=lambda message: message.seq or 0,
    )
    result_seqs: dict[str, list[int]] = {}
    for message in ordered:
        result = message.tool_result()
        if message.role == "tool" and result is not None and message.seq is not None:
            result_seqs.setdefault(result.tool_call_id, []).append(message.seq)

    current = cut
    while current > 0:
        offender: int | None = None
        for message in ordered:
            if message.seq is None or message.seq > current or message.role != "assistant":
                continue
            for call in message.tool_calls():
                seqs = result_seqs.get(call.id, [])
                if not any(message.seq < seq <= current for seq in seqs):
                    offender = message.seq
                    break
            if offender is not None:
                break
        if offender is None:
            break
        current = offender - 1
    return max(current, 0)


class SessionForkService:
    def __init__(
        self,
        *,
        sessions: SessionService,
        anchors: AnchorRepository,
        recorder: EventRecorder,
        audit: AuditService | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.sessions = sessions
        self.anchors = anchors
        self.recorder = recorder
        self.audit = audit
        self.clock = clock or SystemClock()

    async def fork(
        self,
        source_session_id: str,
        *,
        at_message_seq: int | None = None,
        checkpoint_id: str | None = None,
        title: str | None = None,
        checkpoints: CheckpointService | None = None,
    ) -> ForkResult:
        """Fork ``source_session_id`` at a message position.

        The cut is resolved from ``checkpoint_id``, then ``at_message_seq`` and
        finally the current transcript end. Supplying both cut selectors is an
        error. The cut is moved back when necessary by :func:`safe_cut`, so the
        returned ``cut_message_seq`` is the effective, balanced position.
        """
        if at_message_seq is not None and checkpoint_id is not None:
            raise ForkError("specify either at_message_seq or checkpoint_id, not both")
        source = await self.sessions.get(source_session_id)
        if source is None:
            raise ForkError(f"unknown session: {source_session_id}")
        max_seq = await self.sessions.max_message_seq(source_session_id)

        resolved_checkpoint_id = checkpoint_id
        if checkpoint_id is not None:
            if checkpoints is None:
                raise ForkError("checkpoint_id requires a CheckpointService")
            try:
                record = await checkpoints.get_valid(checkpoint_id)
            except (CheckpointError, StorageError) as exc:
                raise ForkError(f"checkpoint {checkpoint_id} is invalid: {exc}") from exc
            if record is None:
                raise ForkError(f"unknown checkpoint: {checkpoint_id}")
            if record.session_id != source_session_id:
                raise ForkError(
                    f"checkpoint {checkpoint_id} belongs to session "
                    f"{record.session_id}, not {source_session_id}"
                )
            requested_cut = record.message_seq
        elif at_message_seq is not None:
            requested_cut = at_message_seq
        else:
            requested_cut = max_seq

        if requested_cut < 0 or requested_cut > max_seq:
            raise ForkError(f"cut {requested_cut} is outside the transcript range 0..{max_seq}")

        new_session, cut, _created_event, forked_event = await self.sessions.db.run(
            lambda conn: self._fork_sync(
                conn,
                source_session_id=source_session_id,
                requested_cut=requested_cut,
                checkpoint_id=resolved_checkpoint_id,
                title=title,
            )
        )
        # The event row is already durable; publish it now so live observers see
        # the fork. Nothing else is written to the source session.
        await self.recorder.publish_message_event(forked_event)
        if self.audit is not None:
            try:
                await self.audit.append(
                    actor="agent",
                    action="session.forked",
                    resource=new_session.id,
                    detail={
                        "source_session_id": source_session_id,
                        "cut_message_seq": cut,
                        "checkpoint_id": resolved_checkpoint_id,
                        "title": new_session.title,
                    },
                    session_id=new_session.id,
                )
            except Exception:
                # The fork row and its events are already durable; an audit
                # sink failure must not make the caller retry and create a
                # second session. The event remains the local record.
                logger.exception("fork audit record failed for %s", new_session.id)
        return ForkResult(
            session=new_session,
            cut_message_seq=cut,
            source_session_id=source_session_id,
            checkpoint_id=resolved_checkpoint_id,
        )

    # ------------------------------------------------------------------ sync

    def _fork_sync(
        self,
        conn: sqlite3.Connection,
        *,
        source_session_id: str,
        requested_cut: int,
        checkpoint_id: str | None,
        title: str | None,
    ) -> tuple[SessionRecord, int, Event, Event]:
        with transaction(conn):
            source = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (source_session_id,)
            ).fetchone()
            if source is None:
                raise ForkError(f"unknown session: {source_session_id}")

            message_rows = conn.execute(
                """
                SELECT seq, role, content_json, token_estimate, created_at
                  FROM messages
                 WHERE session_id = ?
                 ORDER BY seq ASC
                """,
                (source_session_id,),
            ).fetchall()
            messages: list[Message] = []
            for row in message_rows:
                try:
                    message = Message.model_validate_json(row["content_json"])
                except ValueError as exc:
                    raise ForkError(
                        f"session {source_session_id} has an unreadable message "
                        f"at seq {row['seq']}: {exc}"
                    ) from exc
                messages.append(message.model_copy(update={"seq": int(row["seq"])}))
            max_seq = max((int(row["seq"]) for row in message_rows), default=0)
            if requested_cut > max_seq:
                raise ForkError(f"cut {requested_cut} is outside the transcript range 0..{max_seq}")
            cut = safe_cut(messages, requested_cut)
            selected = [row for row in message_rows if int(row["seq"]) <= cut]

            now = self.clock.now()
            new_session = SessionRecord(
                id=new_id("ses"),
                project_id=str(source["project_id"]),
                cwd=str(source["cwd"]),
                title=title if title is not None else source["title"],
                status="active",
                model=source["model"],
                provider=source["provider"],
                last_seq=2,
                parent_session_id=source_session_id,
                fork_message_seq=cut,
                fork_checkpoint_id=checkpoint_id,
                created_at=now,
                updated_at=now,
            )
            conn.execute(
                """
                INSERT INTO sessions (
                    id, project_id, cwd, title, status, model, provider, last_seq,
                    lease_owner, lease_epoch, lease_expires_at, parent_session_id,
                    fork_message_seq, fork_checkpoint_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    new_session.id,
                    new_session.project_id,
                    new_session.cwd,
                    new_session.title,
                    new_session.status,
                    new_session.model,
                    new_session.provider,
                    new_session.last_seq,
                    new_session.parent_session_id,
                    new_session.fork_message_seq,
                    new_session.fork_checkpoint_id,
                    _iso(new_session.created_at),
                    _iso(new_session.updated_at),
                ),
            )

            for row in selected:
                conn.execute(
                    """
                    INSERT INTO messages (
                        session_id, seq, role, content_json, token_estimate, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_session.id,
                        int(row["seq"]),
                        row["role"],
                        row["content_json"],
                        row["token_estimate"],
                        row["created_at"],
                    ),
                )

            anchor_rows = conn.execute(
                """
                SELECT * FROM context_anchors
                 WHERE session_id = ? AND active = 1
                   AND (source_message_seq IS NULL OR source_message_seq <= ?)
                 ORDER BY salience DESC, created_at ASC
                """,
                (source_session_id, cut),
            ).fetchall()
            for anchor in anchor_rows:
                conn.execute(
                    """
                    INSERT INTO context_anchors (
                        id, session_id, kind, text, source_message_seq,
                        salience, active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        new_id("anchor"),
                        new_session.id,
                        anchor["kind"],
                        anchor["text"],
                        anchor["source_message_seq"],
                        float(anchor["salience"]),
                        _iso(now),
                        _iso(now),
                    ),
                )

            created_event = Event(
                session_id=new_session.id,
                seq=1,
                type=EventType.SESSION_CREATED,
                ts=now,
                payload={
                    "cwd": new_session.cwd,
                    "model": new_session.model,
                    "provider": new_session.provider,
                    "parent_session_id": source_session_id,
                    "fork_message_seq": cut,
                },
            )
            forked_event = Event(
                session_id=new_session.id,
                seq=2,
                type=EventType.SESSION_FORKED,
                ts=now,
                payload={
                    "source_session_id": source_session_id,
                    "cut_message_seq": cut,
                    "checkpoint_id": checkpoint_id,
                },
            )
            for event in (created_event, forked_event):
                conn.execute(
                    """
                    INSERT INTO events (
                        session_id, seq, event_id, type, run_id, turn_id,
                        span_id, parent_span_id, schema_version, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_session.id,
                        event.seq,
                        event.event_id,
                        event.type.value,
                        event.run_id,
                        event.turn_id,
                        event.span_id,
                        event.parent_span_id,
                        1,
                        json.dumps(event.payload, ensure_ascii=False, default=str),
                        _iso(event.ts),
                    ),
                )
            return new_session, cut, created_event, forked_event


__all__ = ["ForkResult", "SessionForkService", "safe_cut"]
