"""Transport-level MCP client plumbing.

This module owns the parts that touch the operating system and the network:
spawning stdio servers, building a Streamable HTTP client and creating an
initialized ``ClientSession``. It deliberately does not know about the tool
registry, permissions or status reporting; :mod:`bear_agent.mcp.manager` owns
those.

Security notes:

* stdio children receive a conservative base environment plus the explicitly
  configured server environment. ``secret://`` / ``keyring://`` / ``env:``
  values are resolved here; resolved values are never logged.
* Streamable HTTP URLs are re-validated (the configuration model already does
  this) so a programmatically constructed config cannot smuggle credentials in
  the URL or downgrade to plaintext off-loopback.
* No MCP call is ever retried automatically: tool side effects are unknown.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from bear_agent.config.models import MCPServerConfig
from bear_agent.core.errors import MCPUnavailable
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.secrets.store import SecretStore, resolve_secret_reference

# Keep the base environment small. MCP's stdio client also applies its own
# inherited-variable allowlist, so this is defense in depth rather than the
# only line of defense.
SAFE_ENV_KEYS: frozenset[str] = frozenset(
    {
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
        "SYSTEMDRIVE",
        "COMSPEC",
        "PATHEXT",
        "WINDIR",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROCESSOR_ARCHITECTURE",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "VIRTUAL_ENV",
        "PYTHONIOENCODING",
        "PYTHONUNBUFFERED",
    }
)

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_LOOPBACK_PREFIX = "127."
_MAX_PAGE_TOOLS = 1_000
_MAX_TOOL_PAGES = 100


@dataclass(frozen=True, slots=True)
class MCPToolDefinition:
    """A tool as advertised by a server, before local filtering/renaming."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(slots=True)
class ServerConnection:
    """One live MCP server connection."""

    name: str
    config: MCPServerConfig
    stack: AsyncExitStack
    session: ClientSession
    definitions: tuple[MCPToolDefinition, ...]
    registered_tools: list[str] = field(default_factory=list)


def _is_reference(value: str) -> bool:
    return value.startswith(("secret://", "keyring://", "env:"))


def resolve_reference(
    value: str,
    *,
    store: SecretStore | None,
    redactor: SecretRedactor | None,
    server_name: str,
    label: str,
) -> str:
    """Resolve a secret reference without ever logging the resolved value."""
    try:
        resolved = resolve_secret_reference(value, store)
    except Exception as exc:
        raise MCPUnavailable(
            f"{label} for MCP server {server_name!r} could not be resolved "
            f"({type(exc).__name__}); check the secret store or environment"
        ) from exc
    if _is_reference(value) and not resolved:
        raise MCPUnavailable(
            f"{label} for MCP server {server_name!r} resolved to an empty value; "
            "the secret or environment variable is missing"
        )
    if redactor is not None and _is_reference(value):
        redactor.register(f"mcp:{server_name}:{label}", resolved)
    return resolved


def build_stdio_environment(
    server_name: str,
    config: MCPServerConfig,
    *,
    secret_store: SecretStore | None = None,
    redactor: SecretRedactor | None = None,
) -> dict[str, str]:
    """Build the explicit child environment (safe base + configured overrides)."""
    env = {key: os.environ[key] for key in SAFE_ENV_KEYS if key in os.environ}
    for key, raw_value in config.env.items():
        if not key or "=" in key or "\x00" in key:
            raise MCPUnavailable(
                f"invalid environment variable name for MCP server {server_name!r}: {key!r}"
            )
        value = resolve_reference(
            raw_value,
            store=secret_store,
            redactor=redactor,
            server_name=server_name,
            label=f"env:{key}",
        )
        if "\x00" in value:
            raise MCPUnavailable(
                f"environment variable {key!r} for MCP server {server_name!r} contains a NUL byte"
            )
        env[key] = value
    return env


def build_http_headers(
    server_name: str,
    config: MCPServerConfig,
    *,
    secret_store: SecretStore | None = None,
    redactor: SecretRedactor | None = None,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, raw_value in config.headers.items():
        if not key or any(char in key for char in "\r\n:"):
            raise MCPUnavailable(
                f"invalid HTTP header name for MCP server {server_name!r}: {key!r}"
            )
        value = resolve_reference(
            raw_value,
            store=secret_store,
            redactor=redactor,
            server_name=server_name,
            label=f"header:{key}",
        )
        if "\r" in value or "\n" in value:
            raise MCPUnavailable(
                f"HTTP header {key!r} for MCP server {server_name!r} contains a newline"
            )
        headers[key] = value
    return headers


def validate_server_url(server_name: str, config: MCPServerConfig) -> str:
    """Re-apply the URL rules from the config model at connection time."""
    url = config.url
    if not url or any(ord(char) < 0x20 for char in url):
        raise MCPUnavailable(f"MCP server {server_name!r} has an empty or invalid url")
    try:
        parts = urlsplit(url)
        hostname = (parts.hostname or "").lower()
        # Accessing ``port`` validates it (raises ValueError for a bad port).
        _ = parts.port
    except ValueError as exc:
        raise MCPUnavailable(f"MCP server {server_name!r} has an invalid url") from exc
    if parts.scheme not in ("http", "https") or not hostname:
        raise MCPUnavailable(f"MCP server {server_name!r} url must be an absolute http(s) url")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise MCPUnavailable(f"MCP server {server_name!r} url must not embed credentials")
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname == "localhost"
    if parts.scheme == "http" and not loopback:
        raise MCPUnavailable(
            f"MCP server {server_name!r} must use https; plain http is allowed only "
            "for loopback development servers"
        )
    return url


def _resolve_cwd(
    server_name: str,
    config: MCPServerConfig,
    workspace: Path,
    default_cwd: Path | None = None,
) -> Path:
    if not config.cwd:
        candidate = default_cwd or workspace
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate.resolve()
    candidate = Path(config.cwd)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    candidate = candidate.resolve()
    if not candidate.is_dir():
        raise MCPUnavailable(
            f"MCP server {server_name!r} cwd does not exist or is not a directory: {candidate}"
        )
    return candidate


def _create_http_client(config: MCPServerConfig, headers: dict[str, str]) -> httpx2.AsyncClient:
    timeout = httpx2.Timeout(
        config.timeout_s,
        connect=min(30.0, config.startup_timeout_s),
        read=max(config.timeout_s, 300.0),
        write=config.timeout_s,
        pool=config.timeout_s,
    )
    if config.verify_tls:
        return create_mcp_http_client(headers=headers or None, timeout=timeout)
    # The official factory does not expose TLS verification, so build the
    # client directly when the user explicitly opted out (loopback dev only).
    return httpx2.AsyncClient(headers=headers or None, timeout=timeout, verify=False)


async def _list_all_tools(session: ClientSession) -> list[MCPToolDefinition]:
    definitions: list[MCPToolDefinition] = []
    cursor: str | None = None
    pages = 0
    while True:
        pages += 1
        if pages > _MAX_TOOL_PAGES:
            raise MCPUnavailable(
                f"MCP server exceeded {_MAX_TOOL_PAGES} tool pages; refusing an "
                "unbounded pagination cursor"
            )
        if cursor is None:
            page = await session.list_tools()
        else:
            page = await session.list_tools(params=types.PaginatedRequestParams(cursor=cursor))
        for tool in page.tools:
            definitions.append(
                MCPToolDefinition(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=dict(tool.input_schema),
                )
            )
        if len(definitions) > _MAX_PAGE_TOOLS:
            raise MCPUnavailable(f"MCP server returned more than {_MAX_PAGE_TOOLS} tools; refusing")
        cursor = page.next_cursor
        if not cursor:
            return definitions


async def _initialize(
    session: ClientSession,
    server_name: str,
) -> list[MCPToolDefinition]:
    await session.initialize()
    definitions = await _list_all_tools(session)
    names: set[str] = set()
    for definition in definitions:
        if definition.name in names:
            raise MCPUnavailable(
                f"MCP server {server_name!r} advertised duplicate tool name {definition.name!r}"
            )
        names.add(definition.name)
    return definitions


async def connect_server(
    server_name: str,
    config: MCPServerConfig,
    *,
    workspace: Path,
    default_cwd: Path | None = None,
    secret_store: SecretStore | None = None,
    redactor: SecretRedactor | None = None,
) -> tuple[AsyncExitStack, ClientSession, tuple[MCPToolDefinition, ...]]:
    """Connect, initialize, and discover tools for one server.

    The caller owns the returned ``AsyncExitStack`` and must close it to stop
    the server. Any failure closes the stack before raising.
    """
    stack = AsyncExitStack()
    try:
        async with asyncio.timeout(config.startup_timeout_s):
            if config.transport == "stdio":
                if not config.command:
                    raise MCPUnavailable(
                        f"MCP server {server_name!r} transport is stdio but command is empty"
                    )
                params = StdioServerParameters(
                    command=config.command,
                    args=list(config.args),
                    env=build_stdio_environment(
                        server_name,
                        config,
                        secret_store=secret_store,
                        redactor=redactor,
                    ),
                    cwd=str(_resolve_cwd(server_name, config, workspace, default_cwd)),
                    encoding="utf-8",
                    encoding_error_handler="replace",
                )
                read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
            elif config.transport == "streamable_http":
                url = validate_server_url(server_name, config)
                headers = build_http_headers(
                    server_name,
                    config,
                    secret_store=secret_store,
                    redactor=redactor,
                )
                client = _create_http_client(config, headers)
                await stack.enter_async_context(client)
                streams = await stack.enter_async_context(
                    streamable_http_client(url, http_client=client)
                )
                read_stream, write_stream = streams
            else:  # pragma: no cover - Literal is enforced by pydantic
                raise MCPUnavailable(
                    f"MCP server {server_name!r} has unsupported transport {config.transport!r}"
                )

            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            definitions = await _initialize(session, server_name)
        return stack, session, tuple(definitions)
    except TimeoutError as exc:
        with contextlib.suppress(Exception):
            await stack.aclose()
        raise MCPUnavailable(
            f"MCP server {server_name!r} did not initialize within "
            f"{config.startup_timeout_s:g}s; increase startup_timeout_s or check the "
            "server process"
        ) from exc
    except BaseException:
        with contextlib.suppress(Exception):
            await stack.aclose()
        raise


async def close_connection(connection: ServerConnection) -> None:
    """Close a connection, tolerating servers that already died."""
    with contextlib.suppress(Exception):
        await connection.stack.aclose()
