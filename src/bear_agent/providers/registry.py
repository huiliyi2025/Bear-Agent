"""Provider construction from configuration."""

from __future__ import annotations

from bear_agent.config.models import Settings
from bear_agent.core.errors import ConfigError
from bear_agent.providers.base import ModelCapabilities, ModelProvider
from bear_agent.providers.openai_compat import OpenAICompatProvider
from bear_agent.providers.retry import RetryingProvider, RetryPolicy


def create_provider(
    settings: Settings,
    *,
    provider_name: str | None = None,
    model: str | None = None,
    secret_store: object | None = None,
) -> ModelProvider:
    name = provider_name or settings.agent.default_provider
    try:
        provider = settings.provider(name)
    except KeyError as exc:
        raise ConfigError(str(exc)) from exc
    if not provider.is_local and not provider.resolve_api_key(secret_store):
        env_hint = provider.api_key_env or "BEAR_API_KEY"
        raise ConfigError(
            f"provider {name!r} has no API key. Set {env_hint}, add api_key to the "
            "config, or point base_url at a local endpoint."
        )
    resolved_model = model or settings.agent.default_model
    if not resolved_model:
        raise ConfigError("no model configured; set agent.default_model or --model")
    capabilities = ModelCapabilities(
        context_window=settings.agent.context_window,
        max_output_tokens=settings.agent.max_output_tokens,
        supports_tools=True,
        supports_streaming=True,
    )
    inner = OpenAICompatProvider(
        name,
        provider,
        model=resolved_model,
        capabilities=capabilities,
        secret_store=secret_store,
    )
    if provider.max_retries <= 0:
        return inner
    return RetryingProvider(
        inner,
        policy=RetryPolicy(max_attempts=provider.max_retries),
    )
