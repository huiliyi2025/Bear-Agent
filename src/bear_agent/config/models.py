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

    def resolve_api_key(self, secret_store: object | None = None) -> str:
        if self.api_key:
            from bear_agent.secrets.store import resolve_secret_reference

            if self.api_key.startswith(("secret://", "keyring://")) and secret_store is None:
                return ""
            return resolve_secret_reference(self.api_key, secret_store)  # type: ignore[arg-type]
        if self.api_key_env:
            return os.environ.get(self.api_key_env, "")
        return ""

    @property
    def has_secret_reference(self) -> bool:
        return bool(self.api_key and self.api_key.startswith(("secret://", "keyring://")))

    @property
    def is_local(self) -> bool:
        host = self.base_url.lower()
        return any(
            marker in host for marker in ("localhost", "127.0.0.1", "0.0.0.0", "ollama", "::1")
        )


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


class ShellConfig(BaseModel):
    enabled: bool = True
    # Empty = auto-detect /bin/bash then /bin/sh on POSIX.
    posix_shell: str = ""
    windows_shell: Literal["powershell", "cmd"] = "powershell"
    default_timeout_s: float = 120.0
    max_timeout_s: float = 600.0
    max_output_bytes: int = 200_000
    env_allowlist: list[str] = Field(
        default_factory=lambda: [
            "PATH",
            "HOME",
            "USER",
            "USERNAME",
            "USERPROFILE",
            "HOMEDRIVE",
            "HOMEPATH",
            "LANG",
            "LC_ALL",
            "TERM",
            "TMPDIR",
            "TEMP",
            "TMP",
            "SYSTEMROOT",
            "COMSPEC",
            "PATHEXT",
            "WINDIR",
        ]
    )
    env: dict[str, str] = Field(default_factory=dict)
    allow_commands: list[str] = Field(default_factory=list)
    allow_prefixes: list[str] = Field(default_factory=list)
    deny_patterns: list[str] = Field(default_factory=list)

    @field_validator("default_timeout_s", "max_timeout_s", "max_output_bytes")
    @classmethod
    def _positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be positive")
        return value


class SandboxConfig(BaseModel):
    # require: filesystem-capable sandbox or fail closed.
    # process: process/resource containment is acceptable (Windows Job Object),
    #          but the result is explicitly marked as lacking filesystem isolation.
    # off:     explicit opt-out; tools report sandboxed=false.
    mode: Literal["require", "process", "off"] = "require"
    network: bool = False
    read_system: bool = True
    writable_roots: list[str] = Field(default_factory=list)


class SecretsConfig(BaseModel):
    backend: Literal["keyring", "none"] = "keyring"


class AuditConfig(BaseModel):
    enabled: bool = True


class ClaimsConfig(BaseModel):
    enabled: bool = True
    ttl_s: float = 600.0


class ApprovalConfig(BaseModel):
    session_ttl_s: float = 86_400.0
    project_ttl_s: float = 2_592_000.0


class ContextConfig(BaseModel):
    """Layered context compaction policy.

    L1 prunes old tool output into artifacts, L2 builds a deterministic
    summary, L3 asks the model for a summary and L4 resets the working context
    at a checkpoint. Each level only runs when the previous one did not bring
    the estimated request below the trigger threshold.
    """

    compaction_enabled: bool = True
    trigger_ratio: float = 0.82
    keep_recent_turns: int = 2
    l1_enabled: bool = True
    l1_min_tool_chars: int = 4_000
    l1_keep_chars: int = 1_000
    l2_enabled: bool = True
    l2_max_summary_tokens: int = 1_500
    l3_enabled: bool = True
    l3_max_input_tokens: int = 24_000
    l3_failure_limit: int = 3
    l3_cooldown_turns: int = 5
    l4_enabled: bool = True
    anchors_enabled: bool = True
    anchor_max_tokens: int = 800
    summary_model: str = ""

    @field_validator("trigger_ratio")
    @classmethod
    def _ratio(cls, value: float) -> float:
        if not 0.0 < value <= 1.0:
            raise ValueError("must be in (0, 1]")
        return value

    @field_validator(
        "keep_recent_turns",
        "l1_min_tool_chars",
        "l1_keep_chars",
        "l2_max_summary_tokens",
        "l3_max_input_tokens",
        "l3_failure_limit",
        "l3_cooldown_turns",
        "anchor_max_tokens",
    )
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must not be negative")
        return value


class CheckpointConfig(BaseModel):
    enabled: bool = True
    # 0 disables automatic per-turn checkpoints; manual and pre-compaction
    # checkpoints are still created.
    interval_turns: int = 1
    keep: int = 20

    @field_validator("interval_turns", "keep")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must not be negative")
        return value


class RecoveryConfig(BaseModel):
    enabled: bool = True
    # Kill shell processes registered by a run whose lease was lost. The
    # process registry verifies identity before signalling.
    kill_orphan_processes: bool = True
    # Recovery actions are security-relevant; refuse to proceed if the audit
    # chain cannot be appended.
    require_audit: bool = True


class Settings(BaseModel):
    providers: dict[str, ProviderConfig] = Field(
        default_factory=lambda: {"default": ProviderConfig()}
    )
    agent: AgentConfig = Field(default_factory=AgentConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    shell: ShellConfig = Field(default_factory=ShellConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    claims: ClaimsConfig = Field(default_factory=ClaimsConfig)
    approvals: ApprovalConfig = Field(default_factory=ApprovalConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    checkpoints: CheckpointConfig = Field(default_factory=CheckpointConfig)
    recovery: RecoveryConfig = Field(default_factory=RecoveryConfig)

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

    def validate_ready(
        self,
        provider_name: str | None = None,
        secret_store: object | None = None,
    ) -> None:
        """Validate that the selected provider can actually be used.

        ``secret://`` references are allowed here even before the secret store
        is available; provider construction resolves them and fails closed.
        """
        provider = self.provider(provider_name)
        if (
            not provider.is_local
            and not provider.resolve_api_key(secret_store)
            and not provider.has_secret_reference
        ):
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
