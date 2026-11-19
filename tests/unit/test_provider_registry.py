from __future__ import annotations

import pytest

from bear_agent.config.models import AgentConfig, ProviderConfig, Settings, StorageConfig
from bear_agent.core.errors import ConfigError
from bear_agent.providers.registry import create_provider
from bear_agent.providers.retry import RetryingProvider


def _settings(**provider_overrides: object) -> Settings:
    return Settings(
        providers={
            "fake": ProviderConfig(
                base_url="http://fake.local/v1",
                api_key="test",
                **provider_overrides,
            )
        },
        agent=AgentConfig(
            default_provider="fake", default_model="fake-model", context_window=8_000
        ),
        storage=StorageConfig(),
    )


def test_create_provider_wraps_retries() -> None:
    provider = create_provider(_settings(max_retries=1))
    assert isinstance(provider, RetryingProvider)
    assert provider.capabilities.context_window == 8_000


def test_create_provider_without_retries() -> None:
    provider = create_provider(_settings(max_retries=0))
    assert not isinstance(provider, RetryingProvider)


def test_missing_key_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_KEY", raising=False)
    settings = Settings(
        providers={
            "fake": ProviderConfig(
                base_url="https://api.example.com/v1",
                api_key=None,
                api_key_env="MISSING_KEY",
            )
        },
        agent=AgentConfig(default_provider="fake", default_model="x"),
        storage=StorageConfig(),
    )
    with pytest.raises(ConfigError, match="no API key"):
        create_provider(settings)


def test_validate_ready_local_without_key() -> None:
    settings = Settings(
        providers={"local": ProviderConfig(base_url="http://127.0.0.1:11434/v1")},
        agent=AgentConfig(default_provider="local", default_model="local-model"),
        storage=StorageConfig(),
    )
    settings.validate_ready()
