"""Continuity writes are fenced by the current session lease."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from bear_agent.audit.service import AuditService
from bear_agent.config.models import CheckpointConfig
from bear_agent.core.clock import SystemClock
from bear_agent.core.errors import SessionLeaseError
from bear_agent.core.fencing import LeaseFence
from bear_agent.observability.bus import EventBus
from bear_agent.observability.recorder import EventRecorder
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.sessions.checkpoints import CheckpointService
from bear_agent.sessions.service import SessionService
from bear_agent.storage.continuity_repositories import (
    AnchorRecord,
    AnchorRepository,
    CheckpointRepository,
    CompactionRecord,
    CompactionRepository,
    ContextStateRecord,
    ContextStateRepository,
    ProcessRecord,
    ProcessRepository,
)
from bear_agent.storage.database import Database
from bear_agent.storage.security_repositories import ClaimRecord, ClaimRepository


@pytest.mark.asyncio
async def test_stale_owner_cannot_write_continuity_state(tmp_path: Path) -> None:
    db = Database(tmp_path / "continuity-fence.db")
    try:
        clock = SystemClock()
        sessions = SessionService(db, clock)
        session = await sessions.create(cwd=tmp_path, model="m", provider="p")
        epoch1 = await sessions.acquire_lease(session.id, "owner-1", 60)
        stale = LeaseFence("owner-1", epoch1)
        await sessions.release_lease(session.id, "owner-1", epoch1)
        epoch2 = await sessions.acquire_lease(session.id, "owner-2", 60)
        current = LeaseFence("owner-2", epoch2)

        redactor = SecretRedactor()
        recorder = EventRecorder(sessions, EventBus(), redactor)
        checkpoints = CheckpointService(
            sessions=sessions,
            checkpoints=CheckpointRepository(db),
            compactions=CompactionRepository(db),
            anchors=AnchorRepository(db),
            context_state=ContextStateRepository(db),
            recorder=recorder,
            audit=AuditService(db, redactor=redactor),
            config=CheckpointConfig(),
            redactor=redactor,
            clock=clock,
        )
        with pytest.raises(SessionLeaseError):
            await checkpoints.create(session.id, fence=stale, kind="manual")
        kept = await checkpoints.create(session.id, fence=current, kind="manual")
        with pytest.raises(SessionLeaseError):
            await CheckpointRepository(db).prune(session.id, 1, fence=stale)

        compactions = CompactionRepository(db)
        with pytest.raises(SessionLeaseError):
            await compactions.create(
                CompactionRecord(
                    id="cmp-stale",
                    session_id=session.id,
                    level=2,
                    covered_from_message_seq=1,
                    covered_to_message_seq=2,
                    summary={"text": "stale"},
                    artifact_id=None,
                    before_tokens=10,
                    after_tokens=5,
                    created_at=clock.now(),
                ),
                fence=stale,
            )

        anchors = AnchorRepository(db)
        with pytest.raises(SessionLeaseError):
            await anchors.upsert(
                AnchorRecord(
                    id="anc-stale",
                    session_id=session.id,
                    kind="note",
                    text="stale",
                    source_message_seq=None,
                    salience=1.0,
                    active=True,
                    created_at=clock.now(),
                    updated_at=clock.now(),
                ),
                fence=stale,
            )

        state = ContextStateRepository(db)
        with pytest.raises(SessionLeaseError):
            await state.save(
                ContextStateRecord(
                    session_id=session.id,
                    generation=2,
                    last_compaction_id=None,
                    consecutive_failures=0,
                    disabled_until_turn=None,
                    updated_at=clock.now(),
                ),
                fence=stale,
            )

        processes = ProcessRepository(db)
        with pytest.raises(SessionLeaseError):
            await processes.create(
                ProcessRecord(
                    id="proc-stale",
                    session_id=session.id,
                    tool_call_id=None,
                    lease_owner="owner-1",
                    pid=1234,
                    pgid=None,
                    command_preview="stale",
                    created_at=clock.now(),
                    finished_at=None,
                    status="running",
                ),
                fence=stale,
            )

        assert [
            item.id for item in await CheckpointRepository(db).list_for_session(session.id)
        ] == [kept.id]
        assert await compactions.list_for_session(session.id) == []
        assert await anchors.list_active(session.id) == []
        assert await state.get(session.id) is None
        assert await processes.list_running(session.id) == []

        # A stale recovery must not delete the current owner's live claim.
        claims = ClaimRepository(db)
        now = clock.now()
        await claims.acquire(
            ClaimRecord(
                id="claim-current",
                session_id=session.id,
                lease_owner="owner-2",
                path="src/app.py",
                scope="file",
                mode="exclusive",
                acquired_at=now,
                expires_at=now + timedelta(days=365),
            ),
            now,
        )
        with pytest.raises(SessionLeaseError):
            await claims.release_session_except_owner(session.id, "owner-1", fence=stale)
        remaining = await claims.list_all()
        assert [claim.lease_owner for claim in remaining] == ["owner-2"]
    finally:
        db.close()
