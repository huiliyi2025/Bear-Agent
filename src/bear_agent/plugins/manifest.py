"""Declarative plugin manifests.

A plugin directory contains ``plugin.toml``. The manifest is data only: it can
contribute external hook commands and MCP server definitions, and it may opt in
to a trusted in-process Python entrypoint. Nothing in a manifest is executed
unless the user has enabled Python plugins and the manifest itself declares the
``python`` permission.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, Self

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from bear_agent.config.models import HookCommandConfig, MCPConfig, MCPServerConfig
from bear_agent.core.errors import PluginManifestError

PLUGIN_TOML = "plugin.toml"
SCHEMA_VERSION = 1
DEFAULT_ENTRYPOINT = "plugin.py"

# Plugin ids are used in hook ids (``plugin:<id>:<hook>``), MCP server names
# (``<id>_<server>``) and Python module names, so restrict them to a portable
# slug. Dots and colons are intentionally excluded.
_PLUGIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# Dependency syntax: ``other_plugin`` or ``other_plugin>=1.0,<2.0``.
_DEPENDENCY_RE = re.compile(r"^(?P<id>[a-z0-9][a-z0-9_-]{0,63})(?P<specifier>\s*(?:[<>=!~].*)?)$")


class PluginPermissions(BaseModel):
    """Capabilities a manifest explicitly asks for.

    The default is no capabilities. Hook and MCP contributions are rejected
    unless the matching flag is true; the Python entrypoint is never executed
    unless both ``python = true`` and ``plugins.allow_python = true``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    hooks: bool = False
    mcp: bool = False
    python: bool = False


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = SCHEMA_VERSION
    id: str
    name: str = Field(min_length=1, max_length=200)
    version: str
    description: str = ""
    author: str = ""
    requires_bear: str = ""
    homepage: str = ""
    dependencies: list[str] = Field(default_factory=list)
    permissions: PluginPermissions = Field(default_factory=PluginPermissions)
    hooks: list[HookCommandConfig] = Field(default_factory=list)
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    entrypoint: str = DEFAULT_ENTRYPOINT
    enabled_by_default: bool = False

    @field_validator("schema_version")
    @classmethod
    def _schema_version(cls, value: int) -> int:
        if value != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {value}; this build supports "
                f"schema_version {SCHEMA_VERSION}"
            )
        return value

    @field_validator("id")
    @classmethod
    def _plugin_id(cls, value: str) -> str:
        if not _PLUGIN_ID_RE.match(value):
            raise ValueError(
                "plugin id must be 1..64 characters, start with a lowercase letter or "
                "digit, and contain only lowercase letters, digits, '_' or '-'"
            )
        return value

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        try:
            Version(value)
        except InvalidVersion as exc:
            raise ValueError(f"version {value!r} is not a valid PEP 440 version") from exc
        return value

    @field_validator("requires_bear")
    @classmethod
    def _requires_bear(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        try:
            SpecifierSet(value)
        except InvalidSpecifier as exc:
            raise ValueError(f"requires_bear {value!r} is not a valid version specifier") from exc
        return value

    @field_validator("dependencies")
    @classmethod
    def _dependencies(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        for raw in value:
            text = raw.strip()
            if not text:
                raise ValueError("dependency entries must not be empty")
            match = _DEPENDENCY_RE.match(text)
            if match is None:
                raise ValueError(
                    f"invalid dependency {raw!r}; expected 'plugin_id' or 'plugin_id>=1.0.0'"
                )
            dependency_id = match.group("id")
            specifier = match.group("specifier").strip()
            if specifier:
                try:
                    SpecifierSet(specifier)
                except InvalidSpecifier as exc:
                    raise ValueError(
                        f"invalid version specifier in dependency {raw!r}: {exc}"
                    ) from exc
            if dependency_id in seen:
                raise ValueError(f"duplicate dependency {dependency_id!r}")
            seen.add(dependency_id)
        return value

    @field_validator("mcp_servers")
    @classmethod
    def _mcp_servers(cls, value: dict[str, MCPServerConfig]) -> dict[str, MCPServerConfig]:
        if not value:
            return value
        # Reuse the runtime MCP validation: server name syntax, trusted=true,
        # stdio command / HTTP URL, TLS and credential-in-URL checks.
        try:
            MCPConfig(servers=value)
        except ValidationError as exc:
            raise ValueError(_format_validation_error(exc)) from exc
        return value

    @model_validator(mode="after")
    def _validate_contributions(self) -> Self:
        if self.hooks and not self.permissions.hooks:
            raise ValueError(
                "manifest declares hooks but permissions.hooks is false; add "
                "[permissions] hooks = true after reviewing the commands"
            )
        if self.mcp_servers and not self.permissions.mcp:
            raise ValueError(
                "manifest declares mcp_servers but permissions.mcp is false; add "
                "[permissions] mcp = true after reviewing the servers"
            )
        if self.entrypoint != DEFAULT_ENTRYPOINT and not self.permissions.python:
            raise ValueError(
                "manifest sets entrypoint but permissions.python is false; a Python "
                "entrypoint is only loaded with an explicit python permission"
            )
        if self.permissions.python:
            entrypoint = Path(self.entrypoint)
            if entrypoint.is_absolute() or ".." in entrypoint.parts:
                raise ValueError("entrypoint must be a relative path inside the plugin directory")
        hook_ids = [f"plugin:{self.id}:{hook.id}" for hook in self.hooks]
        if len(set(hook_ids)) != len(hook_ids):
            raise ValueError("duplicate hook ids in manifest")
        if any(_dependency_id(dependency) == self.id for dependency in self.dependencies):
            raise ValueError("a plugin must not depend on itself")
        return self

    @property
    def bear_specifier(self) -> SpecifierSet:
        return SpecifierSet(self.requires_bear) if self.requires_bear else SpecifierSet()

    @property
    def dependency_specifiers(self) -> dict[str, SpecifierSet]:
        result: dict[str, SpecifierSet] = {}
        for raw in self.dependencies:
            match = _DEPENDENCY_RE.match(raw.strip())
            assert match is not None  # validated above
            specifier = match.group("specifier").strip()
            result[match.group("id")] = SpecifierSet(specifier) if specifier else SpecifierSet()
        return result

    def namespaced_hook_id(self, hook_id: str) -> str:
        return f"plugin:{self.id}:{hook_id}"


def _dependency_id(dependency: str) -> str:
    match = _DEPENDENCY_RE.match(dependency.strip())
    assert match is not None  # validated above
    return match.group("id")


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        message = str(error["msg"])
        prefix = "Value error, "
        if message.startswith(prefix):
            message = message[len(prefix) :]
        parts.append(f"{location}: {message}")
    return "; ".join(parts) or str(exc)


def load_manifest(path: str | Path) -> PluginManifest:
    """Load and validate ``plugin.toml``.

    ``path`` may be the plugin directory or the manifest file itself. Every
    error includes the concrete path so a broken plugin is actionable without
    reading a traceback.
    """

    target = Path(path).expanduser()
    manifest_path = target / PLUGIN_TOML if target.is_dir() else target
    if not manifest_path.is_file():
        raise PluginManifestError(
            f"plugin manifest not found: {manifest_path} (expected a directory "
            f"containing {PLUGIN_TOML})"
        )
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PluginManifestError(f"cannot read plugin manifest {manifest_path}: {exc}") from exc
    try:
        raw: Any = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PluginManifestError(
            f"invalid TOML in plugin manifest {manifest_path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise PluginManifestError(
            f"invalid plugin manifest {manifest_path}: root must be a TOML table"
        )
    try:
        manifest = PluginManifest.model_validate(raw)
    except ValidationError as exc:
        raise PluginManifestError(
            f"invalid plugin manifest {manifest_path}: {_format_validation_error(exc)}"
        ) from exc

    if manifest.permissions.python:
        plugin_dir = manifest_path.parent.resolve()
        entrypoint = (plugin_dir / manifest.entrypoint).resolve()
        if not entrypoint.is_relative_to(plugin_dir):
            raise PluginManifestError(
                f"invalid plugin manifest {manifest_path}: entrypoint "
                f"{manifest.entrypoint!r} escapes the plugin directory"
            )
        if not entrypoint.is_file():
            raise PluginManifestError(
                f"invalid plugin manifest {manifest_path}: Python entrypoint "
                f"{entrypoint} does not exist"
            )
    return manifest


__all__ = [
    "DEFAULT_ENTRYPOINT",
    "PLUGIN_TOML",
    "SCHEMA_VERSION",
    "PluginManifest",
    "PluginPermissions",
    "load_manifest",
]
