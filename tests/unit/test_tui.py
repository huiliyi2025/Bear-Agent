"""Headless Textual integration tests for the P7 TUI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from tests.support import (
    ScriptedProvider,
    default_settings,
    make_container,
    new_session,
    text_script,
    tool_script,
)

from bear_agent.core.cancel import CancelToken
from bear_agent.core.modes import PermissionMode
from bear_agent.core.usage import TokenUsage
from bear_agent.providers.base import FinishEvent, ModelProvider, TextDelta, UsageEvent
from bear_agent.tasks.models import PlanDraft, TaskSpec
from bear_agent.ui.tui import ApprovalScreen, BearTUI, TuiApprover


class SlowProvider(ModelProvider):
    name = "slow"
    capabilities = ScriptedProvider([]).capabilities

    def __init__(self) -> None:
        self.closed = False

    async def stream(self, request: Any, cancel: CancelToken):  # type: ignore[no-untyped-def]
        await asyncio.sleep(10)
        yield TextDelta(text="late")
        yield UsageEvent(usage=TokenUsage(input_tokens=1, output_tokens=1))
        yield FinishEvent(reason="stop")

    async def aclose(self) -> None:
        self.closed = True


def _settings(tmp_path: Path, mode: PermissionMode = PermissionMode.AUTO):
    settings = default_settings(tmp_path)
    settings.agent.permission_mode = mode
    settings.agent.stream = True
    return settings


async def _wait_for(pilot: Any, predicate: Any, *, limit: float = 8.0) -> None:
    try:
        async with asyncio.timeout(limit):
            while not predicate():
                await pilot.pause(0.05)
    except TimeoutError as exc:
        raise AssertionError("TUI condition was not reached") from exc


@pytest.mark.asyncio
async def test_tui_streams_and_tool_calls(workspace: Path, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    (workspace / "data.txt").write_text("tui data", encoding="utf-8")
    container, _ = make_container(
        settings,
        workspace,
        [
            tool_script("read-1", "read_file", {"path": "data.txt"}),
            text_script("tui answer"),
        ],
    )
    try:
        await container.startup()
        session = await new_session(container, workspace)
        app = BearTUI(
            container=container,
            session=session,
            settings=settings,
            workspace=workspace,
            approver=TuiApprover(),
        )
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            app.query_one("#prompt").value = "read data"
            await pilot.press("enter")
            await _wait_for(pilot, lambda: app._run_task is None)
            transcript = app.query_one("#transcript")
            rendered = "\n".join(str(line) for line in transcript.lines)
            assert "tui answer" in rendered
            assert "read_file" in rendered
            assert app._run_status == "idle"
            await app.action_quit_app()
            await pilot.pause()
        assert container._closed is True
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_tui_approval_shows_arguments_and_denies(workspace: Path, tmp_path: Path) -> None:
    settings = _settings(tmp_path, PermissionMode.SUPERVISED)
    approver = TuiApprover()
    container, _ = make_container(
        settings,
        workspace,
        [
            tool_script("write-1", "write_file", {"path": "denied.txt", "content": "x"}),
            text_script("done"),
        ],
        approver=approver,
    )
    try:
        await container.startup()
        session = await new_session(container, workspace)
        app = BearTUI(
            container=container,
            session=session,
            settings=settings,
            workspace=workspace,
            approver=approver,
        )
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            app.query_one("#prompt").value = "write denied"
            await pilot.press("enter")
            await _wait_for(pilot, lambda: len(app.screen_stack) > 1)
            await pilot.pause(0.1)
            screen = app.screen
            assert isinstance(screen, ApprovalScreen)
            args = "\n".join(str(line) for line in screen.query_one("#approval-args").lines)
            assert "denied.txt" in args
            await pilot.press("enter")
            await pilot.pause(0.1)
            assert app.screen is screen, "Enter must not approve an approval modal"
            await pilot.press("d")
            await _wait_for(pilot, lambda: app._run_task is None)
            assert not (workspace / "denied.txt").exists()
            await app.action_quit_app()
            await pilot.pause()
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_tui_escape_redacts_approval_arguments(workspace: Path, tmp_path: Path) -> None:
    from bear_agent.policy.models import ApprovalRequest

    settings = _settings(tmp_path, PermissionMode.SUPERVISED)
    approver = TuiApprover()
    container, _ = make_container(settings, workspace, [text_script("done")], approver=approver)
    try:
        await container.startup()
        session = await new_session(container, workspace)
        app = BearTUI(
            container=container,
            session=session,
            settings=settings,
            workspace=workspace,
            approver=approver,
        )
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            future = asyncio.get_running_loop().create_future()
            app.submit_approval(
                ApprovalRequest(
                    tool_name="bash",
                    arguments={"command": "echo token=sk-abcdefghijklmnop"},
                    reason="test",
                    session_id=session.id,
                ),
                future,
            )
            await _wait_for(pilot, lambda: len(app.screen_stack) > 1)
            await pilot.pause(0.1)
            screen = app.screen
            args = "\n".join(str(line) for line in screen.query_one("#approval-args").lines)
            assert "sk-abcdefghijklmnop" not in args
            await pilot.press("d")
            await _wait_for(pilot, lambda: future.done())
            assert future.result().approved is False
            await app.action_quit_app()
            await pilot.pause()
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_tui_cancel_and_cleanup(workspace: Path, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    container, _ = make_container(
        settings, workspace, [], provider=SlowProvider(), approver=TuiApprover()
    )
    try:
        await container.startup()
        session = await new_session(container, workspace)
        app = BearTUI(
            container=container,
            session=session,
            settings=settings,
            workspace=workspace,
            approver=TuiApprover(),
        )
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            app.query_one("#prompt").value = "slow request"
            await pilot.press("enter")
            await _wait_for(pilot, lambda: container.runtime.is_running(session.id))
            await pilot.press("ctrl+c")
            await _wait_for(pilot, lambda: not container.runtime.is_running(session.id))
            await app.action_quit_app()
            await pilot.pause()
        assert container._closed is True
        from bear_agent.sessions.service import SessionService
        from bear_agent.storage.database import Database

        probe = Database(settings.resolved_data_dir() / "bear.db")
        try:
            restored = await SessionService(probe).get(session.id)
        finally:
            probe.close()
        assert restored is not None
        assert restored.lease_owner is None
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_tui_plan_command_and_metrics_command(workspace: Path, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    plan = {
        "goal": "TUI plan",
        "tasks": [
            {
                "key": "T1",
                "title": "One",
                "description": "Do one.",
                "acceptance_criteria": "Done.",
                "dependencies": [],
                "verification": "evidence",
            }
        ],
    }
    provider = ScriptedProvider([text_script(json.dumps(plan))])
    container, _ = make_container(
        settings, workspace, [], provider=provider, approver=TuiApprover()
    )
    try:
        await container.startup()
        session = await new_session(container, workspace)
        app = BearTUI(
            container=container,
            session=session,
            settings=settings,
            workspace=workspace,
            approver=TuiApprover(),
        )
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            app.query_one("#prompt").value = "/plan TUI plan"
            await pilot.press("enter")
            await _wait_for(pilot, lambda: app._job_task is None)
            stored = await container.tasks.get_plan(session.id)
            assert stored is not None
            app.query_one("#prompt").value = "/metrics"
            await pilot.press("enter")
            await pilot.pause(0.1)
            await app.action_quit_app()
            await pilot.pause()
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_tui_command_smoke(workspace: Path, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    container, _ = make_container(settings, workspace, [], approver=TuiApprover())
    try:
        await container.startup()
        session = await new_session(container, workspace)
        await container.tasks.create_plan(
            session.id,
            PlanDraft(
                goal="TUI smoke",
                tasks=[
                    TaskSpec(
                        key="T1",
                        title="One",
                        description="Do one.",
                        acceptance_criteria="Done.",
                        verification="evidence",
                    )
                ],
            ),
        )
        task = (await container.tasks.list_tasks(session.id))[0]
        app = BearTUI(
            container=container,
            session=session,
            settings=settings,
            workspace=workspace,
            approver=TuiApprover(),
        )
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            for command in (
                "/help",
                "/session",
                "/sessions",
                "/status",
                "/tools",
                "/metrics",
                "/anchors",
                "/checkpoints",
                "/mcp",
                "/plugins",
                "/hooks",
                "/tasks",
                f"/task {task.id}",
                "/permission auto",
                "/anchor remember this",
                "/compact",
            ):
                await app._dispatch_command(command)
                if app._job_task is not None:
                    await _wait_for(pilot, lambda: app._job_task is None)
            await app._dispatch_command("/fork")
            await app._dispatch_command("/new")
            await app._dispatch_command("/resume " + session.id)
            await app._dispatch_command("/doctor")
            await _wait_for(pilot, lambda: app._job_task is None)
            await app._dispatch_command("/cancel-plan")
            await _wait_for(pilot, lambda: app._job_task is None)
            await app.action_quit_app()
            await pilot.pause()
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_tui_approval_allow_session(workspace: Path, tmp_path: Path) -> None:
    settings = _settings(tmp_path, PermissionMode.SUPERVISED)
    approver = TuiApprover()
    container, _ = make_container(
        settings,
        workspace,
        [
            tool_script("write-2", "write_file", {"path": "allowed.txt", "content": "ok"}),
            text_script("done"),
        ],
        approver=approver,
    )
    try:
        await container.startup()
        session = await new_session(container, workspace)
        app = BearTUI(
            container=container,
            session=session,
            settings=settings,
            workspace=workspace,
            approver=approver,
        )
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            app.query_one("#prompt").value = "write allowed"
            await pilot.press("enter")
            await _wait_for(pilot, lambda: len(app.screen_stack) > 1)
            await pilot.pause(0.1)
            await pilot.press("s")
            await _wait_for(pilot, lambda: app._run_task is None)
            assert (workspace / "allowed.txt").read_text(encoding="utf-8") == "ok"
            await app.action_quit_app()
            await pilot.pause()
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_tui_run_plan(workspace: Path, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.tasks.verify_completion = False
    container, _ = make_container(
        settings,
        workspace,
        [
            tool_script(
                "task-1",
                "task_complete",
                {"summary": "done in tui", "evidence": ["tui evidence"]},
            ),
            text_script("task done"),
        ],
        approver=TuiApprover(),
    )
    try:
        await container.startup()
        session = await new_session(container, workspace)
        await container.tasks.create_plan(
            session.id,
            PlanDraft(
                goal="TUI run plan",
                tasks=[
                    TaskSpec(
                        key="T1",
                        title="One",
                        description="Do one.",
                        acceptance_criteria="Done.",
                        verification="evidence",
                    )
                ],
            ),
        )
        app = BearTUI(
            container=container,
            session=session,
            settings=settings,
            workspace=workspace,
            approver=TuiApprover(),
        )
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            app.query_one("#prompt").value = "/run-plan"
            await pilot.press("enter")
            await _wait_for(pilot, lambda: app._run_task is None)
            plan = await container.tasks.get_plan(session.id)
            assert plan is not None
            assert plan.status.value == "completed"
            await app.action_quit_app()
            await pilot.pause()
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_tui_quit_while_run_active_cleans_up(workspace: Path, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    container, _ = make_container(
        settings, workspace, [], provider=SlowProvider(), approver=TuiApprover()
    )
    try:
        await container.startup()
        session = await new_session(container, workspace)
        app = BearTUI(
            container=container,
            session=session,
            settings=settings,
            workspace=workspace,
            approver=TuiApprover(),
        )
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            app.query_one("#prompt").value = "slow quit"
            await pilot.press("enter")
            await _wait_for(pilot, lambda: container.runtime.is_running(session.id))
            await app.action_quit_app()
            await pilot.pause()
        assert container._closed is True
        from bear_agent.sessions.service import SessionService
        from bear_agent.storage.database import Database

        probe = Database(settings.resolved_data_dir() / "bear.db")
        try:
            restored = await SessionService(probe).get(session.id)
        finally:
            probe.close()
        assert restored is not None
        assert restored.lease_owner is None
    finally:
        await container.close()
