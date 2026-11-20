from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from tests.support import ScriptedProvider, text_script, tool_script
from typer.testing import CliRunner

from bear_agent.bootstrap import build_container as real_build_container
from bear_agent.secrets.store import InMemorySecretStore
from bear_agent.ui.cli import main


def _config(tmp_path: Path, *, local: bool = False) -> Path:
    data_dir = (tmp_path / "data").as_posix()
    base_url = "http://127.0.0.1:11434/v1" if local else "http://fake.local/v1"
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
[agent]
default_provider = "fake"
default_model = "fake-model"
max_turns = 5

[providers.fake]
type = "openai_compat"
base_url = "{base_url}"
{'api_key = "test"' if not local else ""}
models = ["fake-model"]

[storage]
data_dir = "{data_dir}"
""",
        encoding="utf-8",
    )
    return path


def _patch_provider(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    _patch_scripts(monkeypatch, [text_script(text)])


def _patch_scripts(
    monkeypatch: pytest.MonkeyPatch,
    scripts: list[list],  # type: ignore[type-arg]
) -> None:
    def fake_build(settings, *, cwd, approver=None, provider=None, bus=None):
        scripted = ScriptedProvider([list(script) for script in scripts])
        return real_build_container(
            settings, cwd=cwd, approver=approver, provider=scripted, bus=bus
        )

    monkeypatch.setattr(main, "build_container", fake_build)


def test_help_and_version() -> None:
    runner = CliRunner()
    result = runner.invoke(main.app, ["--help"])
    assert result.exit_code == 0
    assert "run" in result.output
    assert "tui" in result.output
    tui_help = runner.invoke(main.app, ["tui", "--help"])
    assert tui_help.exit_code == 0
    assert "TUI" in tui_help.output
    result = runner.invoke(main.app, ["--version"])
    assert result.exit_code == 0
    assert "bear-agent" in result.output


def test_tui_requires_a_terminal(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(main.app, ["tui", "--config", str(_config(tmp_path))])
    assert result.exit_code == 2
    assert "interactive terminal" in result.output


def test_init_creates_config(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "config.toml"
    result = runner.invoke(main.app, ["init", "--config", str(target)])
    assert result.exit_code == 0
    assert target.exists()
    assert "[providers.deepseek]" in target.read_text(encoding="utf-8")


def test_config_show_redacts_key(tmp_path: Path) -> None:
    runner = CliRunner()
    config = _config(tmp_path)
    result = runner.invoke(main.app, ["config", "show", "--config", str(config)])
    assert result.exit_code == 0
    assert "default_provider" in result.output
    assert "test" not in result.output


def test_doctor_local_provider(tmp_path: Path) -> None:
    runner = CliRunner()
    config = _config(tmp_path, local=True)
    result = runner.invoke(main.app, ["doctor", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "data-dir" in result.output


def test_no_args_opens_chat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path, local=True)
    monkeypatch.setenv("BEAR_CONFIG", str(config))
    runner = CliRunner()
    result = runner.invoke(main.app, [], input="/exit\n")
    assert result.exit_code == 0, result.output
    assert "Bear-Agent" in result.output
    assert "/exit" in result.output


def test_run_json_with_scripted_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    config = _config(tmp_path)
    _patch_provider(monkeypatch, "cli answer")
    result = runner.invoke(
        main.app,
        ["run", "hello", "--config", str(config), "--json"],
    )
    assert result.exit_code == 0, result.output
    lines = [json.loads(line) for line in result.output.splitlines() if line.startswith("{")]
    assert any(item.get("type") == "result" for item in lines)
    assert any(
        item.get("type") == "model.text.delta"
        and item.get("payload", {}).get("text") == "cli answer"
        for item in lines
    )
    session_events = [item for item in lines if item.get("type") == "session.created"]
    assert session_events

    session_id = session_events[0]["session_id"]
    listed = runner.invoke(main.app, ["sessions", "list", "--config", str(config)])
    assert listed.exit_code == 0, listed.output
    shown = runner.invoke(main.app, ["sessions", "show", session_id, "--config", str(config)])
    assert shown.exit_code == 0, shown.output
    assert "hello" in shown.output

    verified = runner.invoke(main.app, ["audit", "verify", "--config", str(config)])
    assert verified.exit_code == 0, verified.output
    assert "audit chain OK" in verified.output
    tailed = runner.invoke(main.app, ["audit", "tail", "--config", str(config)])
    assert tailed.exit_code == 0, tailed.output
    assert "Audit tail" in tailed.output


def test_session_checkpoints_fork_and_recover_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    config = _config(tmp_path)
    _patch_provider(monkeypatch, "answer")
    ran = runner.invoke(main.app, ["run", "hello", "--config", str(config), "--json"])
    assert ran.exit_code == 0, ran.output
    created = [
        json.loads(line)
        for line in ran.output.splitlines()
        if line.startswith("{") and json.loads(line).get("type") == "session.created"
    ]
    session_id = created[0]["session_id"]

    checkpoints = runner.invoke(
        main.app, ["sessions", "checkpoints", session_id, "--config", str(config)]
    )
    assert checkpoints.exit_code == 0, checkpoints.output
    assert "Checkpoints" in checkpoints.output
    assert "ok" in checkpoints.output

    forked = runner.invoke(
        main.app,
        ["sessions", "fork", session_id, "--at", "1", "--config", str(config)],
    )
    assert forked.exit_code == 0, forked.output
    match = re.search(r"forked\s+(ses_[A-Za-z0-9]+)", forked.output)
    assert match, forked.output
    forked_id = match.group(1)
    shown = runner.invoke(main.app, ["sessions", "show", forked_id, "--config", str(config)])
    assert shown.exit_code == 0, shown.output
    assert "parent" in shown.output
    assert session_id in shown.output

    source = runner.invoke(main.app, ["sessions", "show", session_id, "--config", str(config)])
    assert source.exit_code == 0, source.output
    assert "parent" not in source.output

    recovered = runner.invoke(
        main.app, ["sessions", "recover", session_id, "--config", str(config)]
    )
    assert recovered.exit_code == 0, recovered.output
    assert "no recovery needed" in recovered.output


def _seed_task_plan(tmp_path: Path, config: Path) -> tuple[str, str]:
    import asyncio

    from bear_agent.bootstrap import build_maintenance
    from bear_agent.config.loader import load_settings
    from bear_agent.core.fencing import LeaseFence, reset_fence, set_fence
    from bear_agent.tasks.models import PlanDraft, TaskSpec

    settings = load_settings(config)
    maintenance = build_maintenance(settings, cwd=tmp_path)

    async def _seed() -> tuple[str, str]:
        session = await maintenance.sessions.create(
            cwd=tmp_path, model="fake-model", provider="fake", title="task plan"
        )
        owner = "seed-owner"
        epoch = await maintenance.sessions.acquire_lease(session.id, owner, 60)
        fence = LeaseFence(owner, epoch)
        token = set_fence(fence)
        try:
            await maintenance.tasks.create_plan(
                session.id,
                PlanDraft(
                    goal="implement and verify the feature",
                    tasks=[
                        TaskSpec(
                            key="T1",
                            title="verify implementation",
                            description="run the checks",
                            acceptance_criteria="checks pass",
                            verification="evidence",
                        )
                    ],
                ),
                source="user",
                fence=fence,
            )
            task = (await maintenance.tasks.list_tasks(session.id))[0]
        finally:
            reset_fence(token)
            await maintenance.sessions.release_lease(session.id, owner, epoch)
        return session.id, task.id

    try:
        return asyncio.run(_seed())
    finally:
        maintenance.db.close()


def test_tasks_show_task_and_cancel_commands(tmp_path: Path) -> None:
    runner = CliRunner()
    config = _config(tmp_path)
    session_id, task_id = _seed_task_plan(tmp_path, config)

    shown = runner.invoke(main.app, ["tasks", "show", session_id, "--config", str(config)])
    assert shown.exit_code == 0, shown.output
    assert "implement and verify the feature" in shown.output
    assert "T1" in shown.output
    assert "verify implementation" in shown.output

    detail = runner.invoke(main.app, ["tasks", "task", task_id, "--config", str(config)])
    assert detail.exit_code == 0, detail.output
    assert "verify implementation" in detail.output
    assert "checks pass" in detail.output

    cancelled = runner.invoke(main.app, ["tasks", "cancel", session_id, "--config", str(config)])
    assert cancelled.exit_code == 0, cancelled.output
    assert "plan cancelled" in cancelled.output
    after = runner.invoke(main.app, ["tasks", "show", session_id, "--config", str(config)])
    assert after.exit_code == 0, after.output
    assert "cancelled" in after.output


def test_tasks_plan_generation_and_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    config = _config(tmp_path)
    plan_json = json.dumps(
        {
            "goal": "implement the feature",
            "tasks": [
                {
                    "key": "T1",
                    "title": "implement",
                    "description": "write code",
                    "acceptance_criteria": "tests pass",
                    "dependencies": [],
                    "priority": 1,
                    "verification": "evidence",
                }
            ],
        }
    )
    _patch_scripts(monkeypatch, [text_script(plan_json)])
    planned = runner.invoke(
        main.app, ["tasks", "plan", "implement the feature", "--config", str(config)]
    )
    assert planned.exit_code == 0, planned.output
    assert "created with 1 task" in planned.output
    assert "implement" in planned.output

    # A separate invocation with scripts for the task run.
    _patch_scripts(
        monkeypatch,
        [
            tool_script(
                "t1-complete",
                "task_complete",
                {"summary": "tests pass", "evidence": ["pytest"]},
            ),
            text_script("done"),
        ],
    )
    settings = main._load(
        config=config,
        base_url=None,
        api_key=None,
        provider=None,
        model=None,
        permission=None,
        max_turns=None,
    )
    import asyncio

    from bear_agent.bootstrap import build_maintenance

    async def session_for_plan() -> str:
        maintenance = build_maintenance(settings, cwd=tmp_path)
        try:
            sessions = await maintenance.sessions.list_recent(5)
            return sessions[0].id
        finally:
            await maintenance.close()

    session_id = asyncio.run(session_for_plan())
    ran = runner.invoke(main.app, ["tasks", "run", session_id, "--config", str(config)])
    assert ran.exit_code == 0, ran.output
    assert "completed" in ran.output
    assert "task T1 completed" in ran.output


def test_tasks_run_json_and_unknown_session_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    config = _config(tmp_path)
    missing = runner.invoke(
        main.app,
        ["tasks", "run", "ses_missing", "--config", str(config), "--json"],
    )
    assert missing.exit_code == 2, missing.output
    assert "invalid session" in missing.output

    session_id, _task_id = _seed_task_plan(tmp_path, config)
    _patch_scripts(
        monkeypatch,
        [
            tool_script(
                "t1-complete",
                "task_complete",
                {"summary": "tests pass", "evidence": ["pytest"]},
            ),
            text_script("done"),
        ],
    )
    result = runner.invoke(
        main.app,
        ["tasks", "run", session_id, "--config", str(config), "--json"],
    )
    assert result.exit_code == 0, result.output
    payloads = [json.loads(line) for line in result.output.splitlines() if line.strip()]
    assert any(item.get("type") == "plan_result" for item in payloads)
    plan_result = next(item for item in payloads if item.get("type") == "plan_result")
    assert plan_result["status"] == "completed"
    assert plan_result["tasks_completed"] == 1


def test_providers_show_and_plugins_cli(tmp_path: Path) -> None:
    runner = CliRunner()
    plugin_dir = tmp_path / "my_plugins" / "demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        """
schema_version = 1
id = "demo"
name = "Demo"
version = "1.0.0"
requires_bear = ">=0.4.0,<0.8.0"
description = "test plugin"
enabled_by_default = false

[permissions]
mcp = true

[mcp_servers.calc]
transport = "stdio"
command = "python"
args = ["-m", "demo_server"]
trusted = true
""",
        encoding="utf-8",
    )
    config = _config(tmp_path)
    with config.open("a", encoding="utf-8") as handle:
        handle.write(f'\n[plugins]\nenabled = true\ndirs = ["{plugin_dir.parent.as_posix()}"]\n')

    providers = runner.invoke(main.app, ["providers", "show", "--config", str(config)])
    assert providers.exit_code == 0, providers.output
    assert "fake" in providers.output
    assert "fallback_enabled" in providers.output
    assert "test" not in providers.output  # api key must not leak

    listed = runner.invoke(main.app, ["plugins", "list", "--config", str(config)])
    assert listed.exit_code == 0, listed.output
    assert "demo" in listed.output
    assert "no" in listed.output  # disabled by default

    validated = runner.invoke(main.app, ["plugins", "validate", "--config", str(config)])
    assert validated.exit_code == 0, validated.output
    assert "ok" in validated.output

    enabled = runner.invoke(main.app, ["plugins", "enable", "demo", "--config", str(config)])
    assert enabled.exit_code == 0, enabled.output
    listed_again = runner.invoke(main.app, ["plugins", "list", "--config", str(config)])
    assert listed_again.exit_code == 0, listed_again.output


def test_mcp_cli_with_real_stdio_server(tmp_path: Path) -> None:
    import sys

    server_script = Path(__file__).resolve().parents[1] / "support" / "mcp_server.py"
    config = _config(tmp_path)
    with config.open("a", encoding="utf-8") as handle:
        handle.write(
            "\n[mcp.servers.calc]\n"
            'transport = "stdio"\n'
            f'command = "{sys.executable.replace(chr(92), "/")}"\n'
            f'args = ["{str(server_script).replace(chr(92), "/")}"]\n'
            "trusted = true\n"
            "required = true\n"
            "timeout_s = 20\n"
            "startup_timeout_s = 20\n"
        )
    runner = CliRunner()
    tools = runner.invoke(main.app, ["mcp", "tools", "--config", str(config)])
    assert tools.exit_code == 0, tools.output
    assert "mcp__calc__echo" in tools.output
    status = runner.invoke(main.app, ["mcp", "status", "--config", str(config)])
    assert status.exit_code == 0, status.output
    assert "ready" in status.output


def test_mcp_and_hooks_cli_without_servers(tmp_path: Path) -> None:
    runner = CliRunner()
    config = _config(tmp_path)
    mcp = runner.invoke(main.app, ["mcp", "status", "--config", str(config)])
    assert mcp.exit_code == 0, mcp.output
    assert "MCP servers" in mcp.output
    hooks = runner.invoke(main.app, ["hooks", "list", "--config", str(config)])
    assert hooks.exit_code == 0, hooks.output
    assert "Hooks" in hooks.output


def test_secret_commands_with_in_memory_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, local=True)
    monkeypatch.setattr(
        main,
        "secret_store_from_config",
        lambda _backend: InMemorySecretStore(),
    )
    runner = CliRunner()
    stored = runner.invoke(
        main.app,
        ["secret", "set", "deepseek", "--config", str(config)],
        input="secret-value\nsecret-value\n",
    )
    assert stored.exit_code == 0, stored.output
    listed = runner.invoke(main.app, ["secret", "list", "--config", str(config)])
    assert listed.exit_code == 0, listed.output
    assert "deepseek" in listed.output
    deleted = runner.invoke(main.app, ["secret", "delete", "deepseek", "--config", str(config)])
    assert deleted.exit_code == 0, deleted.output


def test_config_show_json_does_not_leak_shell_env_or_headers(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    data_dir = (tmp_path / "data").as_posix()
    config.write_text(
        f"""
[agent]
default_provider = "fake"
default_model = "fake-model"

[providers.fake]
type = "openai_compat"
base_url = "http://127.0.0.1:11434/v1"
api_key = "test"
headers = {{ Authorization = "Bearer leaky-token" }}

[shell]
env = {{ MY_SECRET = "leaky-secret" }}

[mcp.servers.leaky]
transport = "stdio"
command = "python"
env = {{ MCP_TOKEN = "mcp-leaky-secret" }}
trusted = true

[storage]
data_dir = "{data_dir}"
""",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(main.app, ["config", "show", "--json", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "leaky-secret" not in result.output
    assert "mcp-leaky-secret" not in result.output
    assert "leaky-token" not in result.output
    plain = runner.invoke(main.app, ["config", "show", "--config", str(config)])
    assert plain.exit_code == 0, plain.output
    assert "leaky-secret" not in plain.output
    assert "leaky-token" not in plain.output


def test_root_config_applies_to_subcommand(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    text = config.read_text(encoding="utf-8").replace(
        'default_model = "fake-model"', 'default_model = "root-model"'
    )
    config.write_text(text, encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(main.app, ["--config", str(config), "config", "show", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["agent"]["default_model"] == "root-model"


def _config_without_key(tmp_path: Path) -> Path:
    config = _config(tmp_path)
    text = config.read_text(encoding="utf-8")
    text = text.replace('api_key = "test"\n', "")
    config.write_text(text, encoding="utf-8")
    return config


def test_run_json_preflight_error_is_not_on_stdout(tmp_path: Path) -> None:
    config = _config_without_key(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main.app, ["run", "hello", "--config", str(config), "--json"])
    assert result.exit_code == 2
    assert result.stdout.strip() == ""
    assert "no API key" in result.stderr


def test_providers_show_json_error_is_structured(tmp_path: Path) -> None:
    config = _config_without_key(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main.app, ["providers", "show", "--config", str(config), "--json"])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["type"] == "error"
    assert "token" not in json.dumps(data).lower()
