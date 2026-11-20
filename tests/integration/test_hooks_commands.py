"""Integration: external command hooks run as real subprocesses."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from tests.support import make_container, new_session, text_script, tool_script

from bear_agent.audit.service import AuditService
from bear_agent.config.models import HookCommandConfig, HooksConfig
from bear_agent.core.errors import HookError
from bear_agent.core.events import EventType
from bear_agent.hooks import HookDecision, HookEvent, HookManager, HookRegistration
from bear_agent.observability.bus import EventBus
from bear_agent.observability.recorder import EventRecorder
from bear_agent.runtime.stop_policy import RunStatus
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.sessions.service import SessionService
from bear_agent.storage.database import Database


def write_script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(
        "import json\nimport os\nimport sys\nimport time\nfrom pathlib import Path\n\n" + body,
        encoding="utf-8",
    )
    return path


def command_config(
    script: Path,
    *,
    event: str = "tool_before",
    args: list[str] | None = None,
    timeout_s: float = 5.0,
    fail_mode: str = "open",
    hook_id: str = "test",
) -> HookCommandConfig:
    return HookCommandConfig(
        id=hook_id,
        event=event,
        command=sys.executable,
        args=["-I", str(script), *(args or [])],
        timeout_s=timeout_s,
        fail_mode=fail_mode,  # type: ignore[arg-type]
    )


def manager_for(tmp_path: Path, *commands: HookCommandConfig, **config: Any) -> HookManager:
    return HookManager(
        config=HooksConfig(allow_external=True, commands=list(commands), **config),
        workspace=tmp_path,
    )


@pytest.mark.asyncio
async def test_relative_hook_script_is_not_resolved_from_agent_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "pwned.txt"
    (workspace / "evil.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('pwned')\n",
        encoding="utf-8",
    )
    safe = tmp_path / "safe"
    safe.mkdir()
    manager = HookManager(
        config=HooksConfig(
            allow_external=True,
            commands=[
                HookCommandConfig(
                    id="relative",
                    event="tool_before",
                    command=sys.executable,
                    args=["-I", "evil.py"],
                    fail_mode="closed",
                )
            ],
        ),
        workspace=workspace,
        safe_cwd=safe,
    )
    outcome = await manager.before_tool_call(tool_name="bash", session_id="s1")
    assert not marker.exists()
    assert outcome.failed_closed is True


def test_invalid_command_hook_event_fails_startup(tmp_path: Path) -> None:
    script = write_script(tmp_path, "noop.py", "print(json.dumps({'action':'continue'}))\n")
    with pytest.raises(HookError, match="invalid event"):
        manager_for(tmp_path, command_config(script, event="before_tool_call_typo"))


@pytest.mark.asyncio
async def test_external_disabled_is_never_executed(tmp_path: Path) -> None:
    marker = tmp_path / "ran.txt"
    script = write_script(
        tmp_path,
        "marker.py",
        'Path(sys.argv[1]).write_text("ran", encoding="utf-8")\n'
        'print(json.dumps({"action": "continue"}))\n',
    )
    manager = HookManager(
        config=HooksConfig(
            allow_external=False, commands=[command_config(script, args=[str(marker)])]
        ),
        workspace=tmp_path,
    )

    outcome = await manager.before_tool_call(tool_name="bash", session_id="s1")

    assert outcome.denied is False
    assert not marker.exists()
    status = manager.status()
    entry = next(item for item in status if item["id"] == "cmd:test")
    assert entry["status"] == "error"
    assert "allow_external" in entry["error"]


@pytest.mark.asyncio
async def test_register_command_at_runtime(tmp_path: Path) -> None:
    script = write_script(
        tmp_path,
        "runtime.py",
        'print(json.dumps({"action": "deny", "reason": "runtime deny"}))\n',
    )
    manager = HookManager(config=HooksConfig(allow_external=True), workspace=tmp_path)
    registration = manager.register_command(command_config(script))

    assert registration.id == "cmd:test"
    outcome = await manager.before_tool_call(tool_name="bash", session_id="s1")
    assert outcome.denied is True
    assert outcome.reason == "runtime deny"

    locked = HookManager(config=HooksConfig(allow_external=False), workspace=tmp_path)
    with pytest.raises(HookError, match="allow_external"):
        locked.register_command(command_config(script))


@pytest.mark.asyncio
async def test_valid_json_deny_and_stdin_contract(tmp_path: Path) -> None:
    capture = tmp_path / "stdin.json"
    script = write_script(
        tmp_path,
        "deny.py",
        "invocation = json.load(sys.stdin)\n"
        'Path(sys.argv[1]).write_text(json.dumps(invocation), encoding="utf-8")\n'
        'print(json.dumps({"action": "deny", "reason": "blocked by external hook"}))\n',
    )
    manager = manager_for(tmp_path, command_config(script, args=[str(capture)], hook_id="deny"))

    outcome = await manager.before_tool_call(
        tool_name="bash",
        arguments={"command": "ls"},
        session_id="s1",
        run_id="r1",
        turn_id="t1",
    )

    assert outcome.denied is True
    assert outcome.reason == "blocked by external hook"
    assert outcome.results[0].hook_id == "cmd:deny"
    assert outcome.results[0].status == "denied"

    invocation = json.loads(capture.read_text(encoding="utf-8"))
    assert invocation["event"] == "tool_before"
    assert invocation["session_id"] == "s1"
    assert invocation["run_id"] == "r1"
    assert invocation["turn_id"] == "t1"
    assert invocation["hook_id"] == "cmd:deny"
    assert invocation["payload"]["tool"] == "bash"
    assert Path(invocation["workspace"]) == manager.workspace


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail_mode", "denied", "failed_closed"),
    [("open", False, False), ("closed", True, True)],
)
async def test_invalid_json_applies_fail_mode(
    tmp_path: Path, fail_mode: str, denied: bool, failed_closed: bool
) -> None:
    script = write_script(tmp_path, "invalid.py", 'print("this is not a json object")\n')
    manager = manager_for(tmp_path, command_config(script, fail_mode=fail_mode))

    outcome = await manager.before_tool_call(tool_name="bash", session_id="s1")

    assert outcome.denied is denied
    assert outcome.failed_closed is failed_closed
    assert outcome.results[0].status == "failed"
    assert "invalid JSON" in (outcome.results[0].error or "")


@pytest.mark.asyncio
async def test_timeout_kills_process_and_fails_closed(tmp_path: Path) -> None:
    script = write_script(
        tmp_path,
        "slow.py",
        'time.sleep(10)\nprint(json.dumps({"action": "continue"}))\n',
    )
    manager = manager_for(
        tmp_path,
        command_config(script, timeout_s=0.2, fail_mode="closed", hook_id="slow"),
    )

    started = time.monotonic()
    outcome = await manager.before_tool_call(tool_name="bash", session_id="s1")
    elapsed = time.monotonic() - started

    assert elapsed < 5.0
    assert outcome.denied is True
    assert outcome.failed_closed is True
    assert outcome.results[0].status == "timeout"
    assert "timed out" in (outcome.results[0].error or "")


@pytest.mark.asyncio
async def test_nonzero_exit_redacts_stderr(tmp_path: Path) -> None:
    script = write_script(
        tmp_path,
        "fail.py",
        'sys.stderr.write("boom token=sk-abcdefghijklmnop\\n")\nsys.exit(3)\n',
    )
    manager = manager_for(tmp_path, command_config(script))

    outcome = await manager.before_tool_call(tool_name="bash", session_id="s1")

    assert outcome.denied is False
    error = outcome.results[0].error or ""
    assert "exited with code 3" in error
    assert "sk-abcdefghijklmnop" not in error
    assert "[REDACTED]" in error


@pytest.mark.asyncio
async def test_stdout_overflow_is_bounded_and_killed(tmp_path: Path) -> None:
    script = write_script(
        tmp_path,
        "flood.py",
        "sys.stdout.write('x' * (70 * 1024))\n"
        "sys.stdout.flush()\n"
        'print(json.dumps({"action": "continue"}))\n',
    )
    manager = manager_for(tmp_path, command_config(script))

    started = time.monotonic()
    outcome = await manager.before_tool_call(tool_name="bash", session_id="s1")
    elapsed = time.monotonic() - started

    assert elapsed < 5.0
    assert outcome.denied is False
    assert outcome.results[0].status == "failed"
    assert "exceeded" in (outcome.results[0].error or "")


@pytest.mark.asyncio
async def test_arguments_are_passed_literally_without_shell(tmp_path: Path) -> None:
    output = tmp_path / "argv.json"
    script = write_script(
        tmp_path,
        "argv.py",
        'Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]), encoding="utf-8")\n'
        'print(json.dumps({"action": "continue"}))\n',
    )
    injected = '"; echo INJECTED > injected.txt; $(echo ALSO_INJECTED)'
    manager = manager_for(tmp_path, command_config(script, args=[str(output), injected]))

    outcome = await manager.before_tool_call(tool_name="bash", session_id="s1")

    assert outcome.denied is False
    assert json.loads(output.read_text(encoding="utf-8")) == [injected]
    assert not (tmp_path / "injected.txt").exists()


@pytest.mark.asyncio
async def test_external_context_append_is_rejected(tmp_path: Path) -> None:
    script = write_script(
        tmp_path,
        "context.py",
        'print(json.dumps({"action": "continue", "context_append": "inject"}))\n',
    )
    manager = manager_for(tmp_path, command_config(script, event="model_before"))

    outcome = await manager.before_model_call(model="m", messages=1, tools=0, session_id="s1")

    assert outcome.context_append == ""
    assert outcome.denied is False
    assert outcome.results[0].status == "failed"
    assert "context_append" in (outcome.results[0].error or "")


@pytest.mark.asyncio
async def test_command_environment_is_allowlisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEAR_HOOK_TEST_SECRET", "leak-me")
    output = tmp_path / "env.json"
    script = write_script(
        tmp_path,
        "env.py",
        "state = {\n"
        '    "secret": os.environ.get("BEAR_HOOK_TEST_SECRET", "missing"),\n'
        '    "path": bool(os.environ.get("PATH")),\n'
        "}\n"
        'Path(sys.argv[1]).write_text(json.dumps(state), encoding="utf-8")\n'
        'print(json.dumps({"action": "continue"}))\n',
    )
    manager = manager_for(tmp_path, command_config(script, args=[str(output)]))

    outcome = await manager.before_tool_call(tool_name="bash", session_id="s1")

    assert outcome.denied is False
    state = json.loads(output.read_text(encoding="utf-8"))
    assert state["secret"] == "missing"
    assert state["path"] is True


@pytest.mark.asyncio
async def test_persisted_hook_events_and_audit_are_redacted(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    try:
        sessions = SessionService(db)
        redactor = SecretRedactor()
        recorder = EventRecorder(sessions, EventBus(), redactor)
        audit = AuditService(db, redactor=redactor)
        session = await sessions.create(cwd=tmp_path, model="m", provider="p")
        manager = HookManager(
            config=HooksConfig(),
            recorder=recorder,
            audit=audit,
            redactor=redactor,
            workspace=tmp_path,
        )

        async def deny(invocation: object) -> HookDecision:
            return HookDecision(action="deny", reason="blocked token=sk-abcdefghijklmnop")

        manager.register(
            HookRegistration(
                id="deny",
                events=frozenset({HookEvent.TOOL_BEFORE}),
                handler=deny,
                can_deny=True,
            )
        )
        outcome = await manager.before_tool_call(
            tool_name="bash", arguments={"command": "ls"}, session_id=session.id
        )
        assert outcome.denied is True

        events = await sessions.events_for(session.id)
        types = [event.type for event in events]
        assert EventType.HOOK_STARTED in types
        assert EventType.HOOK_DENIED in types
        serialized = json.dumps([event.to_json() for event in events])
        assert "sk-abcdefghijklmnop" not in serialized
        assert "[REDACTED]" in serialized

        entries = await audit.tail(20)
        assert any(entry.actor == "hook" and entry.action == "hook.denied" for entry in entries)
        audit_serialized = json.dumps(
            [{"detail": entry.detail, "resource": entry.resource} for entry in entries]
        )
        assert "sk-abcdefghijklmnop" not in audit_serialized
        assert "[REDACTED]" in audit_serialized
    finally:
        db.close()


@pytest.mark.asyncio
async def test_runtime_tool_hook_denial_returns_tool_error(workspace: Path, settings: Any) -> None:
    """ToolExecutor integration: deny after policy/claims becomes a tool error."""
    container, _ = make_container(
        settings,
        workspace,
        [
            tool_script("call-deny", "read_file", {"path": "hello.txt"}),
            text_script("done"),
        ],
    )
    observed: list[HookEvent] = []

    async def deny(invocation: object) -> HookDecision:
        return HookDecision(action="deny", reason="blocked by runtime hook")

    async def observe(invocation: object) -> None:
        observed.append(invocation.event)  # type: ignore[attr-defined]

    container.hooks.register(
        HookRegistration(
            id="deny",
            events=frozenset({HookEvent.TOOL_BEFORE}),
            handler=deny,
            can_deny=True,
        )
    )
    container.hooks.register(
        HookRegistration(
            id="observe",
            events=frozenset({HookEvent.RUN_COMPLETED}),
            handler=observe,
        )
    )
    try:
        session = await new_session(container, workspace)
        result = await container.runtime.run(session.id, "read hello.txt")
        assert result.status is RunStatus.COMPLETED

        row = await container.sessions.tool_calls.get("call-deny")
        assert row is not None
        assert row["status"] == "failed"
        assert row["error_kind"] == "hook_denied"

        messages = await container.sessions.load_messages(session.id)
        tool_text = [
            message.tool_result().content
            for message in messages
            if message.role == "tool" and message.tool_result() is not None
        ]
        assert any("blocked by runtime hook" in text for text in tool_text)

        # Bootstrap subscribes HookManager.on_bus_event to the EventBus.
        assert HookEvent.RUN_COMPLETED in observed
    finally:
        await container.close()
