from __future__ import annotations

from pathlib import Path

import pytest

from bear_agent.core.errors import SessionLeaseError, StorageError
from bear_agent.core.events import Event, EventType
from bear_agent.sessions.models import SessionRecord
from bear_agent.sessions.service import SessionService
from bear_agent.storage.database import Database
from bear_agent.storage.repositories import EventRepository


@pytest.mark.asyncio
async def test_session_and_message_persistence(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    try:
        service = SessionService(db)
        session = await service.create(cwd=tmp_path, model="m", provider="p")
        assert session.last_seq == 1  # session.created
        seq, event = await service.append_user_message(session.id, "hello")
        assert seq == 1
        assert event.seq == 2
        messages = await service.load_messages(session.id)
        assert len(messages) == 1
        assert messages[0].text_content() == "hello"
        events = await service.events_for(session.id)
        assert [e.type for e in events] == [
            EventType.SESSION_CREATED,
            EventType.MESSAGE_USER,
        ]
        assert (await service.get(session.id)).last_seq == 2  # type: ignore[union-attr]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_lease_is_exclusive_and_recoverable(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    try:
        service = SessionService(db)
        session = await service.create(cwd=tmp_path, model="m", provider="p")
        epoch1 = await service.acquire_lease(session.id, "owner-a", ttl_seconds=60)
        assert epoch1 == 1
        with pytest.raises(SessionLeaseError):
            await service.acquire_lease(session.id, "owner-b", ttl_seconds=60)
        assert await service.renew_lease(session.id, "owner-a", epoch1, 60)
        await service.release_lease(session.id, "owner-a", epoch1)
        epoch2 = await service.acquire_lease(session.id, "owner-b", ttl_seconds=60)
        assert epoch2 == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_event_sequence_conflict(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    try:
        service = SessionService(db)
        session = await service.create(cwd=tmp_path, model="m", provider="p")
        events = EventRepository(db)
        with pytest.raises(StorageError, match="sequence conflict"):
            await events.append(
                session.id,
                [
                    Event(
                        session_id=session.id,
                        type=EventType.RUN_STARTED,
                    )
                ],
                expected_seq=0,
            )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_interrupted_tool_marked_unknown(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    try:
        service = SessionService(db)
        session = await service.create(cwd=tmp_path, model="m", provider="p")
        await service.tool_calls.start(
            session.id, "turn", "call-1", "read_file", {"path": "x"}, True
        )
        assert (await service.tool_calls.get("call-1"))["status"] == "running"  # type: ignore[index]
        changed = await service.mark_interrupted_tools_unknown(session.id)
        assert changed == 1
        row = await service.tool_calls.get("call-1")
        assert row is not None and row["status"] == "unknown"
    finally:
        db.close()


def test_session_project_id(tmp_path: Path) -> None:
    session = SessionRecord.new(cwd=tmp_path, model="m", provider="p")
    assert len(session.project_id) == 32
    assert session.cwd == str(tmp_path.resolve())
