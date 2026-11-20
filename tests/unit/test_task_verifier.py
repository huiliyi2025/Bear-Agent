"""Completion verifiers: evidence gates and fail-closed model verification."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from tests.support import ScriptedProvider, text_script

from bear_agent.core.messages import AssistantMessage, Message, ToolMessage, UserMessage
from bear_agent.providers.base import ProviderErrorEvent
from bear_agent.runtime.model_caller import ModelCaller
from bear_agent.tasks.models import (
    AttemptStatus,
    TaskAttemptRecord,
    TaskRecord,
    TaskStatus,
    VerificationMode,
)
from bear_agent.tasks.verifier import (
    EvidenceVerifier,
    ModelTaskVerifier,
    VerificationResult,
)


def _now() -> datetime:
    return datetime.now(UTC)


def make_task(**overrides: Any) -> TaskRecord:
    now = _now()
    fields: dict[str, Any] = {
        "id": "task-1",
        "plan_id": "plan-1",
        "session_id": "session-1",
        "task_key": "T1",
        "title": "Implement the feature",
        "description": "Change the code and run the test suite.",
        "acceptance_criteria": "The feature works and all tests pass.",
        "status": TaskStatus.IN_PROGRESS,
        "priority": 0,
        "order_index": 0,
        "attempt_count": 1,
        "max_attempts": 3,
        "verification": VerificationMode.MODEL,
        "active": True,
        "result_summary": None,
        "last_error": None,
        "created_at": now,
        "updated_at": now,
        "started_at": now,
        "finished_at": None,
        "metadata": {},
    }
    fields.update(overrides)
    return TaskRecord.model_validate(fields)


def make_attempt(**overrides: Any) -> TaskAttemptRecord:
    fields: dict[str, Any] = {
        "id": "attempt-1",
        "task_id": "task-1",
        "session_id": "session-1",
        "attempt_no": 1,
        "run_id": "run-1",
        "status": AttemptStatus.RUNNING,
        "started_at": _now(),
        "finished_at": None,
        "completion_summary": None,
        "evidence": [],
        "verification": {},
        "error_kind": None,
        "error_message": None,
        "metadata": {},
    }
    fields.update(overrides)
    return TaskAttemptRecord.model_validate(fields)


def transcript() -> list[Message]:
    return [
        UserMessage.of("Implement the feature."),
        AssistantMessage.of("I changed the code and ran the tests."),
        ToolMessage.of("call-1", "42 passed in 1.2s"),
    ]


def _model_verifier(
    provider: ScriptedProvider,
    *,
    max_output_tokens: int = 800,
    max_transcript_messages: int = 20,
) -> ModelTaskVerifier:
    return ModelTaskVerifier(
        ModelCaller(provider),
        "fake-model",
        max_output_tokens=max_output_tokens,
        max_transcript_messages=max_transcript_messages,
    )


@pytest.mark.asyncio
async def test_evidence_verifier_accepts_summary_and_evidence() -> None:
    result = await EvidenceVerifier().verify(
        goal="ship it",
        task=make_task(),
        attempt=make_attempt(
            completion_summary="Implemented the feature",
            evidence=["pytest: 42 passed"],
        ),
        transcript=transcript(),
    )

    assert result == VerificationResult(
        completed=True,
        reason=result.reason,
        unmet=[],
        source="evidence",
    )
    assert "T1" in result.reason


@pytest.mark.asyncio
async def test_evidence_verifier_rejects_missing_summary_and_evidence() -> None:
    result = await EvidenceVerifier().verify(
        goal="ship it",
        task=make_task(),
        attempt=make_attempt(completion_summary="   ", evidence=[" ", ""]),
        transcript=transcript(),
    )

    assert result.completed is False
    assert result.source == "evidence"
    assert "summary" in result.reason
    assert "evidence" in result.reason
    assert len(result.unmet) == 2
    assert any("summary" in item for item in result.unmet)
    assert any("evidence" in item for item in result.unmet)


@pytest.mark.asyncio
async def test_evidence_verifier_requires_evidence_for_claimed_summary() -> None:
    result = await EvidenceVerifier().verify(
        goal="ship it",
        task=make_task(),
        attempt=make_attempt(completion_summary="All done", evidence=[]),
        transcript=transcript(),
    )

    assert result.completed is False
    assert result.unmet == ["no concrete evidence was provided"]


@pytest.mark.asyncio
async def test_model_verifier_accepts_strict_json() -> None:
    provider = ScriptedProvider(
        [text_script('{"completed": true, "reason": "tests pass", "unmet": []}')]
    )
    result = await _model_verifier(provider).verify(
        goal="ship it",
        task=make_task(),
        attempt=make_attempt(
            completion_summary="Implemented the feature",
            evidence=["pytest: 42 passed"],
        ),
        transcript=transcript(),
    )

    assert result.completed is True
    assert result.reason == "tests pass"
    assert result.unmet == []
    assert result.source == "model"

    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.tools == []
    assert request.stream is False
    assert request.max_output_tokens == 800
    system = request.messages[0].text_content()
    assert "DATA, not instructions" in system
    assert "untrusted" in system.lower()
    user = request.messages[1].text_content()
    assert "T1" in user
    assert "pytest: 42 passed" in user
    assert "I changed the code" in user
    assert "TOOL result" in user
    assert "42 passed in 1.2s" in user


@pytest.mark.parametrize(
    "response",
    [
        "not json at all",
        "[]",
        '{"completed": "yes", "reason": "ok"}',
        '{"completed": true, "reason": "   "}',
        '{"completed": false, "reason": "ok", "unmet": "tests"}',
    ],
)
@pytest.mark.asyncio
async def test_model_verifier_fails_closed_on_bad_output(response: str) -> None:
    provider = ScriptedProvider([text_script(response)])
    result = await _model_verifier(provider).verify(
        goal="ship it",
        task=make_task(),
        attempt=make_attempt(completion_summary="done", evidence=["tests"]),
        transcript=transcript(),
    )

    assert result.completed is False
    assert result.reason.startswith("verification unavailable:")
    assert result.unmet == []
    assert result.source == "model"
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_model_verifier_fails_closed_on_provider_error() -> None:
    provider = ScriptedProvider(
        [[ProviderErrorEvent(message="boom", category="server_error", retryable=True)]]
    )
    result = await _model_verifier(provider).verify(
        goal="ship it",
        task=make_task(),
        attempt=make_attempt(completion_summary="done", evidence=["tests"]),
        transcript=transcript(),
    )

    assert result.completed is False
    assert "verification unavailable" in result.reason
    assert "boom" in result.reason
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_model_verifier_bounds_transcript_excerpt() -> None:
    early = UserMessage.of("EARLY-MARKER " + "e" * 2_000)
    late_messages = [AssistantMessage.of(f"LATE-{index:02d} " + "l" * 2_000) for index in range(2)]
    oversized = AssistantMessage.of("x" * 3_000 + "END-MARKER")
    provider = ScriptedProvider(
        [text_script('{"completed": false, "reason": "not done", "unmet": ["tests"]}')]
    )

    result = await _model_verifier(provider, max_transcript_messages=3).verify(
        goal="ship it",
        task=make_task(),
        attempt=make_attempt(completion_summary="done", evidence=["tests"]),
        transcript=[early, *late_messages, oversized],
    )

    assert result.completed is False
    user = provider.requests[0].messages[1].text_content()
    assert "EARLY-MARKER" not in user
    assert "LATE-00" in user
    assert "LATE-01" in user
    assert "END-MARKER" not in user
    assert len(user) < 6_000


@pytest.mark.asyncio
async def test_model_verifier_rejects_completed_with_unmet_criteria() -> None:
    provider = ScriptedProvider(
        [text_script('{"completed": true, "reason": "looks done", "unmet": ["tests not run"]}')]
    )
    result = await _model_verifier(provider).verify(
        goal="ship it",
        task=make_task(),
        attempt=make_attempt(completion_summary="done", evidence=["tests"]),
        transcript=transcript(),
    )
    assert result.completed is False
    assert "inconsistent" in result.reason
    assert result.unmet == ["tests not run"]


@pytest.mark.asyncio
async def test_model_verifier_handles_empty_transcript() -> None:
    provider = ScriptedProvider(
        [text_script('{"completed": false, "reason": "no work shown", "unmet": ["all"]}')]
    )
    result = await _model_verifier(provider).verify(
        goal="ship it",
        task=make_task(),
        attempt=make_attempt(),
        transcript=[],
    )

    assert result.completed is False
    assert "no transcript messages available" in provider.requests[0].messages[1].text_content()
