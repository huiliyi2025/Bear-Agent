"""Compaction history loads the most recent rows, not the oldest."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bear_agent.sessions.service import SessionService
from bear_agent.storage.continuity_repositories import CompactionRecord, CompactionRepository
from bear_agent.storage.database import Database


@pytest.mark.asyncio
async def test_compaction_list_keeps_newest_rows_in_chronological_order(tmp_path: Path) -> None:
    db = Database(tmp_path / "compactions.db")
    try:
        sessions = SessionService(db)
        session = await sessions.create(cwd=tmp_path, model="m", provider="p")
        repository = CompactionRepository(db)
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for index in range(5):
            await repository.create(
                CompactionRecord(
                    id=f"cmp-{index}",
                    session_id=session.id,
                    level=2,
                    covered_from_message_seq=1,
                    covered_to_message_seq=index + 1,
                    summary={"text": f"summary {index}"},
                    artifact_id=None,
                    before_tokens=100 - index,
                    after_tokens=10,
                    created_at=base + timedelta(seconds=index),
                )
            )
        rows = await repository.list_for_session(session.id, limit=3)
        assert [row.id for row in rows] == ["cmp-2", "cmp-3", "cmp-4"]
        assert [row.summary["text"] for row in rows] == [
            "summary 2",
            "summary 3",
            "summary 4",
        ]
    finally:
        db.close()
