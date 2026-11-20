"""Lease epoch fencing for transcript, event and tool state writes."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bear_agent.core.errors import SessionLeaseError
from bear_agent.core.events import EventType
from bear_agent.core.fencing import LeaseFence, reset_fence, resolve_fence, set_fence
from bear_agent.core.messages import UserMessage
from bear_agent.core.usage import TokenUsage
from bear_agent.sessions.service import SessionService
from bear_agent.storage.database import Database


async def _session(db: Database) -> str:
    service = SessionService(db)
    record = await service.create(cwd=Path.cwd(), model="m", provider="p")
    return record.id


async def test_old_owner_cannot_write_transcript_or_events(tmp_path: Path) -> None:
    db = Database(tmp_path / "fence.db")
    try:
        session_id = await _session(db)
        service = SessionService(db)
        epoch1 = await service.acquire_lease(session_id, "owner-1", 60)
        old = LeaseFence("owner-1", epoch1)
        token = set_fence(old)
        try:
            await service.append_message(
                session_id,
                UserMessage.of("hello"),
                event_type=EventType.MESSAGE_USER,
            )
            await service.append_event(session_id, EventType.RUN_STARTED, payload={"run": 1})
        finally:
            reset_fence(token)

        await service.release_lease(session_id, "owner-1", epoch1)
        epoch2 = await service.acquire_lease(session_id, "owner-2", 60)
        assert epoch2 == epoch1 + 1

        stale_token = set_fence(old)
        try:
            with pytest.raises(SessionLeaseError):
                await service.append_message(
                    session_id,
                    UserMessage.of("stale"),
                    event_type=EventType.MESSAGE_USER,
                )
            with pytest.raises(SessionLeaseError):
                await service.append_event(session_id, EventType.RUN_STARTED, payload={"run": 2})
            with pytest.raises(SessionLeaseError):
                await service.set_status(session_id, "archived")
            with pytest.raises(SessionLeaseError):
                await service.tool_calls.start(session_id, None, "call-1", "read_file", {}, True)
            with pytest.raises(SessionLeaseError):
                await service.model_calls.record(
                    call_id="mc-1",
                    session_id=session_id,
                    turn_id=None,
                    provider="p",
                    model="m",
                    usage=TokenUsage(),
                    latency_ms=1,
                    status="completed",
                )
            with pytest.raises(SessionLeaseError):
                await service.mark_interrupted_tools_unknown(session_id)
        finally:
            reset_fence(stale_token)

        messages = await service.load_messages(session_id)
        assert [message.text_content() for message in messages] == ["hello"]
        events = await service.events_for(session_id)
        assert [event.type for event in events] == [
            EventType.SESSION_CREATED,
            EventType.MESSAGE_USER,
            EventType.RUN_STARTED,
        ]
        record = await service.get(session_id)
        assert record is not None and record.status == "active"
    finally:
        db.close()


async def test_background_task_inherits_stale_fence(tmp_path: Path) -> None:
    db = Database(tmp_path / "fence-bg.db")
    try:
        session_id = await _session(db)
        service = SessionService(db)
        epoch1 = await service.acquire_lease(session_id, "owner-1", 60)
        old = LeaseFence("owner-1", epoch1)
        token = set_fence(old)
        seen: list[str] = []
        release = asyncio.Event()

        async def late_writer() -> None:
            await release.wait()
            try:
                await service.append_event(
                    session_id, EventType.RUN_COMPLETED, payload={"late": True}
                )
            except SessionLeaseError:
                seen.append("fenced")

        task = asyncio.create_task(late_writer())
        await asyncio.sleep(0)
        reset_fence(token)
        await service.release_lease(session_id, "owner-1", epoch1)
        await service.acquire_lease(session_id, "owner-2", 60)
        release.set()
        await asyncio.wait_for(task, timeout=5)
        assert seen == ["fenced"]
        assert resolve_fence() is None
    finally:
        db.close()


async def test_runtime_run_does_not_leak_ambient_fence(workspace: Path) -> None:
    from tests.support import default_settings, make_container, new_session, text_script

    from bear_agent.runtime.stop_policy import RunStatus

    settings = default_settings(workspace.parent / "fence-runtime")
    container, _provider = make_container(settings, workspace, [text_script("done")])
    try:
        session = await new_session(container, workspace)
        result = await container.runtime.run(session.id, "hello")
        assert result.status == RunStatus.COMPLETED
        assert resolve_fence() is None
        record = await container.sessions.get(session.id)
        assert record is not None
        assert record.lease_owner is None
        assert record.status == "idle"
    finally:
        await container.close()


async def test_concurrent_lease_acquisition_only_one_wins(tmp_path: Path) -> None:
    path = tmp_path / "lease.db"
    db1 = Database(path)
    db2 = Database(path)
    try:
        service1 = SessionService(db1)
        service2 = SessionService(db2)
        record = await service1.create(cwd=Path.cwd(), model="m", provider="p")
        results = await asyncio.gather(
            service1.acquire_lease(record.id, "a", 60),
            service2.acquire_lease(record.id, "b", 60),
            return_exceptions=True,
        )
        successes = [value for value in results if isinstance(value, int)]
        failures = [value for value in results if isinstance(value, SessionLeaseError)]
        assert len(successes) == 1
        assert len(failures) == 1
        current = await service1.get(record.id)
        assert current is not None
        assert current.lease_owner in {"a", "b"}
        assert current.lease_epoch == successes[0]
    finally:
        db1.close()
        db2.close()
