from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.support import ScriptedProvider, text_script
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
    def fake_build(settings, *, cwd, approver=None, provider=None, bus=None):
        scripted = ScriptedProvider([text_script(text)])
        return real_build_container(
            settings, cwd=cwd, approver=approver, provider=scripted, bus=bus
        )

    monkeypatch.setattr(main, "build_container", fake_build)


def test_help_and_version() -> None:
    runner = CliRunner()
    result = runner.invoke(main.app, ["--help"])
    assert result.exit_code == 0
    assert "run" in result.output
    result = runner.invoke(main.app, ["--version"])
    assert result.exit_code == 0
    assert "bear-agent" in result.output


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

[storage]
data_dir = "{data_dir}"
""",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(main.app, ["config", "show", "--json", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "leaky-secret" not in result.output
    assert "leaky-token" not in result.output
    plain = runner.invoke(main.app, ["config", "show", "--config", str(config)])
    assert plain.exit_code == 0, plain.output
    assert "leaky-secret" not in plain.output
    assert "leaky-token" not in plain.output
