"""Metrics aggregated from a real Runtime run and TaskEngine execution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.support import make_container, new_session, text_script, tool_script

from bear_agent.observability.metrics import MetricsService
from bear_agent.runtime.stop_policy import RunStatus
from bear_agent.tasks.models import PlanDraft, TaskSpec, VerificationMode


@pytest.mark.asyncio
async def test_runtime_metrics_from_real_facts(workspace: Path, settings: Any) -> None:
    container, _ = make_container(
        settings,
        workspace,
        [
            tool_script("read-1", "read_file", {"path": "hello.txt"}),
            text_script("done"),
        ],
    )
    try:
        session = await new_session(container, workspace)
        result = await container.runtime.run(session.id, "read hello.txt")
        assert result.status is RunStatus.COMPLETED
        report = await MetricsService(container.db, config=settings.observability).report(
            since=datetime.now(UTC) - timedelta(hours=1),
            session_id=session.id,
        )
        assert report["runs"]["total"] == 1
        assert report["runs"]["completed"] == 1
        assert report["runs"]["success_rate"] == 1.0
        assert report["models"]["calls"] == 2
        assert report["models"]["input_tokens"] == 20
        assert report["models"]["output_tokens"] == 10
        assert report["models"]["total_tokens"] == 30
        assert report["tools"]["calls"] == 1
        assert report["tools"]["completed"] == 1
        assert report["tools"]["by_tool"][0]["name"] == "read_file"
        assert report["context"]["builds"] == 2
        assert report["recovery"]["checkpoints_created"] >= 1
        assert report["system"]["traces"]["total"] == 1
        assert report["window"]["session_id"] == session.id
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_task_metrics_from_real_plan_run(workspace: Path, settings: Any) -> None:
    settings.tasks.verify_completion = False
    scripts = []
    for index in range(2):
        scripts.append(
            tool_script(
                f"task-{index}",
                "task_complete",
                {
                    "summary": f"task {index} completed",
                    "evidence": [f"evidence {index}"],
                },
            )
        )
        scripts.append(text_script("task finished"))
    container, _ = make_container(settings, workspace, scripts)
    try:
        session = await new_session(container, workspace)
        await container.tasks.create_plan(
            session.id,
            PlanDraft(
                goal="Metrics plan",
                tasks=[
                    TaskSpec(
                        key=f"T{index}",
                        title=f"Task {index}",
                        description=f"Perform task {index}.",
                        acceptance_criteria=f"Task {index} is done.",
                        verification=VerificationMode.EVIDENCE,
                    )
                    for index in range(1, 3)
                ],
            ),
        )
        report = await container.runtime.run_plan(session.id)
        assert report.status == "completed"
        metrics = await MetricsService(container.db, config=settings.observability).report(
            since=datetime.now(UTC) - timedelta(hours=1),
            session_id=session.id,
        )
        assert metrics["tasks"]["total"] == 2
        assert metrics["tasks"]["completed"] == 2
        assert metrics["tasks"]["success_rate"] == 1.0
        assert metrics["tasks"]["attempts"] == 2
        assert metrics["tasks"]["plan_revisions"] == 0
        assert metrics["tools"]["by_tool"][0]["name"] == "task_complete"
        assert metrics["runs"]["plan_runs"] == 1
        assert metrics["runs"]["total"] >= 3
    finally:
        await container.close()
