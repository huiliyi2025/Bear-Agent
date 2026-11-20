"""MCP server lifecycle and tool registration.

The manager is the only component allowed to mutate the tool registry for MCP:
servers are started together, their tools are staged and collision-checked
before any registration happens, and every failure is attributed per server.

Design constraints:

* Lifecycle operations (start/stop/reconnect) are serialized by a lock, while
  independent servers start concurrently.
* A required server failure makes ``start()`` fail atomically (all connections
  are closed) so the composition root never runs with a partial security
  boundary. Optional server failures are reported in the returned status.
* Tool calls are never retried. Cancellation is raced against the in-flight
  call with :class:`CancelToken`.
* Status never contains resolved secret values; error text is redacted through
  the shared :class:`SecretRedactor`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TypeVar

from mcp import types

from bear_agent.config.models import MCPConfig, MCPServerConfig
from bear_agent.core.cancel import CancelToken, RunCancelled
from bear_agent.core.errors import MCPError, MCPToolError, MCPUnavailable
from bear_agent.mcp.client import (
    ServerConnection,
    close_connection,
    connect_server,
)
from bear_agent.mcp.tools import MCPTool
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.secrets.store import SecretStore
from bear_agent.tools.registry import ToolRegistry

STATE_DISABLED = "disabled"
STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_READY = "ready"
STATE_ERROR = "error"
MAX_TOOL_DESCRIPTION_CHARS = 2_000
MAX_TOOL_SCHEMA_BYTES = 64 * 1024

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class MCPServerStatus:
    """Secret-free snapshot of one configured server."""

    name: str
    transport: str
    state: str
    tools: tuple[str, ...] = ()
    last_error: str | None = None
    reconnect_count: int = 0
    required: bool = True


class MCPManager:
    def __init__(
        self,
        config: MCPConfig,
        registry: ToolRegistry,
        *,
        workspace: Path,
        server_cwd: Path | None = None,
        redactor: SecretRedactor | None = None,
        logger: logging.Logger | None = None,
        secret_store: SecretStore | None = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.workspace = Path(workspace)
        self.server_cwd = Path(server_cwd).resolve() if server_cwd else None
        self.redactor = redactor
        self.logger = logger or logging.getLogger("bear_agent.mcp")
        self.secret_store = secret_store
        self._connections: dict[str, ServerConnection] = {}
        self._statuses: dict[str, MCPServerStatus] = {
            name: MCPServerStatus(
                name=name,
                transport=server.transport,
                state=STATE_STOPPED if config.enabled and server.enabled else STATE_DISABLED,
                required=server.required,
            )
            for name, server in config.servers.items()
        }
        self._lifecycle_lock = asyncio.Lock()
        self._started = False

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, MCPServerStatus]:
        return {
            name: replace(status, tools=tuple(status.tools))
            for name, status in self._statuses.items()
        }

    # --------------------------------------------------------------- lifecycle

    async def start(self) -> dict[str, MCPServerStatus]:
        """Start every enabled server and register their tools.

        Raises :class:`MCPUnavailable` if any required server fails; in that
        case all connections opened during this call are closed again.
        """
        async with self._lifecycle_lock:
            if not self.config.enabled:
                return self.status()
            enabled = {
                name: server
                for name, server in self.config.servers.items()
                if server.enabled and name not in self._connections
            }
            if not enabled:
                self._started = True
                return self.status()
            if self._started:
                return self.status()

            for name in enabled:
                self._update_status(name, state=STATE_STARTING, last_error=None)

            try:
                results = await asyncio.gather(
                    *(self._start_server(name, server) for name, server in enabled.items()),
                    return_exceptions=True,
                )
            except asyncio.CancelledError:
                await self._stop_all_locked()
                raise
            failures: list[tuple[str, BaseException]] = []
            for name, result in zip(enabled, results, strict=True):
                if isinstance(result, asyncio.CancelledError):
                    await self._stop_all_locked()
                    raise result
                if isinstance(result, BaseException):
                    failures.append((name, result))
            if failures:
                await self._stop_all_locked(preserve_errors=True)
                required = [(name, error) for name, error in failures if enabled[name].required]
                if required:
                    messages = " | ".join(
                        f"{name}: {self._safe_error(error)}" for name, error in required
                    )
                    raise MCPUnavailable(
                        f"required MCP server(s) failed to start: {messages}. "
                        "Fix the server configuration or set required=false."
                    )
                # Only unexpected BaseExceptions for optional servers reach here.
                self._started = True
                return self.status()
            self._started = True
            return self.status()

    async def stop(self) -> None:
        """Close every connection and unregister every MCP tool."""
        async with self._lifecycle_lock:
            await self._stop_all_locked()

    async def reconnect(self, server_name: str) -> MCPServerStatus:
        """Close one server, start it again, and rediscover its tools."""
        async with self._lifecycle_lock:
            server = self.config.servers.get(server_name)
            if server is None:
                raise MCPUnavailable(f"unknown MCP server: {server_name!r}")
            if not self.config.enabled or not server.enabled:
                raise MCPUnavailable(
                    f"MCP server {server_name!r} is disabled; enable it before reconnecting"
                )
            previous = self._statuses.get(server_name)
            reconnect_count = (previous.reconnect_count if previous else 0) + 1
            await self._close_connection(server_name)
            self._update_status(
                server_name,
                state=STATE_STARTING,
                tools=(),
                last_error=None,
                reconnect_count=reconnect_count,
            )
            return await self._start_server(server_name, server)

    # ------------------------------------------------------------------ calls

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_s: float | None = None,
        cancel: CancelToken | None = None,
    ) -> dict[str, Any]:
        """Call one remote tool exactly once.

        Failures raise :class:`MCPToolError` (or :class:`MCPUnavailable` when
        the server is not connected). There is deliberately no retry.
        """
        server = self.config.servers.get(server_name)
        if server is None:
            raise MCPUnavailable(f"unknown MCP server: {server_name!r}")
        connection = self._connections.get(server_name)
        status = self._statuses.get(server_name)
        if connection is None or status is None or status.state != STATE_READY:
            detail = status.last_error if status and status.last_error else "not connected"
            raise MCPUnavailable(f"MCP server {server_name!r} is not connected ({detail})")
        if tool_name not in {definition.name for definition in connection.definitions}:
            raise MCPToolError(
                f"MCP tool {tool_name!r} is not available on server {server_name!r} "
                "(not advertised)"
            )
        if not self._tool_allowed(server, tool_name):
            raise MCPToolError(
                f"MCP tool {tool_name!r} is not available on server {server_name!r} "
                "(filtered by allowed_tools/denied_tools)"
            )
        if not isinstance(arguments, dict):
            raise MCPToolError("MCP tool arguments must be a JSON object")
        if cancel is not None:
            cancel.raise_if_cancelled()

        effective_timeout = server.timeout_s
        if timeout_s is not None and timeout_s > 0:
            effective_timeout = min(timeout_s, server.timeout_s)
        label = f"{server_name}/{tool_name}"
        try:
            result = await self._await_call(
                connection.session.call_tool(
                    tool_name,
                    arguments,
                    read_timeout_seconds=effective_timeout,
                ),
                cancel=cancel,
                timeout_s=effective_timeout,
                label=label,
            )
        except (RunCancelled, asyncio.CancelledError):
            raise
        except TimeoutError as exc:
            message = f"MCP tool {label} timed out after {effective_timeout:g}s"
            self._note_call_error(server_name, message)
            raise MCPToolError(message) from exc
        except MCPError:
            raise
        except Exception as exc:
            message = f"MCP tool {label} failed: {type(exc).__name__}: {self._safe_error(exc)}"
            self._note_call_error(server_name, message)
            raise MCPToolError(message) from exc

        if not isinstance(result, types.CallToolResult):
            raise MCPToolError(
                f"MCP tool {label} returned an unsupported result type: {type(result).__name__}"
            )
        return self._result_payload(result)

    # ------------------------------------------------------------ internals

    async def _start_server(self, server_name: str, server: MCPServerConfig) -> MCPServerStatus:
        if not server.trusted:
            message = (
                f"MCP server {server_name!r} is not trusted; set trusted=true only "
                "for servers you actually trust"
            )
            self._update_status(server_name, state=STATE_ERROR, tools=(), last_error=message)
            if server.required:
                raise MCPUnavailable(message)
            return self._statuses[server_name]

        try:
            stack, session, definitions = await connect_server(
                server_name,
                server,
                workspace=self.workspace,
                secret_store=self.secret_store,
                redactor=self.redactor,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = self._safe_error(exc)
            self._update_status(server_name, state=STATE_ERROR, tools=(), last_error=message)
            self.logger.warning("MCP server %s failed to start: %s", server_name, message)
            if server.required:
                raise MCPUnavailable(
                    f"MCP server {server_name!r} ({server.transport}) failed to start: "
                    f"{message}. Check the command/url, environment and startup timeout."
                ) from exc
            return self._statuses[server_name]

        selected = [
            definition for definition in definitions if self._tool_allowed(server, definition.name)
        ]
        capped: list[Any] = []
        for definition in selected:
            try:
                schema_size = len(
                    json.dumps(definition.input_schema, ensure_ascii=False, default=str)
                )
            except (TypeError, ValueError):
                schema_size = MAX_TOOL_SCHEMA_BYTES + 1
            if schema_size > MAX_TOOL_SCHEMA_BYTES:
                self.logger.warning(
                    "skipping MCP tool %s/%s: schema is %s bytes (limit %s)",
                    server_name,
                    definition.name,
                    schema_size,
                    MAX_TOOL_SCHEMA_BYTES,
                )
                continue
            description = definition.description or ""
            if len(description) > MAX_TOOL_DESCRIPTION_CHARS:
                description = description[:MAX_TOOL_DESCRIPTION_CHARS] + "..."
            capped.append((definition, description))
        tools = [
            MCPTool(
                manager=self,
                server_name=server_name,
                remote_name=definition.name,
                description=description,
                input_schema=definition.input_schema,
                timeout_s=server.timeout_s,
            )
            for definition, description in capped
        ]

        conflict = self._registration_conflict(server_name, tools)
        if conflict is not None:
            await close_connection(
                ServerConnection(
                    name=server_name,
                    config=server,
                    stack=stack,
                    session=session,
                    definitions=definitions,
                )
            )
            self._update_status(server_name, state=STATE_ERROR, tools=(), last_error=conflict)
            self.logger.warning("MCP server %s rejected: %s", server_name, conflict)
            if server.required:
                raise MCPUnavailable(conflict)
            return self._statuses[server_name]

        registered: list[str] = []
        try:
            for tool in tools:
                self.registry.register(tool)
                registered.append(tool.name)
        except Exception as exc:  # pragma: no cover - validation makes this unreachable
            for name in registered:
                self.registry.unregister(name)
            await close_connection(
                ServerConnection(
                    name=server_name,
                    config=server,
                    stack=stack,
                    session=session,
                    definitions=definitions,
                )
            )
            message = self._safe_error(exc)
            self._update_status(server_name, state=STATE_ERROR, tools=(), last_error=message)
            if server.required:
                raise MCPUnavailable(
                    f"MCP server {server_name!r} tool registration failed: {message}"
                ) from exc
            return self._statuses[server_name]

        self._connections[server_name] = ServerConnection(
            name=server_name,
            config=server,
            stack=stack,
            session=session,
            definitions=definitions,
            registered_tools=registered,
        )
        self._update_status(
            server_name,
            state=STATE_READY,
            tools=tuple(registered),
            last_error=None,
        )
        self.logger.info(
            "MCP server %s started (%s) with %d tool(s)",
            server_name,
            server.transport,
            len(registered),
        )
        return self._statuses[server_name]

    def _registration_conflict(self, server_name: str, tools: list[MCPTool]) -> str | None:
        seen: dict[str, str] = {}
        for tool in tools:
            if tool.name in seen:
                return (
                    f"MCP server {server_name!r} tools {seen[tool.name]!r} and "
                    f"{tool.remote_name!r} map to the same local name {tool.name!r}"
                )
            seen[tool.name] = tool.remote_name
        for tool in tools:
            if self.registry.has(tool.name):
                return (
                    f"MCP tool name collision: {tool.name!r} from server "
                    f"{server_name!r} is already registered"
                )
        return None

    @staticmethod
    def _tool_allowed(server: MCPServerConfig, tool_name: str) -> bool:
        if server.allowed_tools and tool_name not in server.allowed_tools:
            return False
        return tool_name not in server.denied_tools

    async def _await_call(
        self,
        awaitable: Awaitable[T],
        *,
        cancel: CancelToken | None,
        timeout_s: float,
        label: str,
    ) -> T:
        task: asyncio.Task[T] = asyncio.ensure_future(awaitable)
        waiter: asyncio.Task[None] | None = None
        try:
            async with asyncio.timeout(timeout_s):
                if cancel is None:
                    return await task
                waiter = asyncio.create_task(cancel.wait())
                done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
                if waiter in done and not task.done():
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await task
                    raise RunCancelled(f"run cancelled while calling MCP tool {label}")
                return await task
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            raise
        finally:
            if waiter is not None:
                waiter.cancel()
                with contextlib.suppress(BaseException):
                    await waiter
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

    @staticmethod
    def _result_payload(result: types.CallToolResult) -> dict[str, Any]:
        return {
            "content": [block.model_dump(mode="python", by_alias=True) for block in result.content],
            "structured_content": result.structured_content,
            "is_error": result.is_error,
            "meta": result.meta,
        }

    async def _stop_all_locked(self, *, preserve_errors: bool = False) -> None:
        for server_name in list(self._connections):
            await self._close_connection(server_name)
        for server_name, status in list(self._statuses.items()):
            if preserve_errors and status.state == STATE_ERROR:
                self._statuses[server_name] = replace(status, tools=())
                continue
            server = self.config.servers.get(server_name)
            disabled = not self.config.enabled or server is None or not server.enabled
            self._statuses[server_name] = replace(
                status,
                state=STATE_DISABLED if disabled else STATE_STOPPED,
                tools=(),
                last_error=None,
            )
        self._started = False

    async def _close_connection(self, server_name: str) -> None:
        connection = self._connections.pop(server_name, None)
        if connection is not None:
            for tool_name in connection.registered_tools:
                current = self.registry.get(tool_name)
                if isinstance(current, MCPTool) and current.server_name == server_name:
                    self.registry.unregister(tool_name)
            await close_connection(connection)
        self._update_status(server_name, state=STATE_STOPPED, tools=(), last_error=None)

    def _update_status(
        self,
        server_name: str,
        *,
        state: str,
        tools: tuple[str, ...] | None = None,
        last_error: str | None = None,
        reconnect_count: int | None = None,
    ) -> None:
        current = self._statuses.get(server_name)
        if current is None:
            return
        changes: dict[str, Any] = {"state": state, "last_error": last_error}
        if tools is not None:
            changes["tools"] = tools
        if reconnect_count is not None:
            changes["reconnect_count"] = reconnect_count
        self._statuses[server_name] = replace(current, **changes)

    def _note_call_error(self, server_name: str, message: str) -> None:
        self._update_status(server_name, state=STATE_READY, last_error=self._safe_text(message))

    def _safe_text(self, text: str) -> str:
        if self.redactor is not None:
            return self.redactor.redact(text)
        return text

    def _safe_error(self, exc: BaseException) -> str:
        return self._safe_text(str(exc) or type(exc).__name__)
