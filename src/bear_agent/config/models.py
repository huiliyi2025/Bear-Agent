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

import ipaddress
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from bear_agent.core.modes import PermissionMode


class ProviderConfig(BaseModel):
    type: Literal["openai_compat"] = "openai_compat"
    base_url: str = "https://api.openai.com/v1"
    api_key: str | None = None
    api_key_env: str | None = None
    models: list[str] = Field(default_factory=list)
    timeout_s: float = 120.0
    max_retries: int = 2
    retry_base_delay_s: float = 0.5
    retry_max_delay_s: float = 8.0
    retry_jitter: float = 0.2
    # Ask OpenAI-compatible endpoints to include usage on the final stream chunk.
    # Disabled by default because some relays reject unknown stream_options.
    include_usage: bool = False
    headers: dict[str, str] = Field(default_factory=dict)
    # Per-provider capability hints used by provider fallback. None means use
    # the global agent settings.
    context_window: int | None = None
    supports_tools: bool = True
    supports_streaming: bool = True

    @field_validator("base_url")
    @classmethod
    def _strip_base_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("retry_base_delay_s", "retry_max_delay_s", "retry_jitter")
    @classmethod
    def _retry_values(cls, value: float) -> float:
        if value < 0:
            raise ValueError("retry delays/jitter must not be negative")
        return value

    @field_validator("retry_jitter")
    @classmethod
    def _jitter_bounds(cls, value: float) -> float:
        if value > 1:
            raise ValueError("retry_jitter must be between 0 and 1")
        return value

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
    # Fallback model targets in "provider:model" or "provider" form.
    fallback_models: list[str] = Field(default_factory=list)
    provider_fallback_enabled: bool = True
    circuit_breaker_failures: int = 3
    circuit_breaker_cooldown_s: float = 60.0

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


class TasksConfig(BaseModel):
    """Task / Plan / Todo engine policy.

    Model planning and completion verification are bounded: the program owns
    dependency checks, state transitions, retry limits and revision limits.
    """

    enabled: bool = True
    max_tasks: int = 50
    max_attempts: int = 3
    max_revisions: int = 10
    max_tasks_per_run: int = 50
    # 0 means use agent.max_turns for each task run.
    max_task_turns: int = 20
    verify_completion: bool = True
    planner_model: str = ""
    verification_model: str = ""
    planner_max_retries: int = 2
    planner_max_output_tokens: int = 4096
    context_max_tokens: int = 1_200

    @field_validator(
        "max_tasks",
        "max_attempts",
        "max_tasks_per_run",
        "max_task_turns",
        "planner_max_retries",
        "planner_max_output_tokens",
        "context_max_tokens",
    )
    @classmethod
    def _positive(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must not be negative")
        return value

    @field_validator("max_revisions")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must not be negative")
        return value


class HookCommandConfig(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    event: str = Field(min_length=1, max_length=100)
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    timeout_s: float = 5.0
    fail_mode: Literal["open", "closed"] = "open"
    enabled: bool = True

    @field_validator("timeout_s")
    @classmethod
    def _positive_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be positive")
        return value


class HooksConfig(BaseModel):
    enabled: bool = True
    timeout_s: float = 5.0
    max_depth: int = 8
    max_context_chars: int = 4_000
    # External hook commands run as OS subprocesses with the user's privileges;
    # they are opt-in and never loaded from model output.
    allow_external: bool = False
    commands: list[HookCommandConfig] = Field(default_factory=list)


class MCPServerConfig(BaseModel):
    transport: Literal["stdio", "streamable_http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    # Explicit trust decision required for every enabled server.
    trusted: bool = False
    required: bool = True
    timeout_s: float = 60.0
    startup_timeout_s: float = 20.0
    allowed_tools: list[str] = Field(default_factory=list)
    denied_tools: list[str] = Field(default_factory=list)
    verify_tls: bool = True
    allow_insecure_http: bool = False


class MCPConfig(BaseModel):
    enabled: bool = True
    servers: dict[str, MCPServerConfig] = Field(default_factory=dict)

    @field_validator("servers")
    @classmethod
    def _validate_servers(cls, value: dict[str, MCPServerConfig]) -> dict[str, MCPServerConfig]:
        if len(value) > 50:
            raise ValueError("too many MCP servers (max 50)")
        for name, server in value.items():
            if (
                not name
                or len(name) > 64
                or not all(char.isalnum() or char in "_-" for char in name)
            ):
                raise ValueError(f"invalid MCP server name: {name!r}")
            if server.enabled and not server.trusted:
                raise ValueError(
                    f"MCP server {name!r} is enabled but not trusted; set trusted=true "
                    "only for servers you actually trust"
                )
            if server.transport == "stdio" and not server.command:
                raise ValueError(f"MCP server {name!r} requires a command")
            if server.transport == "streamable_http":
                if not server.url:
                    raise ValueError(f"MCP server {name!r} requires a url")
                try:
                    parts = urlsplit(server.url)
                    host = (parts.hostname or "").lower()
                    _ = parts.port
                except ValueError as exc:
                    raise ValueError(f"MCP server {name!r} has an invalid url") from exc
                try:
                    loopback = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    loopback = host == "localhost"
                if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
                    raise ValueError(
                        f"MCP server {name!r} must use https (http is allowed only "
                        "for loopback development servers)"
                    )
                if parts.username is not None or parts.password is not None:
                    raise ValueError(f"MCP server {name!r} url must not embed credentials")
        return value


class PluginsConfig(BaseModel):
    enabled: bool = True
    dirs: list[Path] = Field(default_factory=list)
    # Loading arbitrary in-process Python is disabled by default. Manifest-only
    # plugins (MCP servers / external hooks) do not need this.
    allow_python: bool = False
    max_plugins: int = 50


class ModelPrice(BaseModel):
    """Per-model price used for *estimated* cost reporting.

    Prices are deliberately configuration, not code: vendors change them and a
    local model often has no meaningful monetary price. Cost estimates are not
    invoices; cache pricing and rounding differ between providers.
    """

    input_per_million: float = 0.0
    output_per_million: float = 0.0
    cache_read_per_million: float = 0.0
    cache_write_per_million: float = 0.0
    currency: str = "USD"

    @field_validator(
        "input_per_million",
        "output_per_million",
        "cache_read_per_million",
        "cache_write_per_million",
    )
    @classmethod
    def _non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("model prices must not be negative")
        return value

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not cleaned or len(cleaned) > 8:
            raise ValueError("currency must be a short non-empty code")
        return cleaned


class ObservabilityConfig(BaseModel):
    """Trace and metrics policy.

    Trace rows are diagnostics, never a security mechanism. Metrics are
    aggregated from authoritative domain tables and the event log instead of
    being written a second time; this keeps the runtime write path lean.
    """

    enabled: bool = True
    traces_enabled: bool = True
    metrics_enabled: bool = True
    # 1.0 keeps every run. Sampling is per root trace and recorded in its
    # attributes so a gap is explainable.
    trace_sample_ratio: float = 1.0
    trace_batch_size: int = 32
    trace_max_attributes_bytes: int = 8_192
    trace_retention_days: int = 14
    trace_max_traces: int = 50_000
    # A running trace older than this is reported as incomplete. It is not
    # auto-closed while a concurrently running process may still own it.
    incomplete_trace_grace_s: int = 3_600
    pricing: dict[str, ModelPrice] = Field(default_factory=dict)
    default_currency: str = "USD"

    @field_validator("trace_sample_ratio")
    @classmethod
    def _sample_ratio(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("trace_sample_ratio must be in [0, 1]")
        return value

    @field_validator(
        "trace_batch_size",
        "trace_max_attributes_bytes",
        "trace_retention_days",
        "trace_max_traces",
        "incomplete_trace_grace_s",
    )
    @classmethod
    def _positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be positive")
        return value

    @field_validator("default_currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not cleaned or len(cleaned) > 8:
            raise ValueError("currency must be a short non-empty code")
        return cleaned

    def price_for(self, provider: str, model: str) -> ModelPrice | None:
        """Look up the most specific configured price.

        Precedence: ``provider:model``, then ``model``, then ``provider:*``,
        then ``*``. A missing price is reported as "pricing unavailable"; the
        cost is never fabricated.
        """
        candidates = (
            f"{provider}:{model}",
            model,
            f"{provider}:*",
            "*",
        )
        for key in candidates:
            price = self.pricing.get(key)
            if price is not None:
                return price
        return None


class BenchmarksConfig(BaseModel):
    enabled: bool = True
    default_iterations: int = 10
    default_warmup: int = 2
    max_iterations: int = 200
    retention_days: int = 90
    max_runs: int = 1_000
    # Used by `bench compare` to flag likely regressions.
    compare_threshold_pct: float = 20.0

    @field_validator(
        "default_iterations", "default_warmup", "max_iterations", "retention_days", "max_runs"
    )
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must not be negative")
        return value

    @field_validator("compare_threshold_pct")
    @classmethod
    def _threshold(cls, value: float) -> float:
        if value < 0:
            raise ValueError("compare_threshold_pct must not be negative")
        return value


class Settings(BaseModel):
    # Bumped only when the configuration file format changes incompatibly.
    config_version: int = 1
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
    tasks: TasksConfig = Field(default_factory=TasksConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    benchmarks: BenchmarksConfig = Field(default_factory=BenchmarksConfig)

    @field_validator("config_version")
    @classmethod
    def _config_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError(f"unsupported config_version {value}; this build expects 1")
        return value

    def provider(self, name: str | None = None) -> ProviderConfig:
        provider_name = name or self.agent.default_provider
        try:
            return self.providers[provider_name]
        except KeyError as exc:
            known = ", ".join(sorted(self.providers)) or "(none)"
            raise KeyError(f"unknown provider {provider_name!r}; configured: {known}") from exc

    def provider_names(self) -> list[str]:
        return sorted(self.providers)

    @model_validator(mode="after")
    def _validate_fallback_models(self) -> Settings:
        for spec in self.agent.fallback_models:
            provider_name, separator, model = spec.partition(":")
            if not provider_name or (separator and not model):
                raise ValueError(
                    f"invalid fallback model {spec!r}; use 'provider:model' or 'provider'"
                )
            if provider_name not in self.providers:
                raise ValueError(
                    f"fallback model {spec!r} references unknown provider {provider_name!r}"
                )
        return self

    def resolved_data_dir(self) -> Path:
        return self.storage.data_dir or default_data_dir()

    def ensure_data_dirs(self) -> Path:
        data_dir = self.resolved_data_dir()
        for sub in (
            data_dir,
            data_dir / "artifacts",
            data_dir / "logs",
            data_dir / "backups",
            # External hooks and MCP servers run with a safe cwd outside the
            # agent-writable workspace; that cwd must exist before spawn.
            data_dir / "hooks",
            data_dir / "mcp",
        ):
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
