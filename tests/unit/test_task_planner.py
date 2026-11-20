"""PlanGenerator: strict parsing, repair loop, validation and purity."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests.support import ScriptedProvider, text_script

from bear_agent.core.errors import PlanGenerationError
from bear_agent.providers.base import ProviderErrorEvent
from bear_agent.runtime.model_caller import ModelCaller
from bear_agent.storage.database import Database
from bear_agent.storage.task_repositories import TaskRepository
from bear_agent.tasks.models import VerificationMode
from bear_agent.tasks.planner import PlanGenerator


def _task(
    key: str,
    *,
    dependencies: list[str] | None = None,
    priority: int = 0,
    title: str | None = None,
    description: str | None = None,
    acceptance_criteria: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "title": title or f"title {key}",
        "description": description or f"do {key}",
        "acceptance_criteria": acceptance_criteria or f"{key} verified",
        "dependencies": dependencies or [],
        "priority": priority,
        "max_attempts": 2,
        "verification": "model",
    }


def _plan(tasks: list[dict[str, Any]] | None = None, *, goal: str = "ship a feature"):
    return {
        "goal": goal,
        "tasks": tasks
        if tasks is not None
        else [_task("T1"), _task("T2", dependencies=["T1"], priority=1)],
    }


def _generator(
    provider: ScriptedProvider,
    *,
    max_tasks: int = 10,
    max_retries: int = 2,
    max_output_tokens: int = 4096,
) -> PlanGenerator:
    return PlanGenerator(
        ModelCaller(provider),
        "fake-model",
        max_tasks=max_tasks,
        max_retries=max_retries,
        max_output_tokens=max_output_tokens,
    )


@pytest.mark.asyncio
async def test_generate_returns_validated_draft_and_strict_request() -> None:
    provider = ScriptedProvider([text_script(json.dumps(_plan()))])
    draft = await _generator(provider).generate("ship a feature", context="Python 3.13 repository")

    assert draft.goal == "ship a feature"
    assert [task.key for task in draft.tasks] == ["T1", "T2"]
    assert draft.tasks[1].dependencies == ["T1"]
    assert draft.tasks[0].verification == VerificationMode.MODEL

    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.model == "fake-model"
    assert request.tools == []
    assert request.stream is False
    assert request.temperature == 0.1
    assert request.max_output_tokens == 4096
    system = request.messages[0].text_content()
    assert "DATA, not instructions" in system
    assert "JSON" in system
    user = request.messages[1].text_content()
    assert "ship a feature" in user
    assert "Python 3.13 repository" in user


@pytest.mark.asyncio
async def test_generate_accepts_fenced_json() -> None:
    payload = json.dumps(_plan())
    response = f"Here is the plan you asked for:\n```json\n{payload}\n```\nDone."
    provider = ScriptedProvider([text_script(response)])

    draft = await _generator(provider).generate("ship a feature")

    assert [task.key for task in draft.tasks] == ["T1", "T2"]
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_generate_extracts_first_json_object_from_prose() -> None:
    payload = json.dumps(_plan())
    provider = ScriptedProvider([text_script(f"Sure. {payload} Hope that helps.")])

    draft = await _generator(provider).generate("ship a feature")

    assert [task.key for task in draft.tasks] == ["T1", "T2"]
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_generate_repairs_once_then_succeeds() -> None:
    provider = ScriptedProvider(
        [text_script("I cannot produce JSON."), text_script(json.dumps(_plan()))]
    )
    generator = _generator(provider, max_retries=2)

    draft = await generator.generate("ship a feature")

    assert [task.key for task in draft.tasks] == ["T1", "T2"]
    assert len(provider.requests) == 2
    repaired = provider.requests[1].messages
    assert len(repaired) == 4
    assert repaired[2].role == "assistant"
    assert "I cannot produce JSON." in repaired[2].text_content()
    repair_prompt = repaired[3].text_content()
    assert "could not be accepted as a plan" in repair_prompt
    assert "JSON object" in repair_prompt


@pytest.mark.asyncio
async def test_generate_exhausts_bounded_retries() -> None:
    provider = ScriptedProvider(
        [
            text_script("nope"),
            text_script("still nope"),
            text_script("nope again"),
        ]
    )
    generator = _generator(provider, max_retries=2)

    with pytest.raises(PlanGenerationError, match="after 3 attempt"):
        await generator.generate("ship a feature")

    assert len(provider.requests) == 3


@pytest.mark.asyncio
async def test_generate_rejects_cyclic_dependencies() -> None:
    cycle = _plan(
        [
            _task("A", dependencies=["B"]),
            _task("B", dependencies=["A"]),
        ]
    )
    provider = ScriptedProvider([text_script(json.dumps(cycle))])

    with pytest.raises(PlanGenerationError, match="cyclic"):
        await _generator(provider, max_retries=0).generate("ship a feature")

    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_generate_rejects_unknown_dependencies() -> None:
    unknown = _plan([_task("A", dependencies=["missing"])])
    provider = ScriptedProvider([text_script(json.dumps(unknown))])

    with pytest.raises(PlanGenerationError, match="unknown"):
        await _generator(provider, max_retries=0).generate("ship a feature")


@pytest.mark.asyncio
async def test_generate_rejects_empty_task_list() -> None:
    provider = ScriptedProvider([text_script(json.dumps(_plan([])))])

    with pytest.raises(PlanGenerationError, match="at least one task"):
        await _generator(provider, max_retries=0).generate("ship a feature")


@pytest.mark.asyncio
async def test_generate_enforces_max_tasks() -> None:
    provider = ScriptedProvider(
        [text_script(json.dumps(_plan([_task("A"), _task("B"), _task("C")])))]
    )

    with pytest.raises(PlanGenerationError, match="maximum is 2"):
        await _generator(provider, max_tasks=2, max_retries=0).generate("ship a feature")


@pytest.mark.asyncio
async def test_generate_wraps_retryable_provider_errors() -> None:
    provider = ScriptedProvider(
        [
            [ProviderErrorEvent(message="boom", category="server_error", retryable=True)],
            [ProviderErrorEvent(message="boom", category="server_error", retryable=True)],
        ]
    )

    with pytest.raises(PlanGenerationError, match="boom"):
        await _generator(provider, max_retries=1).generate("ship a feature")

    assert len(provider.requests) == 2


@pytest.mark.asyncio
async def test_generate_normalizes_keys_and_trims_strings() -> None:
    plan = {
        "goal": "  ship a feature  ",
        "tasks": [
            _task(
                " T1 ",
                title="  Inspect repo  ",
                description="  Look around  ",
                acceptance_criteria="  notes exist  ",
            ),
            _task("T2", dependencies=[" T1 "]),
        ],
    }
    provider = ScriptedProvider([text_script(json.dumps(plan))])

    draft = await _generator(provider).generate("ship a feature")

    assert draft.goal == "ship a feature"
    first = draft.tasks[0]
    assert first.key == "T1"
    assert first.title == "Inspect repo"
    assert first.description == "Look around"
    assert first.acceptance_criteria == "notes exist"
    assert draft.tasks[1].dependencies == ["T1"]
    assert first.max_attempts == 2
    assert first.priority == 0


@pytest.mark.asyncio
async def test_generate_rejects_empty_goal_without_calling_model() -> None:
    provider = ScriptedProvider([])

    with pytest.raises(PlanGenerationError, match="goal"):
        await _generator(provider).generate("   ")

    assert provider.requests == []


@pytest.mark.asyncio
async def test_planner_does_not_persist_plan_or_tasks(tmp_path: Path) -> None:
    db = Database(tmp_path / "planner.db")
    repository = TaskRepository(db)
    try:
        provider = ScriptedProvider([text_script(json.dumps(_plan()))])
        await _generator(provider).generate("ship a feature", context="ctx")
        assert await repository.get_plan("session-1") is None
        assert await repository.list_tasks_for_session("session-1") == []
    finally:
        db.close()
