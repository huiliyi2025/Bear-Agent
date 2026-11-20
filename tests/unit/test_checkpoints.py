from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from bear_agent.audit.service import AuditService
from bear_agent.config.models import CheckpointConfig
from bear_agent.core.clock import FakeClock
from bear_agent.core.errors import CheckpointCorrupt, CheckpointIncompatible, SessionLeaseError
from bear_agent.core.events import EventType
from bear_agent.core.fencing import LeaseFence
from bear_agent.core.messages import AssistantMessage
from bear_agent.observability.bus import EventBus
from bear_agent.observability.recorder import EventRecorder
from bear_agent.sessions.checkpoints import CHECKPOINT_SCHEMA_VERSION, CheckpointService
from bear_agent.sessions.service import SessionService
from bear_agent.storage.continuity_repositories import (
    AnchorRepository,
    CheckpointRecord,
    CheckpointRepository,
    CompactionRepository,
    ContextStateRepository,
    compute_checkpoint_checksum,
)
from bear_agent.storage.database import Database


async def _setup(tmp_path: Path, *, keep: int = 20):
    db = Database(tmp_path / "bear.db")
    clock = FakeClock(datetime.now(UTC))
    sessions = SessionService(db, clock)
    recorder = EventRecorder(sessions, EventBus())
    audit = AuditService(db, clock=clock)
    service = CheckpointService(
        sessions=sessions,
        checkpoints=CheckpointRepository(db),
        compactions=CompactionRepository(db),
        anchors=AnchorRepository(db),
        context_state=ContextStateRepository(db),
        recorder=recorder,
        audit=audit,
        config=CheckpointConfig(keep=keep),
        clock=clock,
    )
    session = await sessions.create(cwd=tmp_path, model="m", provider="p")
    epoch = await sessions.acquire_lease(session.id, "owner-a", 300)
    return db, sessions, service, audit, session, LeaseFence("owner-a", epoch)


async def _checkpoint_count(db: Database, session_id: str) -> int:
    def _count(conn):
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM checkpoints WHERE session_id = ?", (session_id,)
        ).fetchone()
        return int(row["n"])

    return await db.run(_count)


@pytest.mark.asyncio
async def test_create_and_load_roundtrip(tmp_path: Path) -> None:
    db, sessions, service, audit, session, fence = await _setup(tmp_path)
    try:
        await sessions.append_user_message(session.id, "hello")
        await sessions.tool_calls.start(
            session.id, "turn-1", "call-1", "read_file", {"path": "x.txt"}, True
        )
        before = await sessions.get(session.id)
        assert before is not None
        record = await service.create(
            session.id, fence=fence, kind="turn", run_id="run-1", turn_index=2
        )

        loaded = await service.get_raw(record.id)
        assert loaded == record
        current = await sessions.get(session.id)
        assert current is not None
        # The position is captured before the checkpoint's own event.
        assert record.event_seq == before.last_seq
        assert current.last_seq == before.last_seq + 1
        assert record.message_seq == 1
        assert record.schema_version == CHECKPOINT_SCHEMA_VERSION

        state = record.state
        assert state["format"] == "bear-checkpoint"
        assert state["session"]["id"] == session.id
        assert state["session"]["model"] == "m"
        assert state["position"] == {"event_seq": record.event_seq, "message_seq": 1}
        assert state["run"] == {"run_id": "run-1", "last_turn": 2}
        assert state["tools"]["running"] == [
            {"id": "call-1", "name": "read_file", "turn_id": "turn-1", "idempotent": True}
        ]
        assert state["usage"]["calls"] == 0

        expected = compute_checkpoint_checksum(
            session_id=record.session_id,
            kind=record.kind,
            event_seq=record.event_seq,
            message_seq=record.message_seq,
            state=record.state,
            schema_version=record.schema_version,
        )
        assert record.checksum == expected

        await service.validate(record)
        assert await service.get_valid(record.id) is not None
        assert await service.get_valid("ckpt_missing") is None
        assert await service.latest_valid(session.id) == record

        events = await sessions.events_for(session.id)
        assert EventType.CHECKPOINT_CREATED in [event.type for event in events]
        actions = [entry.action for entry in await audit.tail()]
        assert "checkpoint.created" in actions
    finally:
        db.close()


@pytest.mark.asyncio
async def test_corrupted_state_or_checksum_is_rejected(tmp_path: Path) -> None:
    db, sessions, service, _audit, session, fence = await _setup(tmp_path)
    try:
        record = await service.create(session.id, fence=fence)

        tampered_state = dict(record.state)
        tampered_state["usage"] = {**tampered_state["usage"], "calls": 99}
        await db.run(
            lambda conn: conn.execute(
                "UPDATE checkpoints SET state_json = ?, checksum = ? WHERE id = ?",
                (
                    json.dumps(tampered_state),
                    record.checksum,
                    record.id,
                ),
            )
        )
        loaded = await service.get_raw(record.id)
        assert loaded is not None
        with pytest.raises(CheckpointCorrupt):
            await service.validate(loaded)

        await db.run(
            lambda conn: conn.execute(
                "UPDATE checkpoints SET checksum = ? WHERE id = ?",
                ("0" * 64, record.id),
            )
        )
        loaded = await service.get_raw(record.id)
        assert loaded is not None
        with pytest.raises(CheckpointCorrupt):
            await service.validate(loaded)

        # A row whose state JSON cannot be parsed is reported and ignored, not
        # trusted and not deleted.
        await db.run(
            lambda conn: conn.execute(
                "UPDATE checkpoints SET state_json = ? WHERE id = ?",
                ("not-json", record.id),
            )
        )
        assert await service.latest_valid(session.id) is None
        events = await sessions.events_for(session.id)
        assert EventType.CHECKPOINT_INVALID in [event.type for event in events]
        assert await _checkpoint_count(db, session.id) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_incompatible_schema_version_is_ignored(tmp_path: Path) -> None:
    db, sessions, service, _audit, session, fence = await _setup(tmp_path)
    try:
        record = await service.create(session.id, fence=fence)
        await db.run(
            lambda conn: conn.execute(
                "UPDATE checkpoints SET schema_version = 99 WHERE id = ?", (record.id,)
            )
        )
        loaded = await service.get_raw(record.id)
        assert loaded is not None
        with pytest.raises(CheckpointIncompatible):
            await service.validate(loaded)

        assert await service.latest_valid(session.id) is None
        events = await sessions.events_for(session.id)
        invalid = [event for event in events if event.type is EventType.CHECKPOINT_INVALID]
        assert invalid
        assert invalid[-1].payload["checkpoint_id"] == record.id
        assert await _checkpoint_count(db, session.id) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_retention_prunes_and_emits_event(tmp_path: Path) -> None:
    db, sessions, service, _audit, session, fence = await _setup(tmp_path, keep=2)
    try:
        for index in range(4):
            await sessions.append_user_message(session.id, f"message {index}")
            await service.create(session.id, fence=fence)

        assert await _checkpoint_count(db, session.id) == 2
        events = await sessions.events_for(session.id)
        pruned = [event for event in events if event.type is EventType.CHECKPOINT_PRUNED]
        assert pruned
        assert pruned[0].payload["keep"] == 2
        assert len(pruned[0].payload["ids"]) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_stale_fence_is_rejected_without_writing(tmp_path: Path) -> None:
    db, sessions, service, _audit, session, fence = await _setup(tmp_path)
    try:
        with pytest.raises(SessionLeaseError):
            await service.create(session.id, fence=LeaseFence("owner-a", fence.epoch + 1))
        with pytest.raises(SessionLeaseError):
            await service.create(session.id, fence=LeaseFence("owner-b", fence.epoch))
        assert await _checkpoint_count(db, session.id) == 0
        events = await sessions.events_for(session.id)
        assert EventType.CHECKPOINT_CREATED not in [event.type for event in events]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_checkpoint_does_not_copy_transcript(tmp_path: Path) -> None:
    db, sessions, service, _audit, session, fence = await _setup(tmp_path)
    try:
        await sessions.append_user_message(session.id, "secret transcript text")
        await sessions.append_assistant_message(session.id, AssistantMessage.of("answer"))
        before = await sessions.load_messages(session.id)

        record = await service.create(session.id, fence=fence)

        after = await sessions.load_messages(session.id)
        assert [message for message in after] == [message for message in before]
        assert "secret transcript text" not in json.dumps(record.state)
        assert record.state["position"]["message_seq"] == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unknown_kind_is_recorded_as_given(tmp_path: Path) -> None:
    db, _sessions, service, _audit, session, fence = await _setup(tmp_path)
    try:
        record = await service.create(session.id, fence=fence, kind="custom_kind")
        assert record.kind == "custom_kind"
        await service.validate(record)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_list_returns_rows_and_empty_kind_is_rejected(tmp_path: Path) -> None:
    db, _sessions, service, _audit, session, fence = await _setup(tmp_path)
    try:
        record = await service.create(session.id, fence=fence)
        rows = await service.list(session.id, limit=10)
        assert [row.id for row in rows] == [record.id]
        with pytest.raises(ValueError, match="kind"):
            await service.create(session.id, fence=fence, kind="")
    finally:
        db.close()


def _rechecksummed(
    record: CheckpointRecord,
    mutate: Callable[[dict[str, Any]], None],
    *,
    event_seq: int | None = None,
    message_seq: int | None = None,
) -> CheckpointRecord:
    """Tamper with a valid state and re-sign it so shape checks are reached."""
    state = copy.deepcopy(record.state)
    mutate(state)
    resolved_event_seq = record.event_seq if event_seq is None else event_seq
    resolved_message_seq = record.message_seq if message_seq is None else message_seq
    checksum = compute_checkpoint_checksum(
        session_id=record.session_id,
        kind=record.kind,
        event_seq=resolved_event_seq,
        message_seq=resolved_message_seq,
        state=state,
        schema_version=record.schema_version,
    )
    return replace(
        record,
        event_seq=resolved_event_seq,
        message_seq=resolved_message_seq,
        state=state,
        checksum=checksum,
    )


@pytest.mark.asyncio
async def test_validate_rejects_malformed_state_shapes(tmp_path: Path) -> None:
    db, _sessions, service, _audit, session, fence = await _setup(tmp_path)
    try:
        record = await service.create(session.id, fence=fence)

        mutators: dict[str, Callable[[dict[str, Any]], None]] = {
            "missing format": lambda state: state.pop("format"),
            "missing session": lambda state: state.__setitem__("session", []),
            "session id mismatch": lambda state: state["session"].__setitem__("id", "ses_other"),
            "missing position": lambda state: state.pop("position"),
            "position mismatch": lambda state: state["position"].__setitem__(
                "event_seq", state["position"]["event_seq"] + 1
            ),
            "non-integer position": lambda state: state["position"].__setitem__("message_seq", "1"),
            "missing run": lambda state: state.pop("run"),
            "invalid run_id": lambda state: state["run"].__setitem__("run_id", 7),
            "invalid last_turn": lambda state: state["run"].__setitem__("last_turn", "x"),
            "missing context": lambda state: state.__setitem__("context", []),
            "invalid generation": lambda state: state["context"].__setitem__("generation", "x"),
            "invalid last_compaction_id": lambda state: state["context"].__setitem__(
                "last_compaction_id", 7
            ),
            "invalid anchors": lambda state: state["context"].__setitem__("anchors", {}),
            "invalid compactions": lambda state: state["context"].__setitem__("compactions", {}),
            "missing tools": lambda state: state.__setitem__("tools", []),
            "invalid running tools": lambda state: state["tools"].__setitem__("running", {}),
            "invalid recent tools": lambda state: state["tools"].__setitem__("recent", {}),
            "missing usage": lambda state: state.__setitem__("usage", []),
            "invalid usage": lambda state: state["usage"].__setitem__("calls", "x"),
        }
        for mutate in mutators.values():
            tampered = _rechecksummed(record, mutate)
            with pytest.raises(CheckpointCorrupt):
                await service.validate(tampered)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_validate_rejects_positions_beyond_transcript(tmp_path: Path) -> None:
    db, _sessions, service, _audit, session, fence = await _setup(tmp_path)
    try:
        record = await service.create(session.id, fence=fence)

        def push_event(state: dict[str, Any]) -> None:
            state["position"]["event_seq"] = record.event_seq + 50

        future_event = _rechecksummed(record, push_event, event_seq=record.event_seq + 50)
        with pytest.raises(CheckpointCorrupt, match="beyond the session position"):
            await service.validate(future_event)

        def push_message(state: dict[str, Any]) -> None:
            state["position"]["message_seq"] = record.message_seq + 50

        future_message = _rechecksummed(record, push_message, message_seq=record.message_seq + 50)
        with pytest.raises(CheckpointCorrupt, match="beyond the transcript position"):
            await service.validate(future_message)
    finally:
        db.close()
