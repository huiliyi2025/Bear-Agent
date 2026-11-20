from __future__ import annotations

from pathlib import Path

import pytest

from bear_agent.context.anchors import AnchorLedger
from bear_agent.context.tokens import estimate_text_tokens
from bear_agent.core.clock import FakeClock
from bear_agent.core.events import EventType
from bear_agent.core.fencing import LeaseFence
from bear_agent.core.messages import AssistantMessage, UserMessage
from bear_agent.observability.bus import EventBus
from bear_agent.observability.recorder import EventRecorder
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.sessions.models import SessionRecord
from bear_agent.sessions.service import SessionService
from bear_agent.storage.continuity_repositories import AnchorRepository
from bear_agent.storage.database import Database


async def _setup(
    tmp_path: Path,
) -> tuple[Database, SessionService, SessionRecord, LeaseFence, AnchorLedger, FakeClock]:
    db = Database(tmp_path / "bear.db")
    clock = FakeClock()
    sessions = SessionService(db, clock)
    session = await sessions.create(cwd=tmp_path, model="m", provider="p")
    epoch = await sessions.acquire_lease(session.id, "owner", 300)
    fence = LeaseFence(owner="owner", epoch=epoch)
    recorder = EventRecorder(sessions, EventBus(), SecretRedactor())
    ledger = AnchorLedger(AnchorRepository(db), clock=clock, recorder=recorder)
    return db, sessions, session, fence, ledger, clock


@pytest.mark.asyncio
async def test_add_dedupes_and_bumps_salience(tmp_path: Path) -> None:
    db, sessions, session, fence, ledger, clock = await _setup(tmp_path)
    try:
        first = await ledger.add(session.id, "Keep the public API stable", fence=fence)
        assert first.kind == "note"
        assert first.active
        clock.advance(1)
        duplicate = await ledger.add(
            session.id,
            "  keep   the   PUBLIC api STABLE ",
            fence=fence,
        )
        assert duplicate.id == first.id
        assert duplicate.salience > first.salience
        assert duplicate.updated_at > first.updated_at
        assert len(await ledger.active(session.id)) == 1

        events = [
            event
            for event in await sessions.events_for(session.id)
            if event.type == EventType.CONTEXT_ANCHOR_ADDED
        ]
        assert len(events) == 1
        assert events[0].payload["count"] == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_remove_deactivates_anchor(tmp_path: Path) -> None:
    db, _sessions, session, fence, ledger, _clock = await _setup(tmp_path)
    try:
        anchor = await ledger.add(session.id, "Temporary note", fence=fence)
        assert await ledger.remove(session.id, anchor.id, fence=fence) is True
        assert await ledger.active(session.id) == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_render_respects_budget_and_drops_low_salience(tmp_path: Path) -> None:
    db, _sessions, session, fence, ledger, _clock = await _setup(tmp_path)
    try:
        await ledger.add(session.id, "critical goal " + "g" * 100, salience=1.0, fence=fence)
        await ledger.add(session.id, "minor note " + "n" * 100, salience=0.2, fence=fence)
        anchors = await ledger.active(session.id)

        block = ledger.render(anchors, 40)
        assert estimate_text_tokens(block) <= 40
        assert block.startswith("<context-anchors>")
        assert block.endswith("</context-anchors>")
        assert "critical goal" in block
        assert "minor note" not in block

        assert ledger.render([], 100) == ""
        assert ledger.render(anchors, 0) == ""
    finally:
        db.close()


@pytest.mark.asyncio
async def test_observe_messages_creates_goal_and_constraints_idempotently(
    tmp_path: Path,
) -> None:
    db, _sessions, session, fence, ledger, _clock = await _setup(tmp_path)
    try:
        messages = [
            UserMessage.of("Build a CLI").model_copy(update={"seq": 1}),
            AssistantMessage.of("ok").model_copy(update={"seq": 2}),
            UserMessage.of("You must never delete user data").model_copy(update={"seq": 3}),
            UserMessage.of("Please add tests").model_copy(update={"seq": 4}),
            UserMessage.of("数据不能改").model_copy(update={"seq": 5}),
        ]
        added = await ledger.observe_messages(session.id, messages, fence=fence)
        assert added == 3

        anchors = await ledger.active(session.id)
        assert sorted(anchor.kind for anchor in anchors) == [
            "constraint",
            "constraint",
            "goal",
        ]
        goal = next(anchor for anchor in anchors if anchor.kind == "goal")
        assert goal.text == "Build a CLI"
        assert goal.source_message_seq == 1

        assert await ledger.observe_messages(session.id, messages, fence=fence) == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_anchor_text_is_truncated(tmp_path: Path) -> None:
    db, _sessions, session, fence, ledger, _clock = await _setup(tmp_path)
    try:
        anchor = await ledger.add(session.id, "x" * 1500, fence=fence)
        assert len(anchor.text) == 1000
    finally:
        db.close()


@pytest.mark.asyncio
async def test_anchors_survive_database_reload(tmp_path: Path) -> None:
    db, _sessions, session, fence, ledger, clock = await _setup(tmp_path)
    anchor = await ledger.add(session.id, "persistent goal", kind="goal", fence=fence)
    db.close()

    reopened = Database(tmp_path / "bear.db")
    try:
        reloaded = AnchorLedger(AnchorRepository(reopened), clock=clock)
        active = await reloaded.active(session.id)
        assert [item.id for item in active] == [anchor.id]
        assert active[0].text == "persistent goal"
        assert active[0].kind == "goal"
    finally:
        reopened.close()
