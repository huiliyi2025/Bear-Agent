from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from bear_agent.audit.service import AuditService
from bear_agent.config.models import CheckpointConfig
from bear_agent.core.clock import FakeClock
from bear_agent.core.errors import ForkError
from bear_agent.core.events import Event, EventType
from bear_agent.core.fencing import LeaseFence
from bear_agent.core.messages import (
    AssistantMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from bear_agent.observability.bus import EventBus
from bear_agent.observability.recorder import EventRecorder
from bear_agent.sessions.checkpoints import CheckpointService
from bear_agent.sessions.fork import SessionForkService, safe_cut
from bear_agent.sessions.service import SessionService
from bear_agent.storage.continuity_repositories import (
    AnchorRecord,
    AnchorRepository,
    CheckpointRepository,
    CompactionRepository,
    ContextStateRepository,
)
from bear_agent.storage.database import Database
from bear_agent.storage.security_repositories import ApprovalRecord, ApprovalRepository


class Harness:
    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.db = Database(tmp_path / "bear.db")
        self.clock = FakeClock(datetime.now(UTC))
        self.sessions = SessionService(self.db, self.clock)
        self.bus = EventBus()
        self.recorder = EventRecorder(self.sessions, self.bus)
        self.audit = AuditService(self.db, clock=self.clock)
        self.anchors = AnchorRepository(self.db)
        self.approvals = ApprovalRepository(self.db)
        self.checkpoints = CheckpointService(
            sessions=self.sessions,
            checkpoints=CheckpointRepository(self.db),
            compactions=CompactionRepository(self.db),
            anchors=self.anchors,
            context_state=ContextStateRepository(self.db),
            recorder=self.recorder,
            audit=self.audit,
            config=CheckpointConfig(),
            clock=self.clock,
        )
        self.forker = SessionForkService(
            sessions=self.sessions,
            anchors=self.anchors,
            recorder=self.recorder,
            audit=self.audit,
            clock=self.clock,
        )
        self.events: list[Event] = []
        self.bus.subscribe(self._capture)

    async def _capture(self, event: Event) -> None:
        self.events.append(event)

    async def source(self, *, title: str = "source"):
        session = await self.sessions.create(
            cwd=self.tmp_path, model="m", provider="p", title=title
        )
        await self.sessions.append_user_message(session.id, "hello")
        await self.sessions.append_assistant_message(session.id, AssistantMessage.of("hi"))
        await self.sessions.append_user_message(session.id, "do a thing")
        await self.sessions.append_assistant_message(
            session.id,
            AssistantMessage.with_tool_calls(
                "running",
                [ToolCall(id="call-1", name="bash", arguments={"command": "echo hi"})],
            ),
        )
        await self.sessions.append_tool_message(session.id, ToolMessage.of("call-1", "done"))
        await self.sessions.append_assistant_message(session.id, AssistantMessage.of("finished"))
        return session

    async def session_row(self, session_id: str) -> dict:
        def _row(conn):
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            return dict(row) if row else {}

        return await self.db.run(_row)

    async def count(self, table: str, session_id: str) -> int:
        def _count(conn):
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE session_id = ?", (session_id,)
            ).fetchone()
            return int(row["n"])

        return await self.db.run(_count)

    def close(self) -> None:
        self.db.close()


def _seqs(messages) -> list[int | None]:
    return [message.seq for message in messages]


def test_safe_cut_backs_off_an_unbalanced_tool_call() -> None:
    messages = [
        UserMessage.of("first"),
        AssistantMessage.of("hi"),
        UserMessage.of("do a thing"),
        AssistantMessage.with_tool_calls(None, [ToolCall(id="call-1", name="bash", arguments={})]),
        ToolMessage.of("call-1", "done"),
        AssistantMessage.of("finished"),
    ]
    for index, message in enumerate(messages, start=1):
        message.seq = index

    # Cut 4 lands on the assistant message whose result is at 5.
    assert safe_cut(messages, 4) == 3
    assert safe_cut(messages, 5) == 5
    assert safe_cut(messages, 2) == 2
    assert safe_cut(messages, 0) == 0
    assert safe_cut(messages, 99) == 99


@pytest.mark.asyncio
async def test_fork_lineage_messages_and_events(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        source = await harness.source()
        source_messages = await harness.sessions.load_messages(source.id)

        result = await harness.forker.fork(source.id)

        assert result.source_session_id == source.id
        assert result.cut_message_seq == 6
        assert result.checkpoint_id is None
        forked = result.session
        assert forked.id != source.id
        assert forked.parent_session_id == source.id
        assert forked.fork_message_seq == 6
        assert forked.fork_checkpoint_id is None
        assert forked.project_id == source.project_id
        assert forked.cwd == source.cwd
        assert forked.model == source.model
        assert forked.provider == source.provider
        assert forked.title == "source"
        assert forked.status == "active"
        assert forked.last_seq == 2
        assert forked.lease_owner is None

        copied = await harness.sessions.load_messages(forked.id)
        assert _seqs(copied) == [1, 2, 3, 4, 5, 6]
        for original, clone in zip(source_messages, copied, strict=True):
            assert original.model_dump() == clone.model_dump()

        events = await harness.sessions.events_for(forked.id)
        assert [event.type for event in events] == [
            EventType.SESSION_CREATED,
            EventType.SESSION_FORKED,
        ]
        assert [event.seq for event in events] == [1, 2]
        assert events[1].payload["source_session_id"] == source.id
        assert events[1].payload["cut_message_seq"] == 6

        assert any(
            event.type is EventType.SESSION_FORKED and event.session_id == forked.id
            for event in harness.events
        )
        actions = [entry.action for entry in await harness.audit.tail()]
        assert "session.forked" in actions
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_anchors_are_copied_with_new_ids(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        source = await harness.source()
        now = harness.clock.now()
        await harness.anchors.upsert(
            AnchorRecord(
                id="anchor-src",
                session_id=source.id,
                kind="decision",
                text="keep the API stable",
                source_message_seq=3,
                salience=0.9,
                active=True,
                created_at=now,
                updated_at=now,
            )
        )
        await harness.anchors.upsert(
            AnchorRecord(
                id="anchor-inactive",
                session_id=source.id,
                kind="decision",
                text="old",
                source_message_seq=1,
                salience=0.1,
                active=False,
                created_at=now,
                updated_at=now,
            )
        )

        result = await harness.forker.fork(source.id)

        copied = await harness.anchors.list_active(result.session.id)
        assert len(copied) == 1
        anchor = copied[0]
        assert anchor.id != "anchor-src"
        assert anchor.kind == "decision"
        assert anchor.text == "keep the API stable"
        assert anchor.source_message_seq == 3
        assert anchor.salience == pytest.approx(0.9)
        assert await harness.count("context_anchors", source.id) == 2
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_fork_does_not_inherit_anchors_from_after_the_cut(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        source = await harness.source()
        now = harness.clock.now()

        def anchor(anchor_id: str, text: str, seq: int | None) -> AnchorRecord:
            return AnchorRecord(
                id=anchor_id,
                session_id=source.id,
                kind="constraint",
                text=text,
                source_message_seq=seq,
                salience=1.0,
                active=True,
                created_at=now,
                updated_at=now,
            )

        await harness.anchors.upsert(anchor("early", "early constraint", 2))
        await harness.anchors.upsert(anchor("future", "future constraint", 4))
        await harness.anchors.upsert(anchor("global", "manual global fact", None))

        result = await harness.forker.fork(source.id, at_message_seq=2)
        copied = await harness.anchors.list_active(result.session.id)
        texts = {item.text for item in copied}
        assert texts == {"early constraint", "manual global fact"}
        assert "future constraint" not in texts
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_source_is_unchanged_and_execution_state_is_not_copied(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    try:
        source = await harness.source()
        fence_epoch = await harness.sessions.acquire_lease(source.id, "owner-a", 300)
        fence = LeaseFence("owner-a", fence_epoch)
        await harness.sessions.tool_calls.start(
            source.id, "turn-1", "call-src", "bash", {"command": "ls"}, False
        )
        now = datetime.now(UTC)
        await harness.approvals.save(
            ApprovalRecord(
                id="appr-1",
                fingerprint="fp",
                tool_name="bash",
                scope="session",
                decision="allow",
                session_id=source.id,
                project_id=None,
                reason=None,
                created_at=now,
                expires_at=now,
            )
        )
        await harness.db.run(
            lambda conn: conn.execute(
                """
                INSERT INTO file_claims (
                    id, session_id, lease_owner, path, scope, mode, acquired_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "claim-1",
                    source.id,
                    "owner-a",
                    "file.txt",
                    "file",
                    "exclusive",
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        )
        await harness.checkpoints.create(source.id, fence=fence)

        before_row = await harness.session_row(source.id)
        before_messages = await harness.sessions.load_messages(source.id)
        before_events = await harness.sessions.events_for(source.id)

        result = await harness.forker.fork(source.id)

        after_row = await harness.session_row(source.id)
        after_messages = await harness.sessions.load_messages(source.id)
        after_events = await harness.sessions.events_for(source.id)
        assert before_row == after_row
        assert before_messages == after_messages
        assert before_events == after_events
        assert after_row["lease_owner"] == "owner-a"
        assert int(after_row["lease_epoch"]) == fence_epoch

        forked_id = result.session.id
        for table in (
            "tool_calls",
            "model_calls",
            "process_records",
            "file_claims",
            "approvals",
            "checkpoints",
            "compactions",
            "context_state",
        ):
            assert await harness.count(table, forked_id) == 0
        assert await harness.sessions.load_messages(forked_id) != []
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_checkpoint_cut_is_used_and_recorded(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        session = await harness.sessions.create(
            cwd=tmp_path, model="m", provider="p", title="checkpointed"
        )
        await harness.sessions.append_user_message(session.id, "one")
        await harness.sessions.append_assistant_message(session.id, AssistantMessage.of("two"))
        epoch = await harness.sessions.acquire_lease(session.id, "owner-a", 300)
        checkpoint = await harness.checkpoints.create(
            session.id, fence=LeaseFence("owner-a", epoch)
        )
        await harness.sessions.append_user_message(session.id, "after checkpoint")

        result = await harness.forker.fork(
            session.id, checkpoint_id=checkpoint.id, checkpoints=harness.checkpoints
        )

        assert result.cut_message_seq == 2
        assert result.checkpoint_id == checkpoint.id
        assert result.session.fork_checkpoint_id == checkpoint.id
        assert result.session.fork_message_seq == 2
        assert _seqs(await harness.sessions.load_messages(result.session.id)) == [1, 2]
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_fork_at_unbalanced_cut_moves_back(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        source = await harness.source()
        result = await harness.forker.fork(source.id, at_message_seq=4)
        assert result.cut_message_seq == 3
        assert _seqs(await harness.sessions.load_messages(result.session.id)) == [1, 2, 3]
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_cut_zero_creates_empty_fork(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        source = await harness.source()
        result = await harness.forker.fork(source.id, at_message_seq=0)
        assert result.cut_message_seq == 0
        assert await harness.sessions.load_messages(result.session.id) == []
        assert result.session.fork_message_seq == 0
        assert result.session.last_seq == 2
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_invalid_inputs_raise_fork_error(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        source = await harness.source()
        other = await harness.sessions.create(cwd=tmp_path, model="m", provider="p")
        epoch = await harness.sessions.acquire_lease(other.id, "owner-b", 300)
        other_checkpoint = await harness.checkpoints.create(
            other.id, fence=LeaseFence("owner-b", epoch)
        )

        with pytest.raises(ForkError, match="unknown session"):
            await harness.forker.fork("ses_missing")
        with pytest.raises(ForkError, match="not both"):
            await harness.forker.fork(
                source.id, at_message_seq=1, checkpoint_id=other_checkpoint.id
            )
        with pytest.raises(ForkError, match="outside the transcript range"):
            await harness.forker.fork(source.id, at_message_seq=-1)
        with pytest.raises(ForkError, match="outside the transcript range"):
            await harness.forker.fork(source.id, at_message_seq=7)
        with pytest.raises(ForkError, match="unknown checkpoint"):
            await harness.forker.fork(
                source.id, checkpoint_id="ckpt_missing", checkpoints=harness.checkpoints
            )
        with pytest.raises(ForkError, match="belongs to session"):
            await harness.forker.fork(
                source.id,
                checkpoint_id=other_checkpoint.id,
                checkpoints=harness.checkpoints,
            )
        with pytest.raises(ForkError, match="requires a CheckpointService"):
            await harness.forker.fork(source.id, checkpoint_id=other_checkpoint.id)
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_corrupt_checkpoint_cut_is_rejected(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        source = await harness.source()
        epoch = await harness.sessions.acquire_lease(source.id, "owner-a", 300)
        checkpoint = await harness.checkpoints.create(source.id, fence=LeaseFence("owner-a", epoch))
        await harness.db.run(
            lambda conn: conn.execute(
                "UPDATE checkpoints SET state_json = 'broken' WHERE id = ?",
                (checkpoint.id,),
            )
        )
        with pytest.raises(ForkError, match="is invalid"):
            await harness.forker.fork(
                source.id, checkpoint_id=checkpoint.id, checkpoints=harness.checkpoints
            )
    finally:
        harness.close()
