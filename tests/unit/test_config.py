from __future__ import annotations

from pathlib import Path

import pytest

from bear_agent.config.loader import (
    load_settings,
    settings_to_toml,
    write_default_config,
)
from bear_agent.core.errors import ConfigError

TOML = """\
[agent]
default_provider = "local"
default_model = "local-model"
max_turns = 7
permission_mode = "supervised"

[providers.local]
type = "openai_compat"
base_url = "http://127.0.0.1:11434/v1"
api_key = "secret-value"
models = ["local-model"]
"""


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "BEAR_CONFIG",
        "BEAR_PROVIDER",
        "BEAR_MODEL",
        "BEAR_BASE_URL",
        "BEAR_API_KEY",
        "BEAR_DATA_DIR",
        "BEAR_MAX_TURNS",
        "BEAR_MAX_TOOL_CALLS",
        "BEAR_TEMPERATURE",
        "BEAR_MAX_OUTPUT_TOKENS",
        "BEAR_CONTEXT_WINDOW",
        "BEAR_PERMISSION_MODE",
        "BEAR_SYSTEM_PROMPT_EXTRA",
    ):
        monkeypatch.delenv(name, raising=False)


def test_load_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    path = tmp_path / "config.toml"
    path.write_text(TOML, encoding="utf-8")
    settings = load_settings(path)
    assert settings.agent.default_provider == "local"
    assert settings.agent.default_model == "local-model"
    assert settings.agent.max_turns == 7
    assert settings.agent.permission_mode.value == "supervised"
    assert settings.provider().base_url == "http://127.0.0.1:11434/v1"
    assert settings.provider().resolve_api_key() == "secret-value"


def test_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    path = tmp_path / "config.toml"
    path.write_text(TOML, encoding="utf-8")
    monkeypatch.setenv("BEAR_MODEL", "override-model")
    monkeypatch.setenv("BEAR_MAX_TURNS", "3")
    settings = load_settings(path)
    assert settings.agent.default_model == "override-model"
    assert settings.agent.max_turns == 3


def test_missing_provider_is_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    path = tmp_path / "config.toml"
    path.write_text(
        '[agent]\ndefault_provider = "missing"\ndefault_model = "x"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="default_provider"):
        load_settings(path)


def test_default_provider_when_no_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEAR_API_KEY", "from-env")
    settings = load_settings(tmp_path / "missing.toml")
    assert settings.agent.default_provider == "default"
    assert settings.provider().resolve_api_key() == "from-env"


def test_write_default_config(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "config.toml"
    written = write_default_config(path)
    assert written == path
    assert path.exists()
    with pytest.raises(FileExistsError):
        write_default_config(path)
    write_default_config(path, force=True)


def test_settings_to_toml_omits_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    path = tmp_path / "config.toml"
    path.write_text(TOML, encoding="utf-8")
    rendered = settings_to_toml(load_settings(path))
    assert "secret-value" not in rendered
    assert "default_provider" in rendered


def test_compaction_and_recovery_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    path = tmp_path / "config.toml"
    path.write_text(
        TOML
        + """
[context]
trigger_ratio = 0.7
keep_recent_turns = 3
l3_enabled = false
anchor_max_tokens = 400

[checkpoints]
interval_turns = 0
keep = 5

[recovery]
kill_orphan_processes = false
require_audit = false
""",
        encoding="utf-8",
    )
    settings = load_settings(path)
    assert settings.context.trigger_ratio == 0.7
    assert settings.context.keep_recent_turns == 3
    assert settings.context.l3_enabled is False
    assert settings.context.anchor_max_tokens == 400
    assert settings.checkpoints.interval_turns == 0
    assert settings.checkpoints.keep == 5
    assert settings.recovery.kill_orphan_processes is False
    assert settings.recovery.require_audit is False
    rendered = settings_to_toml(settings)
    assert "[context]" in rendered
    assert "[checkpoints]" in rendered
    assert "[recovery]" in rendered


def test_invalid_trigger_ratio_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    path = tmp_path / "config.toml"
    path.write_text(TOML + "\n[context]\ntrigger_ratio = 1.5\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="trigger_ratio"):
        load_settings(path)


def test_default_template_parses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    path = tmp_path / "config.toml"
    write_default_config(path)
    settings = load_settings(path)
    assert settings.context.compaction_enabled is True
    assert settings.checkpoints.keep == 20
    assert settings.recovery.require_audit is True
