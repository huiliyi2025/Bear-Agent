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
        "BEAR_FALLBACK_MODELS",
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
    empty = tmp_path / "empty.toml"
    empty.write_text("", encoding="utf-8")
    settings = load_settings(empty)
    assert settings.agent.default_provider == "default"
    assert settings.provider().resolve_api_key() == "from-env"


def test_explicit_missing_config_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    with pytest.raises(ConfigError, match="does not exist"):
        load_settings(tmp_path / "missing.toml")


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

[tasks]
max_tasks = 12
max_attempts = 2
max_revisions = 4
verify_completion = false
max_task_turns = 5
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
    assert settings.tasks.max_tasks == 12
    assert settings.tasks.max_attempts == 2
    assert settings.tasks.max_revisions == 4
    assert settings.tasks.verify_completion is False
    assert settings.tasks.max_task_turns == 5
    rendered = settings_to_toml(settings)
    assert "[context]" in rendered
    assert "[checkpoints]" in rendered
    assert "[recovery]" in rendered
    assert "[tasks]" in rendered


def test_hooks_mcp_and_plugins_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    path = tmp_path / "config.toml"
    path.write_text(
        TOML
        + """
[hooks]
enabled = true
allow_external = true

[[hooks.commands]]
id = "audit-tool"
event = "before_tool_call"
command = "python"
args = ["hook.py"]
fail_mode = "closed"

[mcp]
enabled = true

[mcp.servers.echo]
transport = "stdio"
command = "python"
args = ["-m", "echo_server"]
trusted = true

[plugins]
enabled = true
allow_python = false
""",
        encoding="utf-8",
    )
    settings = load_settings(path)
    assert settings.hooks.allow_external is True
    assert settings.hooks.commands[0].id == "audit-tool"
    assert settings.mcp.servers["echo"].trusted is True
    assert settings.plugins.allow_python is False
    rendered = settings_to_toml(settings)
    assert "[hooks]" in rendered
    assert "[mcp]" in rendered
    assert "[plugins]" in rendered


def test_observability_and_benchmark_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    path = tmp_path / "config.toml"
    path.write_text(
        TOML
        + """
[observability]
enabled = true
traces_enabled = true
metrics_enabled = true
trace_sample_ratio = 0.5
trace_retention_days = 7
trace_max_traces = 1000
incomplete_trace_grace_s = 60

[observability.pricing."local:local-model"]
input_per_million = 1.5
output_per_million = 3.0
cache_read_per_million = 0.1
currency = "usd"

[observability.pricing.local-model]
input_per_million = 2.0
output_per_million = 9.0
currency = "usd"

[benchmarks]
default_iterations = 3
default_warmup = 1
max_iterations = 20
compare_threshold_pct = 15
""",
        encoding="utf-8",
    )
    settings = load_settings(path)
    assert settings.config_version == 1
    assert settings.observability.trace_sample_ratio == 0.5
    assert settings.observability.trace_retention_days == 7
    price = settings.observability.price_for("local", "local-model")
    assert price is not None
    assert price.currency == "USD"
    assert price.input_per_million == 1.5
    model_price = settings.observability.price_for("other", "local-model")
    assert model_price is not None
    assert model_price.output_per_million == 9.0
    assert settings.observability.price_for("local", "unknown") is None
    assert settings.benchmarks.default_iterations == 3
    assert settings.benchmarks.compare_threshold_pct == 15
    rendered = settings_to_toml(settings)
    assert "[observability]" in rendered
    assert '[observability.pricing."local:local-model"]' in rendered
    assert "[benchmarks]" in rendered
    assert "secret-value" not in rendered


def test_invalid_observability_values_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    path = tmp_path / "config.toml"
    path.write_text(TOML + "\n[observability]\ntrace_sample_ratio = 2.0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="trace_sample_ratio"):
        load_settings(path)


def test_untrusted_mcp_server_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    path = tmp_path / "config.toml"
    path.write_text(
        TOML
        + """
[mcp.servers.echo]
transport = "stdio"
command = "python"
trusted = false
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="not trusted"):
        load_settings(path)


def test_unknown_fallback_provider_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    path = tmp_path / "config.toml"
    path.write_text(
        TOML.replace(
            "max_turns = 7",
            'max_turns = 7\nfallback_models = ["missing:model"]',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unknown provider"):
        load_settings(path)


def test_mcp_loopback_http_rejects_lookalike_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    path = tmp_path / "config.toml"
    path.write_text(
        TOML
        + """
[mcp.servers.lookalike]
transport = "streamable_http"
url = "http://127.0.0.1.attacker.example/mcp"
trusted = true
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="must use https"):
        load_settings(path)


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
    assert settings.tasks.enabled is True
    assert settings.tasks.max_attempts == 3
