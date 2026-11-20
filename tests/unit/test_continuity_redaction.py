"""Checkpoint and anchor state must not persist registered secrets."""

from __future__ import annotations

from pathlib import Path

import pytest

from bear_agent.audit.service import AuditService
from bear_agent.config.models import CheckpointConfig
from bear_agent.context.anchors import AnchorLedger
from bear_agent.core.clock import SystemClock
from bear_agent.core.fencing import LeaseFence
from bear_agent.observability.bus import EventBus
from bear_agent.observability.recorder import EventRecorder
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.sessions.checkpoints import CheckpointService
from bear_agent.sessions.service import SessionService
from bear_agent.storage.continuity_repositories import (
    AnchorRepository,
    CheckpointRepository,
    CompactionRepository,
    ContextStateRepository,
    canonical_json,
)
from bear_agent.storage.database import Database

SECRET = "sk-live-supersecret-123456"


@pytest.mark.asyncio
async def test_checkpoint_and_anchor_redact_registered_secret(tmp_path: Path) -> None:
    db = Database(tmp_path / "redact.db")
    try:
        clock = SystemClock()
        sessions = SessionService(db, clock)
        session = await sessions.create(
            cwd=tmp_path,
            model="m",
            provider="p",
            title=f"token {SECRET}",
        )
        epoch = await sessions.acquire_lease(session.id, "owner", 60)
        fence = LeaseFence("owner", epoch)
        redactor = SecretRedactor()
        redactor.register("test-secret", SECRET)
        recorder = EventRecorder(sessions, EventBus(), redactor)

        anchors_repo = AnchorRepository(db)
        anchors = AnchorLedger(anchors_repo, clock=clock, redactor=redactor)
        anchor = await anchors.add(
            session.id,
            f"never print {SECRET}",
            kind="constraint",
            fence=fence,
        )
        assert SECRET not in anchor.text

        checkpoints = CheckpointService(
            sessions=sessions,
            checkpoints=CheckpointRepository(db),
            compactions=CompactionRepository(db),
            anchors=anchors_repo,
            context_state=ContextStateRepository(db),
            recorder=recorder,
            audit=AuditService(db, redactor=redactor),
            config=CheckpointConfig(),
            redactor=redactor,
            clock=clock,
        )
        record = await checkpoints.create(session.id, fence=fence, kind="manual")
        raw = await checkpoints.get_raw(record.id)
        assert raw is not None
        rendered = canonical_json(raw.state)
        assert SECRET not in rendered
        assert "REDACTED" in rendered
        await checkpoints.validate(raw)

        active = await anchors_repo.list_active(session.id)
        assert active
        assert all(SECRET not in item.text for item in active)
    finally:
        db.close()
