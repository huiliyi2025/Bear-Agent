"""Transcript and event writes are atomic and lease-fenced."""

from __future__ import annotations

from pathlib import Path

import pytest

from bear_agent.core.errors import SessionLeaseError
from bear_agent.core.events import EventType
from bear_agent.core.fencing import LeaseFence, reset_fence, set_fence
from bear_agent.core.messages import UserMessage
from bear_agent.sessions.service import SessionService
from bear_agent.storage.database import Database


async def test_event_failure_rolls_back_message_and_sequence(tmp_path: Path) -> None:
    db = Database(tmp_path / "atomic.db")
    try:
        service = SessionService(db)
        session = await service.create(cwd=Path.cwd(), model="m", provider="p")
        epoch = await service.acquire_lease(session.id, "owner", 60)
        fence = LeaseFence("owner", epoch)
        token = set_fence(fence)
        try:
            db.connection.execute(
                """
                CREATE TRIGGER fail_user_event
                BEFORE INSERT ON events
                WHEN NEW.type = 'message.user'
                BEGIN
                    SELECT RAISE(ABORT, 'injected event failure');
                END
                """
            )
            with pytest.raises(Exception, match="injected event failure"):
                await service.append_message(
                    session.id,
                    UserMessage.of("will roll back"),
                    event_type=EventType.MESSAGE_USER,
                )
            db.connection.execute("DROP TRIGGER fail_user_event")
        finally:
            reset_fence(token)

        assert await service.load_messages(session.id) == []
        record = await service.get(session.id)
        assert record is not None
        assert record.last_seq == 1  # session.created only
        events = await service.events_for(session.id)
        assert [event.type for event in events] == [EventType.SESSION_CREATED]
    finally:
        db.close()


async def test_stale_owner_cannot_append_after_takeover(tmp_path: Path) -> None:
    db = Database(tmp_path / "takeover.db")
    try:
        service = SessionService(db)
        session = await service.create(cwd=Path.cwd(), model="m", provider="p")
        epoch1 = await service.acquire_lease(session.id, "owner-1", 60)
        old = LeaseFence("owner-1", epoch1)
        token = set_fence(old)
        try:
            await service.append_message(
                session.id,
                UserMessage.of("before"),
                event_type=EventType.MESSAGE_USER,
            )
        finally:
            reset_fence(token)
        await service.release_lease(session.id, "owner-1", epoch1)
        await service.acquire_lease(session.id, "owner-2", 60)

        stale = set_fence(old)
        try:
            with pytest.raises(SessionLeaseError):
                await service.append_message(
                    session.id,
                    UserMessage.of("after"),
                    event_type=EventType.MESSAGE_USER,
                )
        finally:
            reset_fence(stale)
        messages = await service.load_messages(session.id)
        assert [message.text_content() for message in messages] == ["before"]
    finally:
        db.close()
