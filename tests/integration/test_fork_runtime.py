"""Fork isolation through the real runtime."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support import make_container, new_session, text_script

from bear_agent.runtime.stop_policy import RunStatus


@pytest.mark.asyncio
async def test_fork_runs_independently_and_source_is_unchanged(workspace: Path, settings) -> None:
    container, _provider = make_container(
        settings, workspace, [text_script("source answer"), text_script("fork answer")]
    )
    try:
        source = await new_session(container, workspace)
        first = await container.runtime.run(source.id, "source question")
        assert first.status == RunStatus.COMPLETED

        source_before = await container.sessions.get(source.id)
        source_messages_before = await container.sessions.load_messages(source.id)
        source_events_before = await container.sessions.events_for(source.id)

        forked = await container.fork.fork(source.id, checkpoints=container.checkpoints)
        assert forked.session.parent_session_id == source.id
        assert forked.cut_message_seq == len(source_messages_before)

        second = await container.runtime.run(forked.session.id, "fork question")
        assert second.status == RunStatus.COMPLETED
        assert second.final_text == "fork answer"

        source_after = await container.sessions.get(source.id)
        assert source_after is not None
        assert source_before is not None
        assert source_after.last_seq == source_before.last_seq
        assert source_after.lease_owner is None
        assert await container.sessions.load_messages(source.id) == source_messages_before
        assert await container.sessions.events_for(source.id) == source_events_before

        forked_messages = await container.sessions.load_messages(forked.session.id)
        assert [message.text_content() for message in forked_messages] == [
            "source question",
            "source answer",
            "fork question",
            "fork answer",
        ]
        assert (await container.audit.verify()).ok
    finally:
        await container.close()
