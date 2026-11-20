from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from tests.support import make_container, new_session, text_script, tool_script

from bear_agent.core.cancel import CancelToken
from bear_agent.runtime.stop_policy import RunStatus
from bear_agent.tools.base import ToolContext


def _long_sleep_command() -> str:
    return "Start-Sleep -Seconds 30" if sys.platform == "win32" else "sleep 30"


def _timeout_command() -> str:
    return "Start-Sleep -Seconds 5" if sys.platform == "win32" else "sleep 5"


def _large_output_command() -> str:
    if sys.platform == "win32":
        return "1..1000 | ForEach-Object { 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' }"
    return "yes xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx | head -1000"


@pytest.mark.asyncio
async def test_shell_tool_executes_and_audits(workspace: Path, settings) -> None:
    settings.sandbox.mode = "off"  # explicit opt-out for this functional test
    scripts = [
        tool_script("c1", "bash", {"command": "echo hello"}),
        text_script("done"),
    ]
    container, _provider = make_container(settings, workspace, scripts)
    try:
        session = await new_session(container, workspace)
        result = await container.runtime.run(session.id, "run echo")
        assert result.status == RunStatus.COMPLETED
        messages = await container.sessions.load_messages(session.id)
        tool_text = messages[2].tool_result()
        assert tool_text is not None
        assert "hello" in tool_text.content
        assert "[exit code 0]" in tool_text.content
        assert "NOT OS-ISOLATED" in tool_text.content
        assert (await container.audit.verify()).ok
        actions = [entry.action for entry in await container.audit.tail(20)]
        assert "policy.decision" in actions
        assert "tool.started" in actions
        assert "tool.finished" in actions
        assert await container.claims.list_all() == []
        process_rows = container.db.connection.execute(
            "SELECT status FROM process_records"
        ).fetchall()
        assert process_rows
        assert all(row["status"] == "exited" for row in process_rows)
        assert await container.processes.running() == []
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_shell_process_record_redacts_command_preview(workspace: Path, settings) -> None:
    settings.sandbox.mode = "off"
    secret = "sk-process-preview-secret"
    container, _provider = make_container(
        settings,
        workspace,
        [
            tool_script("c1", "bash", {"command": f"echo {secret}"}),
            text_script("done"),
        ],
    )
    try:
        container.redactor.register("preview-test", secret)
        session = await new_session(container, workspace)
        result = await container.runtime.run(session.id, "echo secret")
        assert result.status == RunStatus.COMPLETED
        row = container.db.connection.execute(
            "SELECT command_preview FROM process_records ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert secret not in (row["command_preview"] or "")
        assert "REDACTED" in (row["command_preview"] or "")
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_shell_nonzero_exit_is_an_error(workspace: Path, settings) -> None:
    settings.sandbox.mode = "off"
    scripts = [
        tool_script("c1", "bash", {"command": "exit 3"}),
        text_script("done"),
    ]
    container, _provider = make_container(settings, workspace, scripts)
    try:
        session = await new_session(container, workspace)
        result = await container.runtime.run(session.id, "exit")
        assert result.status == RunStatus.COMPLETED
        messages = await container.sessions.load_messages(session.id)
        tool_text = messages[2].tool_result()
        assert tool_text is not None
        assert "[exit code 3]" in tool_text.content
        assert tool_text.is_error
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_dangerous_command_is_denied_before_execution(workspace: Path, settings) -> None:
    settings.sandbox.mode = "off"
    marker = workspace / "should-not-exist.txt"
    command = (
        f"Remove-Item -Recurse -Force '{marker}'" if sys.platform == "win32" else f"rm -rf {marker}"
    )
    # A hard-deny pattern independent of the marker: recursive root deletion.
    command = "Remove-Item -Recurse -Force C:\\\\" if sys.platform == "win32" else "rm -rf /"
    scripts = [
        tool_script("c1", "bash", {"command": command}),
        text_script("blocked"),
    ]
    container, _provider = make_container(settings, workspace, scripts)
    try:
        session = await new_session(container, workspace)
        result = await container.runtime.run(session.id, "destroy")
        assert result.status == RunStatus.COMPLETED
        messages = await container.sessions.load_messages(session.id)
        tool_text = messages[2].tool_result()
        assert tool_text is not None
        assert "blocked by command policy" in tool_text.content
        assert (await container.audit.verify()).ok
        policy_records = [
            entry for entry in await container.audit.tail(20) if entry.action == "policy.decision"
        ]
        assert policy_records
        assert policy_records[-1].detail["decision"] == "deny"
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_shell_timeout(workspace: Path, settings) -> None:
    settings.sandbox.mode = "off"
    settings.shell.max_timeout_s = 2
    scripts = [
        tool_script("c1", "bash", {"command": _timeout_command(), "timeout_s": 0.5}),
        text_script("timed out"),
    ]
    container, _provider = make_container(settings, workspace, scripts)
    try:
        session = await new_session(container, workspace)
        result = await container.runtime.run(session.id, "timeout")
        assert result.status == RunStatus.COMPLETED
        messages = await container.sessions.load_messages(session.id)
        tool_text = messages[2].tool_result()
        assert tool_text is not None
        assert "[timed out" in tool_text.content
        row = await container.sessions.tool_calls.get("c1")
        assert row is not None and row["error_kind"] == "timeout"
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_shell_output_is_bounded(workspace: Path, settings) -> None:
    settings.sandbox.mode = "off"
    settings.shell.max_output_bytes = 200
    scripts = [
        tool_script("c1", "bash", {"command": _large_output_command()}),
        text_script("bounded"),
    ]
    container, _provider = make_container(settings, workspace, scripts)
    try:
        session = await new_session(container, workspace)
        result = await container.runtime.run(session.id, "large")
        assert result.status == RunStatus.COMPLETED
        messages = await container.sessions.load_messages(session.id)
        tool_text = messages[2].tool_result()
        assert tool_text is not None
        assert "[output truncated]" in tool_text.content
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_shell_cancellation_returns_cancelled(workspace: Path, settings) -> None:
    settings.sandbox.mode = "off"
    scripts = [tool_script("c1", "bash", {"command": _long_sleep_command()})]
    container, _provider = make_container(settings, workspace, scripts)
    try:
        session = await new_session(container, workspace)
        task = asyncio.create_task(container.runtime.run(session.id, "sleep"))
        await asyncio.sleep(0.8)
        assert container.runtime.cancel(session.id)
        result = await asyncio.wait_for(task, timeout=15)
        assert result.status == RunStatus.CANCELLED
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_required_sandbox_unavailable_fails_closed(workspace: Path, settings) -> None:
    if sys.platform != "win32":
        pytest.skip("require-mode fail-closed is covered by unit sandbox tests")
    settings.sandbox.mode = "require"
    scripts = [
        tool_script("c1", "bash", {"command": "echo should-not-run"}),
        text_script("blocked"),
    ]
    container, _provider = make_container(settings, workspace, scripts)
    try:
        session = await new_session(container, workspace)
        result = await container.runtime.run(session.id, "sandbox")
        assert result.status == RunStatus.COMPLETED
        messages = await container.sessions.load_messages(session.id)
        tool_text = messages[2].tool_result()
        assert tool_text is not None
        assert "sandbox unavailable" in tool_text.content
    finally:
        await container.close()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object only")
async def test_windows_process_sandbox_runs(workspace: Path, settings) -> None:
    settings.sandbox.mode = "process"
    scripts = [
        tool_script("c1", "bash", {"command": "echo job-object"}),
        text_script("done"),
    ]
    container, _provider = make_container(settings, workspace, scripts)
    try:
        session = await new_session(container, workspace)
        result = await container.runtime.run(session.id, "job")
        assert result.status == RunStatus.COMPLETED
        messages = await container.sessions.load_messages(session.id)
        tool_text = messages[2].tool_result()
        assert tool_text is not None
        assert "job-object" in tool_text.content
        assert "[sandbox: windows-job-object]" in tool_text.content
        assert (await container.audit.verify()).ok
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_shell_output_and_audit_are_redacted(workspace: Path, settings) -> None:
    settings.sandbox.mode = "off"
    secret = "super-secret-value"
    scripts = [
        tool_script("c1", "bash", {"command": f"echo {secret}"}),
        text_script("done"),
    ]
    container, _provider = make_container(settings, workspace, scripts)
    container.redactor.register("test", secret)
    try:
        session = await new_session(container, workspace)
        result = await container.runtime.run(session.id, "secret")
        assert result.status == RunStatus.COMPLETED
        messages = await container.sessions.load_messages(session.id)
        tool_text = messages[2].tool_result()
        assert tool_text is not None
        assert secret not in tool_text.content
        events = await container.sessions.events_for(session.id)
        assert secret not in str([event.payload for event in events])
        audit = await container.audit.tail(50)
        assert secret not in str([entry.detail for entry in audit])
        assert (await container.audit.verify()).ok
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_direct_shell_call_cannot_bypass_approval(workspace: Path, settings) -> None:
    settings.sandbox.mode = "off"
    container, _provider = make_container(settings, workspace, [])
    try:
        tool = container.registry.get("bash")
        assert tool is not None
        args = tool.args_model.model_validate({"command": "exit 3"})
        ctx = ToolContext(
            cwd=workspace,
            session_id="direct",
            cancel=CancelToken(),
            path_guard=container.path_guard,
            approved=False,
        )
        result = await tool.run(args, ctx)
        assert result.is_error
        assert result.error_kind == "approval_required"
    finally:
        await container.close()
