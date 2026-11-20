from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from bear_agent.artifacts.store import ArtifactStore
from bear_agent.audit.service import AuditService
from bear_agent.config.models import ContextConfig
from bear_agent.context.anchors import AnchorLedger
from bear_agent.context.builder import ContextBuilder
from bear_agent.context.compaction import ContextCompactor
from bear_agent.core.clock import FakeClock
from bear_agent.core.events import Event, EventType
from bear_agent.core.fencing import LeaseFence
from bear_agent.core.messages import (
    AssistantMessage,
    ToolCall,
    ToolMessage,
)
from bear_agent.observability.bus import EventBus
from bear_agent.observability.recorder import EventRecorder
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.sessions.models import SessionRecord
from bear_agent.sessions.service import SessionService
from bear_agent.storage.continuity_repositories import (
    AnchorRepository,
    ArtifactRepository,
    CompactionRepository,
    ContextStateRepository,
)
from bear_agent.storage.database import Database


@dataclass
class Env:
    db: Database
    sessions: SessionService
    session: SessionRecord
    fence: LeaseFence
    builder: ContextBuilder
    config: ContextConfig
    compactor: ContextCompactor
    anchors: AnchorLedger
    artifacts: ArtifactStore
    compactions: CompactionRepository
    context_state: ContextStateRepository
    events: list[Event]
    clock: FakeClock


class FakeSummarizer:
    def __init__(
        self,
        text: str = "MODEL SUMMARY: goal and constraints preserved.",
        *,
        error: Exception | None = None,
    ) -> None:
        self.text = text
        self.error = error
        self.calls = 0
        self.prompts: list[str] = []

    async def __call__(self, *, session_id: str, prompt: str) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return self.text


async def _setup(
    tmp_path: Path,
    *,
    context_window: int = 4_000,
    max_output_tokens: int = 500,
    summarizer: FakeSummarizer | None = None,
    checkpoint_callback: Any = None,
    audit: bool = False,
    **context_overrides: Any,
) -> Env:
    db = Database(tmp_path / "bear.db")
    clock = FakeClock()
    sessions = SessionService(db, clock)
    session = await sessions.create(cwd=tmp_path, model="m", provider="p")
    epoch = await sessions.acquire_lease(session.id, "owner", 300)
    fence = LeaseFence(owner="owner", epoch=epoch)

    events: list[Event] = []

    async def collect(event: Event) -> None:
        events.append(event)

    bus = EventBus()
    bus.subscribe(collect)
    redactor = SecretRedactor()
    recorder = EventRecorder(sessions, bus, redactor)
    artifacts = ArtifactStore(tmp_path / "artifacts", ArtifactRepository(db), redactor=redactor)
    anchors = AnchorLedger(AnchorRepository(db), clock=clock, recorder=recorder)
    builder = ContextBuilder(
        cwd=tmp_path,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        project_instructions="",
    )
    config = ContextConfig(**context_overrides)
    compactions = CompactionRepository(db)
    context_state = ContextStateRepository(db)
    compactor = ContextCompactor(
        sessions=sessions,
        builder=builder,
        config=config,
        compactions=compactions,
        context_state=context_state,
        anchors=anchors,
        artifacts=artifacts,
        recorder=recorder,
        audit=AuditService(db) if audit else None,
        summarizer=summarizer,
        checkpoint_callback=checkpoint_callback,
        redactor=redactor,
        clock=clock,
    )
    return Env(
        db=db,
        sessions=sessions,
        session=session,
        fence=fence,
        builder=builder,
        config=config,
        compactor=compactor,
        anchors=anchors,
        artifacts=artifacts,
        compactions=compactions,
        context_state=context_state,
        events=events,
        clock=clock,
    )


async def _append_turn(
    env: Env,
    user_text: str,
    *,
    assistant_text: str = "",
    tool_call_id: str | None = None,
    tool_output: str | None = None,
    is_error: bool = False,
) -> None:
    await env.sessions.append_user_message(env.session.id, user_text)
    if tool_call_id is not None:
        call = ToolCall(id=tool_call_id, name="shell", arguments={"cmd": "run"})
        await env.sessions.append_assistant_message(
            env.session.id,
            AssistantMessage.with_tool_calls(assistant_text or None, [call]),
        )
        if tool_output is not None:
            await env.sessions.append_tool_message(
                env.session.id,
                ToolMessage.of(tool_call_id, tool_output, is_error=is_error),
            )
    else:
        await env.sessions.append_assistant_message(
            env.session.id, AssistantMessage.of(assistant_text)
        )


async def _seed(env: Env, *, big_chars: int = 12_000) -> list[Any]:
    await _append_turn(
        env,
        "Build the export CLI",
        assistant_text="plan: use argparse",
        tool_call_id="c1",
        tool_output="x" * big_chars,
    )
    await _append_turn(
        env,
        "You must never delete user data",
        assistant_text="constraint noted",
    )
    await _append_turn(
        env,
        "Now fix the header bug",
        assistant_text="fixed the header bug",
    )
    await _append_turn(env, "Final question", assistant_text="final answer")
    return await env.sessions.load_messages(env.session.id)


async def _seed_small(env: Env) -> list[Any]:
    await _append_turn(env, "Build the export CLI", assistant_text="plan: use argparse")
    await _append_turn(env, "You must never delete user data", assistant_text="noted")
    await _append_turn(env, "Now fix the header bug", assistant_text="fixed")
    await _append_turn(env, "Final question", assistant_text="final")
    return await env.sessions.load_messages(env.session.id)


async def _prepare(env: Env, messages: list[Any], *, turn_index: int = 1) -> Any:
    return await env.compactor.prepare(
        env.session,
        messages,
        [],
        turn_index=turn_index,
        run_id="run-1",
        turn_id="turn-1",
        fence=env.fence,
    )


@pytest.mark.asyncio
async def test_no_trigger_below_ratio_does_not_compact(tmp_path: Path) -> None:
    env = await _setup(tmp_path)
    try:
        await env.sessions.append_user_message(env.session.id, "hello")
        await env.sessions.append_assistant_message(env.session.id, AssistantMessage.of("hi"))
        messages = await env.sessions.load_messages(env.session.id)
        prepared = await _prepare(env, messages)

        assert prepared.compacted is False
        assert prepared.levels == []
        assert await env.compactions.list_for_session(env.session.id) == []
        persisted = await env.sessions.load_messages(env.session.id)
        assert [message.text_content() for message in persisted] == ["hello", "hi"]
        non_system = [
            message.text_content() for message in prepared.messages if message.role != "system"
        ]
        assert non_system == ["hello", "hi"]
    finally:
        env.db.close()


@pytest.mark.asyncio
async def test_l1_archives_large_tool_output(tmp_path: Path) -> None:
    env = await _setup(tmp_path, l2_enabled=False, l3_enabled=False, l4_enabled=False)
    try:
        messages = await _seed(env)
        prepared = await _prepare(env, messages)

        assert prepared.compacted is True
        assert prepared.levels == [1]
        assert prepared.estimated_tokens < prepared.before_tokens

        rows = await env.compactions.list_for_session(env.session.id)
        assert [row.level for row in rows] == [1]
        replacements = rows[0].summary["replacements"]
        assert len(replacements) == 1
        replacement = replacements[0]
        artifact = await env.artifacts.repository.get(replacement["artifact_id"])
        assert artifact is not None
        assert artifact.kind == "tool_output"
        assert artifact.metadata["message_seq"] == replacement["message_seq"]
        assert await env.artifacts.get_text(artifact.id) == "x" * 12_000

        # The transcript keeps the original, full output.
        persisted = await env.sessions.load_messages(env.session.id)
        tool_contents = [
            message.tool_result().content
            for message in persisted
            if message.tool_result() is not None
        ]
        assert tool_contents == ["x" * 12_000]

        # The built view references the archived artifact instead.
        view_tool_contents = [
            message.tool_result().content
            for message in prepared.messages
            if message.tool_result() is not None
        ]
        assert len(view_tool_contents) == 1
        assert replacement["artifact_id"] in view_tool_contents[0]
        assert "[full output archived" in view_tool_contents[0]

        event_types = [event.type for event in await env.sessions.events_for(env.session.id)]
        assert EventType.CONTEXT_COMPACTED in event_types
    finally:
        env.db.close()


@pytest.mark.asyncio
async def test_l2_deterministic_summary_preserves_facts_and_transcript(
    tmp_path: Path,
) -> None:
    env = await _setup(tmp_path, l1_enabled=False, l3_enabled=False, l4_enabled=False)
    try:
        await _append_turn(
            env,
            "Build the export CLI",
            assistant_text="DECISION: use argparse " + "d" * 8_000,
        )
        await _append_turn(
            env,
            "You must never delete user data",
            assistant_text="constraint noted",
        )
        await _append_turn(
            env,
            "Run the tests",
            assistant_text="",
            tool_call_id="c1",
            tool_output="Error: boom " + "e" * 8_000,
            is_error=True,
        )
        await _append_turn(env, "Now fix the header bug", assistant_text="fixed")
        await _append_turn(env, "Final question", assistant_text="final")
        messages = await env.sessions.load_messages(env.session.id)
        prepared = await _prepare(env, messages)

        rows = await env.compactions.list_for_session(env.session.id)
        assert [row.level for row in rows] == [2]
        summary = rows[0].summary
        assert summary["source"] == "deterministic"
        text = summary["text"]
        assert "Build the export CLI" in text
        assert "must never delete user data" in text
        assert "boom" in text
        assert summary["supersedes"] == []

        assert rows[0].artifact_id is not None
        assert await env.artifacts.get_text(rows[0].artifact_id) == text

        # Covered turns are gone from the working view but the summary is pinned.
        non_system = [
            message.text_content() for message in prepared.messages if message.role != "system"
        ]
        assert "Build the export CLI" not in non_system
        summary_systems = [
            message.text_content()
            for message in prepared.messages
            if message.role == "system" and "[compacted history]" in message.text_content()
        ]
        assert summary_systems
        assert "Build the export CLI" in summary_systems[0]

        # The transcript itself is immutable.
        persisted = await env.sessions.load_messages(env.session.id)
        assert "Build the export CLI" in [message.text_content() for message in persisted]
    finally:
        env.db.close()


@pytest.mark.asyncio
async def test_l3_success_supersedes_l2_and_uses_model_summary(tmp_path: Path) -> None:
    summarizer = FakeSummarizer("MODEL SUMMARY: goal, constraints and failures kept.")
    env = await _setup(
        tmp_path,
        l1_enabled=False,
        l4_enabled=False,
        summarizer=summarizer,
    )
    try:
        messages = await _seed(env)
        prepared = await env.compactor.force(
            env.session,
            messages,
            [],
            level=3,
            fence=env.fence,
            run_id="run-1",
            turn_id="turn-1",
            turn_index=1,
        )

        rows = await env.compactions.list_for_session(env.session.id)
        assert [row.level for row in rows] == [2, 3]
        l2, l3 = rows
        assert l3.summary["source"] == "model"
        assert l2.id in l3.summary["supersedes"]
        assert l3.summary["previous_compaction_id"] == l2.id
        assert summarizer.calls == 1
        assert "PREVIOUS SUMMARY" in summarizer.prompts[0]
        assert "You must never delete user data" in summarizer.prompts[0]

        assert l3.id in prepared.compaction_ids
        assert l2.id not in prepared.compaction_ids
        summary_texts = [
            message.text_content()
            for message in prepared.messages
            if message.role == "system" and "[compacted history]" in message.text_content()
        ]
        assert summary_texts
        assert "MODEL SUMMARY" in summary_texts[0]
    finally:
        env.db.close()


@pytest.mark.asyncio
async def test_l3_failure_falls_back_and_opens_circuit(tmp_path: Path) -> None:
    summarizer = FakeSummarizer(error=RuntimeError("model down"))
    env = await _setup(
        tmp_path,
        l1_enabled=False,
        l4_enabled=False,
        l3_failure_limit=2,
        l3_cooldown_turns=5,
        summarizer=summarizer,
    )
    try:
        messages = await _seed(env)
        first = await env.compactor.force(
            env.session,
            messages,
            [],
            level=3,
            fence=env.fence,
            turn_index=10,
        )
        assert first.compacted is True
        assert first.levels == [2]
        assert 3 not in first.levels
        assert any(
            "[compacted history]" in message.text_content()
            for message in first.messages
            if message.role == "system"
        )
        state = await env.context_state.get(env.session.id)
        assert state is not None
        assert state.consecutive_failures == 1
        assert state.disabled_until_turn is None
        failures = [
            event
            for event in await env.sessions.events_for(env.session.id)
            if event.type == EventType.CONTEXT_COMPACTION_FAILED
        ]
        assert len(failures) == 1
        assert failures[0].payload["level"] == 3

        second = await env.compactor.force(
            env.session,
            messages,
            [],
            level=3,
            fence=env.fence,
            turn_index=11,
        )
        assert second.levels == []
        state = await env.context_state.get(env.session.id)
        assert state is not None
        assert state.consecutive_failures == 2
        assert state.disabled_until_turn == 5
        assert summarizer.calls == 2

        third = await env.compactor.force(
            env.session,
            messages,
            [],
            level=3,
            fence=env.fence,
            turn_index=12,
        )
        assert summarizer.calls == 2  # L3 skipped while the circuit is open
        assert any("circuit open" in note for note in third.notes)
        # The cooldown is a remaining-attempt counter, so it clears even when
        # subsequent runs restart their turn index at 0.
        for _ in range(4):
            await env.compactor.force(
                env.session,
                messages,
                [],
                level=3,
                fence=env.fence,
                turn_index=0,
            )
        assert summarizer.calls == 2
        await env.compactor.force(
            env.session,
            messages,
            [],
            level=3,
            fence=env.fence,
            turn_index=0,
        )
        assert summarizer.calls == 3
        failures = [
            event
            for event in await env.sessions.events_for(env.session.id)
            if event.type == EventType.CONTEXT_COMPACTION_FAILED
        ]
        assert len(failures) == 3
        assert len(await env.compactions.list_for_session(env.session.id)) == 1
    finally:
        env.db.close()


@pytest.mark.asyncio
async def test_compaction_outputs_redact_registered_secret(tmp_path: Path) -> None:
    secret = "sk-compaction-secret-123"
    env = await _setup(tmp_path, l3_enabled=False, l4_enabled=False)
    try:
        env.compactor.redactor.register("compaction-test", secret)
        await _append_turn(
            env,
            "goal",
            tool_call_id="c1",
            tool_output=secret + ("x" * 10_000),
        )
        await _append_turn(env, "second", assistant_text="ok")
        await _append_turn(env, "third", assistant_text="ok")
        messages = await env.sessions.load_messages(env.session.id)
        await _prepare(env, messages)

        rows = await env.compactions.list_for_session(env.session.id)
        assert rows
        rendered = repr([row.summary for row in rows])
        assert secret not in rendered
        assert "REDACTED" in rendered
        for row in rows:
            if row.artifact_id:
                artifact_text = await env.artifacts.get_text(row.artifact_id)
                assert secret not in artifact_text
    finally:
        env.db.close()


@pytest.mark.asyncio
async def test_unreachable_target_does_not_repeat_l3_l4_every_turn(tmp_path: Path) -> None:
    summarizer = FakeSummarizer()
    checkpoints: list[str] = []

    async def checkpoint(session_id: str, fence: LeaseFence, reason: str) -> str:
        checkpoints.append(reason)
        return "ckpt"

    env = await _setup(
        tmp_path,
        l1_enabled=False,
        l2_enabled=True,
        l3_enabled=True,
        l4_enabled=True,
        summarizer=summarizer,
        checkpoint_callback=checkpoint,
        context_window=4_000,
        max_output_tokens=500,
        keep_recent_turns=2,
    )
    try:
        for index in range(3):
            await env.sessions.append_user_message(env.session.id, f"turn {index} " + ("x" * 5_000))
            await env.sessions.append_assistant_message(env.session.id, AssistantMessage.of("done"))
        messages = await env.sessions.load_messages(env.session.id)

        first = await env.compactor.prepare(
            env.session,
            messages,
            [],
            turn_index=0,
            run_id="run",
            turn_id="turn",
            fence=env.fence,
        )
        second = await env.compactor.prepare(
            env.session,
            messages,
            [],
            turn_index=1,
            run_id="run",
            turn_id="turn",
            fence=env.fence,
        )

        assert 2 in first.levels
        assert 3 not in first.levels
        assert 4 not in first.levels
        assert summarizer.calls == 0
        assert checkpoints == []
        assert any("cannot make the target reachable" in note for note in first.notes)
        # A second prepare must not add another model call or checkpoint.
        assert second.levels == []
        assert summarizer.calls == 0
        assert checkpoints == []
    finally:
        env.db.close()


@pytest.mark.asyncio
async def test_l4_resets_old_history_and_keeps_anchors_and_recent_turns(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, LeaseFence, str]] = []

    async def checkpoint(session_id: str, fence: LeaseFence, reason: str) -> str:
        calls.append((session_id, fence, reason))
        return "ckpt_test"

    env = await _setup(
        tmp_path,
        l1_enabled=False,
        l2_enabled=False,
        l3_enabled=False,
        checkpoint_callback=checkpoint,
    )
    try:
        messages = await _seed(env)
        prepared = await env.compactor.force(
            env.session,
            messages,
            [],
            level=4,
            fence=env.fence,
            run_id="run-1",
            turn_id="turn-1",
            turn_index=1,
        )

        assert calls == [(env.session.id, env.fence, "context-reset")]
        rows = await env.compactions.list_for_session(env.session.id)
        assert [row.level for row in rows] == [4]
        assert rows[0].summary["checkpoint_id"] == "ckpt_test"
        assert rows[0].summary["anchors"] >= 2

        reset_systems = [
            message.text_content()
            for message in prepared.messages
            if message.role == "system" and "[context reset at message" in message.text_content()
        ]
        assert reset_systems
        assert "2 anchor(s) preserved" in reset_systems[0]

        non_system = [
            message.text_content() for message in prepared.messages if message.role != "system"
        ]
        assert "Build the export CLI" not in non_system
        assert "You must never delete user data" not in non_system
        assert "Now fix the header bug" in non_system
        assert "Final question" in non_system

        # Anchors survive the reset and are rendered in the pinned block.
        assert prepared.anchor_block.startswith("<context-anchors>")
        assert "Build the export CLI" in prepared.anchor_block
        assert "You must never delete user data" in prepared.anchor_block
        assert len(await env.anchors.active(env.session.id)) >= 2

        event_types = [event.type for event in await env.sessions.events_for(env.session.id)]
        assert EventType.CONTEXT_RESET in event_types
    finally:
        env.db.close()


@pytest.mark.asyncio
async def test_l4_checkpoint_failure_prevents_reset(tmp_path: Path) -> None:
    async def failing_checkpoint(session_id: str, fence: LeaseFence, reason: str) -> None:
        raise RuntimeError("checkpoint storage unavailable")

    env = await _setup(
        tmp_path,
        l1_enabled=False,
        l2_enabled=False,
        l3_enabled=False,
        checkpoint_callback=failing_checkpoint,
    )
    try:
        messages = await _seed_small(env)
        prepared = await env.compactor.force(
            env.session,
            messages,
            [],
            level=4,
            fence=env.fence,
            turn_index=1,
        )

        assert await env.compactions.list_for_session(env.session.id) == []
        assert any(
            message.role == "user" and message.text_content() == "Build the export CLI"
            for message in prepared.messages
        )
        failures = [
            event
            for event in await env.sessions.events_for(env.session.id)
            if event.type == EventType.CONTEXT_COMPACTION_FAILED
        ]
        assert len(failures) == 1
        assert failures[0].payload["level"] == 4
        assert "checkpoint" in failures[0].payload["reason"]
    finally:
        env.db.close()


@pytest.mark.asyncio
async def test_compaction_never_splits_tool_pairs(tmp_path: Path) -> None:
    env = await _setup(tmp_path, l1_enabled=False, l3_enabled=False, l4_enabled=False)
    try:
        for index in range(5):
            await _append_turn(
                env,
                f"request {index}",
                assistant_text="working",
                tool_call_id=f"c{index}",
                tool_output="r" * 6_000 if index < 2 else "ok",
            )
        messages = await env.sessions.load_messages(env.session.id)
        prepared = await _prepare(env, messages)
        assert prepared.compacted is True

        call_ids: list[str] = []
        result_ids: set[str] = set()
        for message in prepared.messages:
            for call in message.tool_calls():
                call_ids.append(call.id)
            result = message.tool_result()
            if result is not None:
                result_ids.add(result.tool_call_id)
        assert set(call_ids) <= result_ids
        assert result_ids <= set(call_ids)
    finally:
        env.db.close()


@pytest.mark.asyncio
async def test_repeat_prepare_does_not_duplicate_compaction_rows(tmp_path: Path) -> None:
    env = await _setup(tmp_path, l2_enabled=False, l3_enabled=False, l4_enabled=False)
    try:
        messages = await _seed(env)
        first = await _prepare(env, messages)
        assert first.levels == [1]
        rows = await env.compactions.list_for_session(env.session.id)
        assert len(rows) == 1

        second = await _prepare(env, messages, turn_index=2)
        assert second.levels == []
        rows_after = await env.compactions.list_for_session(env.session.id)
        assert [row.id for row in rows_after] == [row.id for row in rows]
    finally:
        env.db.close()


@pytest.mark.asyncio
async def test_budget_after_compaction_is_below_trigger_and_fields_sane(
    tmp_path: Path,
) -> None:
    env = await _setup(tmp_path, l1_enabled=False, l3_enabled=False, l4_enabled=False)
    try:
        messages = await _seed(env)
        prepared = await _prepare(env, messages)
        available = env.builder.available_budget([])

        assert prepared.compacted is True
        assert prepared.before_tokens > prepared.estimated_tokens > 0
        assert prepared.estimated_tokens < env.config.trigger_ratio * available
        rows = await env.compactions.list_for_session(env.session.id)
        assert rows
        assert rows[-1].after_tokens <= rows[-1].before_tokens
        assert rows[-1].after_tokens == prepared.estimated_tokens
    finally:
        env.db.close()
