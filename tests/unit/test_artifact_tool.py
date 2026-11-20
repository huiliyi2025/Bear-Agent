"""read_artifact is session-scoped and verifies content integrity."""

from __future__ import annotations

from pathlib import Path

import pytest

from bear_agent.artifacts.store import ArtifactStore
from bear_agent.core.cancel import CancelToken
from bear_agent.policy.path_guard import PathGuard
from bear_agent.sessions.service import SessionService
from bear_agent.storage.continuity_repositories import ArtifactRepository
from bear_agent.storage.database import Database
from bear_agent.tools.base import ToolContext
from bear_agent.tools.builtin.artifacts import ReadArtifactArgs, ReadArtifactTool


async def _ctx(tmp_path: Path, session_id: str) -> ToolContext:
    return ToolContext(
        cwd=tmp_path,
        session_id=session_id,
        cancel=CancelToken(),
        path_guard=PathGuard(tmp_path),
    )


@pytest.mark.asyncio
async def test_read_artifact_is_session_scoped_and_supports_offsets(tmp_path: Path) -> None:
    db = Database(tmp_path / "artifacts.db")
    try:
        sessions = SessionService(db)
        owner = await sessions.create(cwd=tmp_path, model="m", provider="p")
        other = await sessions.create(cwd=tmp_path, model="m", provider="p")
        store = ArtifactStore(tmp_path / "artifacts", ArtifactRepository(db))
        record = await store.put_text(owner.id, "0123456789" * 100, kind="tool_output")
        tool = ReadArtifactTool(store)

        full = await tool.run(
            ReadArtifactArgs(artifact_id=record.id), await _ctx(tmp_path, owner.id)
        )
        assert not full.is_error
        assert full.content.startswith("0123456789")
        assert len(full.content) == 1000
        assert full.truncated is False

        part = await tool.run(
            ReadArtifactArgs(artifact_id=record.id, offset=990, limit=5),
            await _ctx(tmp_path, owner.id),
        )
        assert part.content.startswith("01234")
        assert part.truncated is True
        assert "artifact truncated" in part.content

        denied = await tool.run(
            ReadArtifactArgs(artifact_id=record.id), await _ctx(tmp_path, other.id)
        )
        assert denied.is_error
        assert denied.error_kind == "unknown_artifact"
        assert "012345" not in denied.content

        missing = await tool.run(
            ReadArtifactArgs(artifact_id="art_missing"), await _ctx(tmp_path, owner.id)
        )
        assert missing.is_error
        assert missing.error_kind == "unknown_artifact"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_read_artifact_detects_corrupted_file(tmp_path: Path) -> None:
    db = Database(tmp_path / "artifacts-corrupt.db")
    try:
        sessions = SessionService(db)
        session = await sessions.create(cwd=tmp_path, model="m", provider="p")
        store = ArtifactStore(tmp_path / "artifacts", ArtifactRepository(db))
        record = await store.put_text(session.id, "trusted content", kind="tool_output")
        target = store.resolve(record)
        target.write_text("tampered content", encoding="utf-8")

        tool = ReadArtifactTool(store)
        result = await tool.run(
            ReadArtifactArgs(artifact_id=record.id), await _ctx(tmp_path, session.id)
        )
        assert result.is_error
        assert result.error_kind == "artifact_unavailable"
        assert "tampered" not in result.content
    finally:
        db.close()
