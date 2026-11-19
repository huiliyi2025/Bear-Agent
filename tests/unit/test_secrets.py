from __future__ import annotations

import logging
from pathlib import Path

import pytest

from bear_agent.config.models import ProviderConfig
from bear_agent.core.errors import ConfigError
from bear_agent.observability.logging import install_redacting_filter
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.secrets.store import (
    InMemorySecretStore,
    KeyringSecretStore,
    SecretManager,
    SecretStoreUnavailable,
    UnavailableSecretStore,
    resolve_secret_reference,
)
from bear_agent.storage.database import Database
from bear_agent.storage.security_repositories import SecretNameRepository


def test_redactor_exact_and_patterns() -> None:
    redactor = SecretRedactor()
    redactor.register("deepseek", "super-secret-value")
    text = (
        "key=super-secret-value api_key=abcdef1234567890 "
        "Authorization: Bearer abcdefghijklmnop sk-abcdefghijklmnop"
    )
    redacted = redactor.redact(text)
    assert "super-secret-value" not in redacted
    assert "abcdef1234567890" not in redactor.redact('{"api_key": "abcdef1234567890"}')
    assert "abcdef1234567890" not in redacted
    assert "abcdefghijklmnop" not in redacted
    assert "[REDACTED" in redacted


def test_redact_obj_recurses() -> None:
    redactor = SecretRedactor()
    redactor.register("x", "secret-value")
    payload = {"a": "secret-value", "b": ["secret-value", 3], "c": {"d": "ok"}}
    result = redactor.redact_obj(payload)
    assert "secret-value" not in str(result)
    assert result["c"]["d"] == "ok"


def test_private_key_block_redacted() -> None:
    redactor = SecretRedactor()
    text = "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"
    assert "abc" not in redactor.redact(text)


def test_secret_reference_resolution() -> None:
    store = InMemorySecretStore({"deepseek": "key-123456"})
    assert resolve_secret_reference("secret://deepseek", store) == "key-123456"
    assert resolve_secret_reference("plain-value", store) == "plain-value"
    assert resolve_secret_reference("env:NOT_SET_XYZ", store) == ""
    with pytest.raises(ConfigError, match="was not found"):
        resolve_secret_reference("secret://missing", store)
    with pytest.raises(ConfigError, match="requires a secret store"):
        resolve_secret_reference("secret://deepseek", None)


def test_provider_secret_reference() -> None:
    store = InMemorySecretStore({"deepseek": "key-123456"})
    provider = ProviderConfig(api_key="secret://deepseek")
    assert provider.resolve_api_key(store) == "key-123456"
    assert provider.has_secret_reference


def test_unavailable_store_fails_closed() -> None:
    store = UnavailableSecretStore()
    assert not store.available()
    with pytest.raises(SecretStoreUnavailable):
        store.get("x")


@pytest.mark.asyncio
async def test_secret_manager_lifecycle(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    try:
        redactor = SecretRedactor()
        manager = SecretManager(InMemorySecretStore(), SecretNameRepository(db), redactor)
        await manager.set("deepseek", "secret-value")
        assert await manager.list_names() == ["deepseek"]
        assert await manager.get("deepseek") == "secret-value"
        assert "secret-value" not in redactor.redact("secret-value")
        await manager.delete("deepseek")
        assert await manager.list_names() == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_secret_manager_rejects_bad_name(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    try:
        manager = SecretManager(InMemorySecretStore(), SecretNameRepository(db), SecretRedactor())
        with pytest.raises(ValueError):
            await manager.set("bad name", "value")
    finally:
        db.close()


def test_logging_filter_redacts_secret(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("bear_agent.test_redaction")
    redactor = SecretRedactor()
    redactor.register("x", "super-secret-value")
    install_redacting_filter(redactor)
    logger.warning("value=%s", "super-secret-value")
    assert "super-secret-value" not in caplog.text


def test_keyring_plaintext_backend_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    class PlaintextKeyring:
        pass

    class FakeKeyring:
        def get_keyring(self) -> object:
            return PlaintextKeyring()

        def get_password(self, _service: str, _name: str) -> None:
            return None

    store = KeyringSecretStore()
    store._keyring = FakeKeyring()  # type: ignore[assignment]
    assert not store.available()
    with pytest.raises(SecretStoreUnavailable):
        store.set("x", "value")
