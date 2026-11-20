"""Session application service.

Owns session creation, transcript appends and leases. Message rows and their
corresponding fact events are written in the same transaction so a crash can
never leave a visible message without its audit trail (or vice versa).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from bear_agent.core.clock import Clock, SystemClock
from bear_agent.core.errors import StorageError
from bear_agent.core.events import Event, EventType
from bear_agent.core.fencing import LeaseFence, resolve_fence
from bear_agent.core.messages import (
    AssistantMessage,
    Message,
    ToolMessage,
    UserMessage,
)
from bear_agent.core.usage import UsageSummary
from bear_agent.sessions.models import SessionRecord
from bear_agent.storage.database import Database, transaction
from bear_agent.storage.repositories import (
    EventRepository,
    MessageRepository,
    ModelCallRepository,
    SessionRepository,
    ToolCallRepository,
    _iso,
    assert_session_lease_row,
)


class SessionService:
    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self.db = db
        self.clock = clock or SystemClock()
        self.sessions = SessionRepository(db)
        self.events = EventRepository(db)
        self.messages = MessageRepository(db)
        self.tool_calls = ToolCallRepository(db)
        self.model_calls = ModelCallRepository(db)

    # ------------------------------------------------------------------ create

    async def create(
        self,
        *,
        cwd: str | Path,
        model: str | None,
        provider: str | None,
        title: str | None = None,
        publish: Callable[[Event], Awaitable[None]] | None = None,
    ) -> SessionRecord:
        session = SessionRecord.new(cwd=cwd, model=model, provider=provider, clock=self.clock)
        if title:
            session.title = title
        await self.sessions.create(session)
        events = await self.events.append(
            session.id,
            [
                Event(
                    session_id=session.id,
                    type=EventType.SESSION_CREATED,
                    ts=session.created_at,
                    payload={
                        "cwd": session.cwd,
                        "model": session.model,
                        "provider": session.provider,
                    },
                )
            ],
        )
        if publish is not None and events:
            await publish(events[-1])
        return session.model_copy(update={"last_seq": events[-1].seq if events else 0})

    async def get(self, session_id: str) -> SessionRecord | None:
        return await self.sessions.get(session_id)

    async def latest(self, cwd: str | Path) -> SessionRecord | None:
        return await self.sessions.latest_for_cwd(cwd)

    async def list_recent(self, limit: int = 50) -> list[SessionRecord]:
        return await self.sessions.list_recent(limit)

    async def set_title(
        self, session_id: str, title: str, *, fence: LeaseFence | None = None
    ) -> None:
        await self.sessions.update_title(session_id, title, fence=fence)

    async def set_status(
        self, session_id: str, status: str, *, fence: LeaseFence | None = None
    ) -> None:
        await self.sessions.update_status(session_id, status, fence=fence)

    async def assert_lease(self, session_id: str, fence: LeaseFence) -> None:
        await self.sessions.assert_lease(session_id, fence.owner, epoch=fence.epoch)

    # --------------------------------------------------------------- transcript

    async def append_message(
        self,
        session_id: str,
        message: Message,
        *,
        event_type: EventType,
        payload: dict[str, Any] | None = None,
        run_id: str | None = None,
        turn_id: str | None = None,
        token_estimate: int | None = None,
        fence: LeaseFence | None = None,
    ) -> tuple[int, Event]:
        fence = resolve_fence(fence)
        event = Event(
            session_id=session_id,
            type=event_type,
            run_id=run_id,
            turn_id=turn_id,
            payload=payload or {},
            ts=self.clock.now(),
        )
        return await self.db.run(
            lambda conn: self._append_message_sync(
                conn, session_id, message, event, token_estimate, fence
            )
        )

    def _append_message_sync(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        message: Message,
        event: Event,
        token_estimate: int | None,
        fence: LeaseFence | None = None,
    ) -> tuple[int, Event]:
        with transaction(conn):
            if fence is not None:
                assert_session_lease_row(conn, session_id, fence)
            row = conn.execute(
                "SELECT last_seq FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise StorageError(f"unknown session: {session_id}")
            now = _iso(event.ts)
            msg_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            message_seq = int(msg_row["max_seq"]) + 1
            conn.execute(
                """
                INSERT INTO messages (session_id, seq, role, content_json, token_estimate, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    message_seq,
                    message.role,
                    message.model_dump_json(),
                    token_estimate,
                    now,
                ),
            )
            event_seq = int(row["last_seq"]) + 1
            stored = event.with_seq(event_seq)
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
                    now,
                ),
            )
            conn.execute(
                "UPDATE sessions SET last_seq = ?, updated_at = ? WHERE id = ?",
                (event_seq, now, session_id),
            )
            enriched = stored.model_copy(
                update={"payload": {**stored.payload, "message_seq": message_seq}}
            )
            return message_seq, enriched

    async def append_user_message(
        self,
        session_id: str,
        text: str,
        *,
        run_id: str | None = None,
        fence: LeaseFence | None = None,
    ) -> tuple[int, Event]:
        return await self.append_message(
            session_id,
            UserMessage.of(text),
            event_type=EventType.MESSAGE_USER,
            run_id=run_id,
            payload={"chars": len(text)},
            fence=fence,
        )

    async def append_assistant_message(
        self,
        session_id: str,
        message: AssistantMessage,
        *,
        run_id: str | None = None,
        turn_id: str | None = None,
        payload: dict[str, Any] | None = None,
        fence: LeaseFence | None = None,
    ) -> tuple[int, Event]:
        return await self.append_message(
            session_id,
            message,
            event_type=EventType.MESSAGE_ASSISTANT,
            run_id=run_id,
            turn_id=turn_id,
            payload=payload or {},
            fence=fence,
        )

    async def append_tool_message(
        self,
        session_id: str,
        message: ToolMessage,
        *,
        run_id: str | None = None,
        turn_id: str | None = None,
        payload: dict[str, Any] | None = None,
        fence: LeaseFence | None = None,
    ) -> tuple[int, Event]:
        return await self.append_message(
            session_id,
            message,
            event_type=EventType.MESSAGE_TOOL,
            run_id=run_id,
            turn_id=turn_id,
            payload=payload or {},
            fence=fence,
        )

    async def load_messages(self, session_id: str) -> list[Message]:
        return await self.messages.load(session_id)

    # ------------------------------------------------------------------- leases

    async def acquire_lease(self, session_id: str, owner: str, ttl_seconds: float = 300.0) -> int:
        return await self.sessions.acquire_lease(session_id, owner, ttl_seconds)

    async def renew_lease(
        self, session_id: str, owner: str, epoch: int, ttl_seconds: float = 300.0
    ) -> bool:
        return await self.sessions.renew_lease(session_id, owner, epoch, ttl_seconds)

    async def release_lease(self, session_id: str, owner: str, epoch: int) -> None:
        await self.sessions.release_lease(session_id, owner, epoch)

    # --------------------------------------------------------------- recovery

    async def mark_interrupted_tools_unknown(
        self, session_id: str, *, fence: LeaseFence | None = None
    ) -> int:
        """After a crash, running tool calls are not known to have completed."""
        return await self.tool_calls.mark_running_unknown(session_id, fence=fence)

    async def max_message_seq(self, session_id: str) -> int:
        return await self.messages.max_seq(session_id)

    async def tool_calls_for(self, session_id: str, limit: int = 500) -> list[dict[str, Any]]:
        return await self.tool_calls.list_for_session(session_id, limit)

    async def running_tool_calls(self, session_id: str) -> list[dict[str, Any]]:
        return await self.tool_calls.list_running(session_id)

    async def usage_summary(self, session_id: str) -> UsageSummary:
        return await self.model_calls.usage_summary(session_id)

    # --------------------------------------------------------------- for tests

    async def events_for(self, session_id: str, after_seq: int = 0) -> list[Event]:
        return await self.events.load(session_id, after_seq=after_seq)

    async def append_event(
        self,
        session_id: str,
        event_type: EventType,
        *,
        payload: dict[str, Any] | None = None,
        run_id: str | None = None,
        turn_id: str | None = None,
        fence: LeaseFence | None = None,
    ) -> Event | None:
        events = await self.events.append(
            session_id,
            [
                Event(
                    session_id=session_id,
                    type=event_type,
                    run_id=run_id,
                    turn_id=turn_id,
                    payload=payload or {},
                    ts=self.clock.now(),
                )
            ],
            fence=fence,
        )
        return events[0] if events else None
