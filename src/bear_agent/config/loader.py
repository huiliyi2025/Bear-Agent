"""TOML config loading, environment overrides and first-run scaffolding."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from platformdirs import user_config_path

from bear_agent.config.models import AgentConfig, ProviderConfig, Settings, StorageConfig
from bear_agent.core.errors import ConfigError


def default_config_path() -> Path:
    return Path(user_config_path("bear-agent", appauthor=False)) / "config.toml"


DEFAULT_CONFIG_TEMPLATE = """\
# Bear-Agent configuration
#
# Prefer environment variables for secrets:
#   export DEEPSEEK_API_KEY=...
# `bear` reads BEAR_* overrides on top of this file.

[agent]
default_provider = "deepseek"
default_model = "deepseek-chat"
max_turns = 30
max_tool_calls = 200
temperature = 0.0
max_output_tokens = 4096
context_window = 128000
permission_mode = "auto"   # plan | supervised | auto | unrestricted

[providers.deepseek]
type = "openai_compat"
base_url = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"
models = ["deepseek-chat", "deepseek-reasoner"]

[shell]
enabled = true
default_timeout_s = 120
max_timeout_s = 600
max_output_bytes = 200000
allow_commands = []       # exact executable names that skip approval in auto mode
allow_prefixes = []       # command prefixes that skip approval in auto mode
deny_patterns = []        # extra hard-deny regexes (always win)

[sandbox]
# require: filesystem-capable OS sandbox or fail closed
# process: process-only containment (Windows Job Object) is accepted and reported
# off:     explicit opt-out; shell runs without OS isolation
mode = "require"
network = false
read_system = true

[secrets]
backend = "keyring"

[audit]
enabled = true

[claims]
enabled = true
ttl_s = 600

[approvals]
session_ttl_s = 86400
project_ttl_s = 2592000

# Any OpenAI-compatible endpoint works, including Ollama / vLLM / a relay:
# [providers.local]
# type = "openai_compat"
# base_url = "http://127.0.0.1:11434/v1"
# models = ["qwen2.5-coder:7b"]
"""


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config root must be a table: {path}")
    return data


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else None


def load_settings(
    config_path: Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    use_env: bool = True,
) -> Settings:
    """Load settings from TOML + environment + explicit overrides.

    ``overrides`` is a nested dict shaped like the config file, e.g.
    ``{"agent": {"default_model": "x"}, "providers": {"default": {...}}}``.
    CLI flags are folded into this dict by the caller.
    """
    path = config_path or Path(_env("BEAR_CONFIG") or default_config_path())
    raw = _read_toml(path)
    if not raw.get("providers"):
        raw["providers"] = {
            "default": {
                "type": "openai_compat",
                "base_url": _env("BEAR_BASE_URL") or "https://api.openai.com/v1",
                "api_key": _env("BEAR_API_KEY"),
                "api_key_env": None if _env("BEAR_API_KEY") else "OPENAI_API_KEY",
                "models": [],
            }
        }
    if use_env:
        _apply_env_overrides(raw)
    _deep_merge(raw, overrides or {})

    try:
        settings = Settings.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError, ValueError from key refs
        raise ConfigError(f"invalid configuration in {path}: {exc}") from exc

    if settings.agent.default_provider not in settings.providers:
        raise ConfigError(
            f"agent.default_provider {settings.agent.default_provider!r} is not defined "
            f"in [providers]; configured: {', '.join(settings.provider_names())}"
        )
    return settings


def _apply_env_overrides(raw: dict[str, Any]) -> None:
    agent = raw.setdefault("agent", {})
    env_map = {
        "BEAR_PROVIDER": "default_provider",
        "BEAR_MODEL": "default_model",
        "BEAR_MAX_TURNS": "max_turns",
        "BEAR_MAX_TOOL_CALLS": "max_tool_calls",
        "BEAR_TEMPERATURE": "temperature",
        "BEAR_MAX_OUTPUT_TOKENS": "max_output_tokens",
        "BEAR_CONTEXT_WINDOW": "context_window",
        "BEAR_PERMISSION_MODE": "permission_mode",
    }
    for env_name, key in env_map.items():
        value = _env(env_name)
        if value is None:
            continue
        agent[key] = _coerce(value)

    if _env("BEAR_DATA_DIR"):
        raw.setdefault("storage", {})["data_dir"] = _env("BEAR_DATA_DIR")

    provider_name = agent.get("default_provider", "default")
    providers = raw.setdefault("providers", {})
    base_url = _env("BEAR_BASE_URL")
    api_key = _env("BEAR_API_KEY")
    if base_url or api_key:
        provider = providers.setdefault(provider_name, {})
        if base_url:
            provider["base_url"] = base_url
        if api_key:
            provider["api_key"] = api_key

    if _env("BEAR_SYSTEM_PROMPT_EXTRA"):
        agent["system_prompt_extra"] = _env("BEAR_SYSTEM_PROMPT_EXTRA")


def _coerce(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _deep_merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def write_default_config(path: Path | None = None, *, force: bool = False) -> Path:
    target = path or default_config_path()
    if target.exists() and not force:
        raise FileExistsError(str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    return target


def settings_to_toml(settings: Settings) -> str:
    """Small serializer used by ``bear config show``.

    Values are emitted as TOML scalars/arrays; nested provider tables are kept
    explicit so the output can be pasted into a config file. Secrets are never
    serialized.
    """
    lines: list[str] = ["[agent]"]
    agent = settings.agent
    for key in (
        "default_provider",
        "default_model",
        "max_turns",
        "max_tool_calls",
        "temperature",
        "max_output_tokens",
        "context_window",
        "permission_mode",
        "stream",
        "max_run_seconds",
    ):
        value = getattr(agent, key)
        lines.append(f"{key} = {_toml_value(value)}")
    lines.extend(["", "[shell]"])
    shell = settings.shell
    for key in (
        "enabled",
        "posix_shell",
        "windows_shell",
        "default_timeout_s",
        "max_timeout_s",
        "max_output_bytes",
        "env_allowlist",
        "allow_commands",
        "allow_prefixes",
        "deny_patterns",
    ):
        lines.append(f"{key} = {_toml_value(getattr(shell, key))}")
    lines.extend(["", "[sandbox]"])
    sandbox = settings.sandbox
    for key in ("mode", "network", "read_system", "writable_roots"):
        lines.append(f"{key} = {_toml_value(getattr(sandbox, key))}")
    lines.extend(["", "[secrets]"])
    lines.append(f"backend = {_toml_value(settings.secrets.backend)}")
    lines.extend(["", "[audit]"])
    lines.append(f"enabled = {_toml_value(settings.audit.enabled)}")
    lines.extend(["", "[claims]"])
    lines.append(f"enabled = {_toml_value(settings.claims.enabled)}")
    lines.append(f"ttl_s = {_toml_value(settings.claims.ttl_s)}")
    lines.extend(["", "[approvals]"])
    lines.append(f"session_ttl_s = {_toml_value(settings.approvals.session_ttl_s)}")
    lines.append(f"project_ttl_s = {_toml_value(settings.approvals.project_ttl_s)}")
    for name, provider in settings.providers.items():
        lines.append("")
        lines.append(f"[providers.{name}]")
        lines.append(f"type = {_toml_value(provider.type)}")
        lines.append(f"base_url = {_toml_value(provider.base_url)}")
        if provider.api_key_env:
            lines.append(f"api_key_env = {_toml_value(provider.api_key_env)}")
        if provider.models:
            lines.append(f"models = {_toml_value(provider.models)}")
        lines.append(f"timeout_s = {_toml_value(provider.timeout_s)}")
        lines.append(f"max_retries = {_toml_value(provider.max_retries)}")
    return "\n".join(lines) + "\n"


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


# Keep imports used by tests and external callers explicit.
__all__ = [
    "DEFAULT_CONFIG_TEMPLATE",
    "AgentConfig",
    "ProviderConfig",
    "Settings",
    "StorageConfig",
    "default_config_path",
    "load_settings",
    "settings_to_toml",
    "write_default_config",
]
