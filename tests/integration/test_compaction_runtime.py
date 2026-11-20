"""Runtime integration: compaction changes the request, not the transcript."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support import make_container, new_session, text_script, tool_script

from bear_agent.core.events import EventType
from bear_agent.runtime.stop_policy import RunStatus
from bear_agent.storage.continuity_repositories import (
    AnchorRepository,
    ArtifactRepository,
    CompactionRepository,
)


@pytest.mark.asyncio
async def test_l1_compaction_archives_output_and_preserves_transcript(
    workspace: Path, settings
) -> None:
    big = "A" * 12_000
    (workspace / "big.txt").write_text(big, encoding="utf-8")
    settings.agent.context_window = 8_000
    settings.agent.max_output_tokens = 1_000
    settings.context.trigger_ratio = 0.5
    settings.context.keep_recent_turns = 1
    settings.context.l1_min_tool_chars = 1_000
    settings.context.l1_keep_chars = 200
    settings.context.l2_enabled = True
    settings.context.l3_enabled = False
    settings.context.l4_enabled = False

    container, provider = make_container(
        settings,
        workspace,
        [
            tool_script("call-read", "read_file", {"path": "big.txt"}),
            text_script("first done"),
            text_script("second done"),
        ],
    )
    try:
        session = await new_session(container, workspace)
        first = await container.runtime.run(
            session.id, "Read big.txt. MUST preserve this constraint."
        )
        assert first.status == RunStatus.COMPLETED

        second = await container.runtime.run(session.id, "What did you learn?")
        assert second.status == RunStatus.COMPLETED

        # The transcript still contains the complete tool output.
        messages = await container.sessions.load_messages(session.id)
        tool_messages = [message for message in messages if message.role == "tool"]
        assert tool_messages
        result = tool_messages[0].tool_result()
        assert result is not None
        assert len(result.content) >= 12_000

        # A compaction and an artifact exist, and the artifact is readable.
        compactions = CompactionRepository(container.db)
        records = await compactions.list_for_session(session.id)
        assert records
        assert records[0].level == 1
        artifact_id = records[0].artifact_id
        assert artifact_id is not None
        artifacts = ArtifactRepository(container.db)
        artifact = await artifacts.get(artifact_id)
        assert artifact is not None
        archived = await container.artifacts.get_text(artifact_id)
        assert big in archived
        assert "full output archived" not in archived

        # The second model request uses the compacted view, not the full output.
        request = provider.requests[-1]
        rendered = "\n".join(
            part.content
            for message in request.messages
            for part in message.parts
            if part.type == "tool_result"
        )
        assert "full output archived" in rendered
        assert big not in rendered

        # Constraints survive as anchors even though the message is old.
        anchors = AnchorRepository(container.db)
        active = await anchors.list_active(session.id)
        assert any("MUST preserve" in anchor.text for anchor in active)

        events = await container.sessions.events_for(session.id)
        assert EventType.CONTEXT_COMPACTED in [event.type for event in events]
        assert EventType.CHECKPOINT_CREATED in [event.type for event in events]
    finally:
        await container.close()
