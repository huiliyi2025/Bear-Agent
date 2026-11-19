"""Secret storage.

The production backend is the OS keyring (Windows Credential Manager, macOS
Keychain, Secret Service on Linux). There is deliberately no plaintext-file
fallback: if the keyring is unavailable, ``secret://`` references fail closed.

``env:`` references remain available for CI and local development, but they are
environment variables, not a keyring substitute.
"""

from __future__ import annotations

import os
from typing import Protocol

from bear_agent.core.errors import ConfigError, SecretStoreError
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.storage.security_repositories import SecretNameRepository


class SecretStoreUnavailable(SecretStoreError):
    pass


class SecretStore(Protocol):
    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...
    def available(self) -> bool: ...


class KeyringSecretStore:
    def __init__(self, service: str = "bear-agent") -> None:
        self.service = service
        self._keyring = None
        try:
            import keyring as keyring_module

            self._keyring = keyring_module
        except Exception:
            self._keyring = None

    def _backend_is_secure(self) -> bool:
        if self._keyring is None:
            return False
        try:
            backend = self._keyring.get_keyring()
            return self._backend_object_is_secure(backend)
        except Exception:
            return False

    def _backend_object_is_secure(self, backend: object) -> bool:
        name = type(backend).__name__.lower()
        module = type(backend).__module__.lower()
        insecure_markers = ("plaintext", "keyrings.alt", "file")
        if any(marker in name or marker in module for marker in insecure_markers):
            return False
        if "fail" in name or "null" in name:
            return False
        children = getattr(backend, "backends", None)
        if children:
            return all(self._backend_object_is_secure(child) for child in children)
        return True

    def available(self) -> bool:
        if not self._backend_is_secure():
            return False
        keyring = self._keyring
        if keyring is None:
            return False
        try:
            # Probe read access; a backend can exist but be locked/unavailable.
            keyring.get_password(self.service, "__bear_probe__")
            return True
        except Exception:
            return False

    def _require_secure(self) -> None:
        if not self._backend_is_secure():
            raise SecretStoreUnavailable(
                "keyring backend is unavailable or insecure (plaintext/file backends are refused)"
            )

    def get(self, name: str) -> str | None:
        self._require_secure()
        keyring = self._keyring
        if keyring is None:
            raise SecretStoreUnavailable("keyring module is not available")
        try:
            value = keyring.get_password(self.service, name)
            return str(value) if value is not None else None
        except Exception as exc:
            raise SecretStoreUnavailable(f"keyring read failed: {exc}") from exc

    def set(self, name: str, value: str) -> None:
        self._require_secure()
        keyring = self._keyring
        if keyring is None:
            raise SecretStoreUnavailable("keyring module is not available")
        try:
            keyring.set_password(self.service, name, value)
        except Exception as exc:
            raise SecretStoreUnavailable(f"keyring write failed: {exc}") from exc

    def delete(self, name: str) -> None:
        self._require_secure()
        keyring = self._keyring
        if keyring is None:
            raise SecretStoreUnavailable("keyring module is not available")
        try:
            keyring.delete_password(self.service, name)
        except Exception as exc:
            raise SecretStoreUnavailable(f"keyring delete failed: {exc}") from exc


class InMemorySecretStore:
    """Test backend; never used by the default composition root."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._values = dict(initial or {})

    def available(self) -> bool:
        return True

    def get(self, name: str) -> str | None:
        return self._values.get(name)

    def set(self, name: str, value: str) -> None:
        self._values[name] = value

    def delete(self, name: str) -> None:
        self._values.pop(name, None)


class UnavailableSecretStore:
    def available(self) -> bool:
        return False

    def get(self, name: str) -> str | None:
        raise SecretStoreUnavailable("secret store is unavailable")

    def set(self, name: str, value: str) -> None:
        raise SecretStoreUnavailable("secret store is unavailable")

    def delete(self, name: str) -> None:
        raise SecretStoreUnavailable("secret store is unavailable")


def resolve_secret_reference(value: str, store: SecretStore | None) -> str:
    """Resolve ``secret://name`` / ``env:NAME`` / plain values."""
    if value.startswith("secret://"):
        name = value[len("secret://") :]
        if not name:
            raise ConfigError("secret reference must include a name")
        if store is None:
            raise ConfigError(
                f"secret reference {name!r} requires a secret store, but none is configured"
            )
        secret = store.get(name)
        if not secret:
            raise ConfigError(f"secret {name!r} was not found in the secret store")
        return secret
    if value.startswith("keyring://"):
        return resolve_secret_reference("secret://" + value[len("keyring://") :], store)
    if value.startswith("env:"):
        env_name = value[len("env:") :]
        return os.environ.get(env_name, "")
    return value


class SecretManager:
    def __init__(
        self,
        store: SecretStore,
        names: SecretNameRepository,
        redactor: SecretRedactor,
    ) -> None:
        self.store = store
        self.names = names
        self.redactor = redactor

    @property
    def available(self) -> bool:
        return self.store.available()

    async def set(self, name: str, value: str) -> None:
        if not name or any(ch.isspace() for ch in name):
            raise ValueError("secret name must be non-empty and contain no whitespace")
        self.store.set(name, value)
        from datetime import UTC, datetime

        await self.names.add(name, datetime.now(UTC))
        self.redactor.register(name, value)

    async def delete(self, name: str) -> None:
        self.store.delete(name)
        await self.names.delete(name)
        self.redactor.unregister(name)

    async def get(self, name: str) -> str | None:
        value = self.store.get(name)
        if value:
            self.redactor.register(name, value)
        return value

    async def list_names(self) -> list[str]:
        return await self.names.list_names()

    async def hydrate_redactor(self) -> None:
        if not self.available:
            return
        for name in await self.list_names():
            try:
                value = self.store.get(name)
            except SecretStoreError:
                continue
            if value:
                self.redactor.register(name, value)


def secret_store_from_config(backend: str) -> SecretStore:
    if backend == "keyring":
        return KeyringSecretStore()
    if backend == "none":
        return UnavailableSecretStore()
    raise ConfigError(f"unknown secret backend: {backend}")
