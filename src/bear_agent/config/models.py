"""Configuration models.

Precedence (highest first):
1. explicit CLI overrides
2. ``BEAR_*`` environment variables
3. the TOML config file
4. built-in defaults

Secrets are never required in the file: ``api_key_env`` names an environment
variable and ``api_key`` may contain a ``keyring://`` or ``env:`` reference in a
later phase. For now plain values and environment variables are supported, and
the resolved key is never logged.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from bear_agent.core.modes import PermissionMode


class ProviderConfig(BaseModel):
    type: Literal["openai_compat"] = "openai_compat"
    base_url: str = "https://api.openai.com/v1"
    api_key: str | None = None
    api_key_env: str | None = None
    models: list[str] = Field(default_factory=list)
    timeout_s: float = 120.0
    max_retries: int = 2
    # Ask OpenAI-compatible endpoints to include usage on the final stream chunk.
    # Disabled by default because some relays reject unknown stream_options.
    include_usage: bool = False
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def _strip_base_url(cls, value: str) -> str:
        return value.rstrip("/")

    def resolve_api_key(self) -> str:
        if self.api_key:
            return _resolve_secret_reference(self.api_key)
        if self.api_key_env:
            return os.environ.get(self.api_key_env, "")
        return ""

    @property
    def is_local(self) -> bool:
        host = self.base_url.lower()
        return any(
            marker in host for marker in ("localhost", "127.0.0.1", "0.0.0.0", "ollama", "::1")
        )


def _resolve_secret_reference(value: str) -> str:
    """Resolve ``env:NAME`` references; plain values pass through.

    ``keyring://`` is intentionally rejected for now rather than silently treated
    as a literal key. Phase 2 adds the keyring-backed store.
    """
    if value.startswith("env:"):
        return os.environ.get(value[4:], "")
    if value.startswith("keyring://"):
        raise ValueError(
            "keyring:// secrets are not supported yet; use api_key_env or a plain value for now"
        )
    return value


class AgentConfig(BaseModel):
    default_provider: str = "default"
    default_model: str = "gpt-4.1-mini"
    max_turns: int = 30
    max_tool_calls: int = 200
    temperature: float = 0.0
    max_output_tokens: int = 4096
    context_window: int = 128_000
    permission_mode: PermissionMode = PermissionMode.AUTO
    stream: bool = True
    system_prompt_extra: str = ""
    # Hard wall-clock limit for one run; 0 disables the limit.
    max_run_seconds: float = 0.0

    @field_validator("max_turns", "max_tool_calls")
    @classmethod
    def _positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be positive")
        return value

    @field_validator("max_output_tokens", "context_window")
    @classmethod
    def _positive_tokens(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be positive")
        return value


class StorageConfig(BaseModel):
    data_dir: Path | None = None


class Settings(BaseModel):
    providers: dict[str, ProviderConfig] = Field(
        default_factory=lambda: {"default": ProviderConfig()}
    )
    agent: AgentConfig = Field(default_factory=AgentConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)

    def provider(self, name: str | None = None) -> ProviderConfig:
        provider_name = name or self.agent.default_provider
        try:
            return self.providers[provider_name]
        except KeyError as exc:
            known = ", ".join(sorted(self.providers)) or "(none)"
            raise KeyError(f"unknown provider {provider_name!r}; configured: {known}") from exc

    def provider_names(self) -> list[str]:
        return sorted(self.providers)

    def resolved_data_dir(self) -> Path:
        return self.storage.data_dir or default_data_dir()

    def ensure_data_dirs(self) -> Path:
        data_dir = self.resolved_data_dir()
        for sub in (data_dir, data_dir / "artifacts", data_dir / "logs", data_dir / "backups"):
            sub.mkdir(parents=True, exist_ok=True)
        return data_dir

    def validate_ready(self, provider_name: str | None = None) -> None:
        """Validate that the selected provider can actually be used."""
        provider = self.provider(provider_name)
        if not provider.is_local and not provider.resolve_api_key():
            raise ValueError(
                f"provider {provider_name or self.agent.default_provider!r} has no API key. "
                f"Set {provider.api_key_env or 'BEAR_API_KEY'}, add api_key to the config, "
                "or point base_url at a local endpoint."
            )
        if not self.agent.default_model:
            raise ValueError("agent.default_model must not be empty")


def default_data_dir() -> Path:
    from platformdirs import user_data_path

    return Path(user_data_path("bear-agent", appauthor=False))
