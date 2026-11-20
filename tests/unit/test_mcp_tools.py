"""Unit tests for the MCP tool adapter and transport helpers."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from bear_agent.config.models import MCPConfig, MCPServerConfig
from bear_agent.core.cancel import CancelToken, RunCancelled
from bear_agent.core.errors import MCPError, MCPToolError, MCPUnavailable
from bear_agent.mcp.client import (
    build_http_headers,
    build_stdio_environment,
    validate_server_url,
)
from bear_agent.mcp.manager import MCPManager
from bear_agent.mcp.tools import (
    MAX_ARGUMENT_BYTES,
    MCPArguments,
    MCPTool,
    namespaced_tool_name,
    sanitize_tool_name,
)
from bear_agent.policy.path_guard import PathGuard
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.secrets.store import InMemorySecretStore
from bear_agent.tools.base import ToolContext
from bear_agent.tools.registry import ToolRegistry

ECHO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


class FakeManager:
    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.payload = payload if payload is not None else {"content": []}
        self.error = error
        self.calls: list[tuple[str, str, dict[str, Any], float | None, Any]] = []
        self.redactor: SecretRedactor | None = SecretRedactor()

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_s: float | None = None,
        cancel: CancelToken | None = None,
    ) -> dict[str, Any]:
        self.calls.append((server_name, tool_name, arguments, timeout_s, cancel))
        if self.error is not None:
            raise self.error
        return self.payload


def make_tool(
    manager: FakeManager,
    *,
    schema: dict[str, Any] | None = None,
    server: str = "calc",
    remote: str = "echo",
    timeout_s: float = 7.5,
) -> MCPTool:
    return MCPTool(
        manager=manager,  # type: ignore[arg-type]
        server_name=server,
        remote_name=remote,
        description="Echo text",
        input_schema=schema or ECHO_SCHEMA,
        timeout_s=timeout_s,
    )


def make_ctx(
    workspace: Path,
    *,
    approved: bool = True,
    max_output_chars: int = 24_000,
    redactor: SecretRedactor | None = None,
) -> ToolContext:
    extras: dict[str, Any] = {}
    if redactor is not None:
        extras["redactor"] = redactor
    return ToolContext(
        cwd=workspace,
        session_id="test-session",
        cancel=CancelToken(),
        path_guard=PathGuard(workspace),
        max_output_chars=max_output_chars,
        approved=approved,
        extras=extras,
    )


def _args(tool: MCPTool, value: dict[str, Any]) -> Any:
    return tool.args_model.model_validate(value)


# ---------------------------------------------------------------- naming


def test_tool_names_are_namespaced_sanitized_and_bounded() -> None:
    assert namespaced_tool_name("calc", "echo") == "mcp__calc__echo"
    assert namespaced_tool_name("calc", "weird.tool name") == "mcp__calc__weird_tool_name"
    assert sanitize_tool_name("") == "tool"
    assert sanitize_tool_name("!!!") == "___"

    long_server = "s" * 64
    long_tool = "t" * 64
    first = namespaced_tool_name(long_server, long_tool)
    second = namespaced_tool_name(long_server, long_tool + "x")
    assert len(first) <= 64
    assert len(second) <= 64
    assert first != second
    assert first.startswith("mcp__")
    assert all(char.isalnum() or char in "_-" for char in first)


def test_duplicate_names_after_sanitization_are_rejected(workspace: Path) -> None:
    manager = MCPManager(MCPConfig(), ToolRegistry(), workspace=workspace)
    first = MCPTool(
        manager=manager,
        server_name="srv",
        remote_name="a.b",
        input_schema=ECHO_SCHEMA,
    )
    second = MCPTool(
        manager=manager,
        server_name="srv",
        remote_name="a_b",
        input_schema=ECHO_SCHEMA,
    )
    assert first.name == second.name == "mcp__srv__a_b"
    conflict = manager._registration_conflict("srv", [first, second])
    assert conflict is not None
    assert "same local name" in conflict


def test_spec_exposes_original_schema_and_tool_metadata(workspace: Path) -> None:
    tool = make_tool(FakeManager(), timeout_s=9.0)
    assert tool.name == "mcp__calc__echo"
    assert "calc" in tool.description and "echo" in tool.description
    assert tool.spec.parameters == ECHO_SCHEMA
    assert tool.side_effect.value == "exec"
    assert tool.idempotent is False
    assert tool.concurrency_safe is False
    assert tool.timeout_s == 9.0
    # The executor-facing model is permissive; real validation uses the schema.
    assert tool.args_model is MCPArguments
    assert tool.args_model.model_validate({"text": 5, "extra": True}).model_dump() == {
        "text": 5,
        "extra": True,
    }


# ---------------------------------------------------------------- execution


async def test_preflight_defers_to_permission_engine(workspace: Path) -> None:
    tool = make_tool(FakeManager())
    assert await tool.preflight(_args(tool, {"text": "hi"}), make_ctx(workspace)) is None


async def test_run_requires_executor_approval(workspace: Path) -> None:
    manager = FakeManager()
    tool = make_tool(manager)
    result = await tool.run(_args(tool, {"text": "hi"}), make_ctx(workspace, approved=False))
    assert result.is_error
    assert result.error_kind == "approval_required"
    assert manager.calls == []


async def test_run_validates_arguments_before_calling_server(workspace: Path) -> None:
    manager = FakeManager()
    tool = make_tool(manager)
    result = await tool.run(_args(tool, {"text": 5}), make_ctx(workspace))
    assert result.is_error
    assert result.error_kind == "invalid_arguments"
    assert "not of type 'string'" in result.content
    assert manager.calls == []

    missing = await tool.run(_args(tool, {}), make_ctx(workspace))
    assert missing.error_kind == "invalid_arguments"
    assert manager.calls == []


async def test_run_rejects_huge_arguments(workspace: Path) -> None:
    manager = FakeManager()
    tool = make_tool(manager)
    huge = "x" * (MAX_ARGUMENT_BYTES + 1)
    result = await tool.run(_args(tool, {"text": huge}), make_ctx(workspace))
    assert result.is_error
    assert result.error_kind == "invalid_arguments"
    assert str(MAX_ARGUMENT_BYTES) in result.content
    assert manager.calls == []


async def test_run_rejects_invalid_server_schema(workspace: Path) -> None:
    manager = FakeManager()
    tool = make_tool(manager, schema={"type": "object", "properties": "nope"})
    result = await tool.run(_args(tool, {"text": "hi"}), make_ctx(workspace))
    assert result.is_error
    assert result.error_kind == "invalid_arguments"
    assert "invalid tool schema" in result.content
    assert manager.calls == []


async def test_run_passes_arguments_exactly_and_renders_text(workspace: Path) -> None:
    manager = FakeManager(
        payload={
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "text", "text": "world"},
            ],
            "structured_content": {"result": 42},
            "is_error": False,
            "meta": None,
        }
    )
    tool = make_tool(manager)
    ctx = make_ctx(workspace)
    result = await tool.run(_args(tool, {"text": "hi"}), ctx)
    assert not result.is_error
    assert result.error_kind is None
    assert result.content == 'hello\nworld\n{"result":42}'
    assert result.meta["server"] == "calc"
    assert result.meta["tool"] == "echo"
    assert result.meta["is_error"] is False
    assert result.meta["structured"] is True
    assert isinstance(result.meta["latency_ms"], int)
    assert result.truncated is False
    assert manager.calls == [("calc", "echo", {"text": "hi"}, 7.5, ctx.cancel)]


async def test_run_renders_non_text_content_as_placeholders(workspace: Path) -> None:
    data = base64.b64encode(b"abcdef").decode("ascii")
    manager = FakeManager(
        payload={
            "content": [
                {"type": "image", "data": data, "mimeType": "image/png"},
                {"type": "audio", "data": data, "mimeType": "audio/wav"},
                {
                    "type": "resource",
                    "resource": {
                        "uri": "mem://doc",
                        "mimeType": "text/plain",
                        "text": "resource body",
                    },
                },
                {
                    "type": "resource",
                    "resource": {
                        "uri": "mem://blob",
                        "mimeType": "application/octet-stream",
                        "blob": data,
                    },
                },
                {"type": "resource_link", "name": "doc", "uri": "mem://doc"},
                {"type": "mystery", "value": 1},
            ],
            "is_error": False,
        }
    )
    tool = make_tool(manager)
    result = await tool.run(_args(tool, {"text": "hi"}), make_ctx(workspace))
    assert "[image mime=image/png 6 bytes]" in result.content
    assert "[audio mime=audio/wav 6 bytes]" in result.content
    assert "[resource uri=mem://doc mime=text/plain text=resource body]" in result.content
    assert "[resource uri=mem://blob mime=application/octet-stream blob 6 bytes]" in (
        result.content
    )
    assert "[resource_link name=doc uri=mem://doc]" in result.content
    assert "[mystery content]" in result.content
    assert result.meta["structured"] is False


async def test_run_maps_server_error_result(workspace: Path) -> None:
    manager = FakeManager(
        payload={
            "content": [{"type": "text", "text": "tool crashed"}],
            "is_error": True,
        }
    )
    tool = make_tool(manager)
    result = await tool.run(_args(tool, {"text": "hi"}), make_ctx(workspace))
    assert result.is_error
    assert result.error_kind == "mcp_tool_error"
    assert result.content == "tool crashed"
    assert result.meta["is_error"] is True


async def test_run_truncates_and_redacts_output(workspace: Path) -> None:
    redactor = SecretRedactor()
    redactor.register("token", "supersecret-token")
    manager = FakeManager(
        payload={
            "content": [
                {"type": "text", "text": "supersecret-token " * 40},
            ],
            "is_error": False,
        }
    )
    manager.redactor = redactor
    tool = make_tool(manager)
    result = await tool.run(_args(tool, {"text": "hi"}), make_ctx(workspace, max_output_chars=80))
    assert result.truncated is True
    assert len(result.content) <= 80
    assert "truncated" in result.content
    assert "supersecret-token" not in result.content
    assert "REDACTED" in result.content


async def test_run_uses_context_redactor(workspace: Path) -> None:
    redactor = SecretRedactor()
    redactor.register("token", "ctx-secret-value")
    manager = FakeManager(payload={"content": [{"type": "text", "text": "ctx-secret-value"}]})
    manager.redactor = None
    tool = make_tool(manager)
    result = await tool.run(_args(tool, {"text": "hi"}), make_ctx(workspace, redactor=redactor))
    assert "ctx-secret-value" not in result.content
    assert "REDACTED" in result.content


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (MCPToolError("remote failure"), "mcp_tool_error"),
        (MCPUnavailable("server down"), "mcp_unavailable"),
        (MCPError("weird"), "mcp_error"),
    ],
)
async def test_run_maps_manager_errors(workspace: Path, error: BaseException, kind: str) -> None:
    tool = make_tool(FakeManager(error=error))
    result = await tool.run(_args(tool, {"text": "hi"}), make_ctx(workspace))
    assert result.is_error
    assert result.error_kind == kind
    assert "failure" in result.content or "down" in result.content or "weird" in result.content


async def test_run_propagates_cancellation(workspace: Path) -> None:
    tool = make_tool(FakeManager(error=RunCancelled("stop")))
    with pytest.raises(RunCancelled):
        await tool.run(_args(tool, {"text": "hi"}), make_ctx(workspace))

    tool = make_tool(FakeManager(error=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await tool.run(_args(tool, {"text": "hi"}), make_ctx(workspace))


# ------------------------------------------------------- manager helpers


def test_trusted_config_validation() -> None:
    with pytest.raises(ValidationError, match="not trusted"):
        MCPConfig(
            servers={"calc": MCPServerConfig(transport="stdio", command="python", trusted=False)}
        )
    # A disabled server may stay untrusted: it never starts.
    config = MCPConfig(
        servers={
            "calc": MCPServerConfig(
                transport="stdio", command="python", trusted=False, enabled=False
            )
        }
    )
    assert config.servers["calc"].enabled is False


def test_stdio_environment_resolves_secrets_and_rejects_missing_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemorySecretStore({"token": "super-secret-token"})
    redactor = SecretRedactor()
    monkeypatch.setenv("BEAR_MCP_TEST_VALUE", "from-environment")
    config = MCPServerConfig(
        transport="stdio",
        command="python",
        trusted=True,
        env={
            "TOKEN": "secret://token",
            "FROM_ENV": "env:BEAR_MCP_TEST_VALUE",
            "PLAIN": "visible",
        },
    )
    env = build_stdio_environment("calc", config, secret_store=store, redactor=redactor)
    assert env["TOKEN"] == "super-secret-token"
    assert env["FROM_ENV"] == "from-environment"
    assert env["PLAIN"] == "visible"
    assert "super-secret-token" not in redactor.redact("super-secret-token")

    missing = MCPServerConfig(
        transport="stdio",
        command="python",
        trusted=True,
        env={"TOKEN": "env:BEAR_MCP_TEST_DOES_NOT_EXIST"},
    )
    with pytest.raises(MCPUnavailable, match="empty"):
        build_stdio_environment("calc", missing, secret_store=store)


def test_http_headers_resolve_secrets_and_reject_injection() -> None:
    store = InMemorySecretStore({"token": "Bearer header-secret-value"})
    redactor = SecretRedactor()
    config = MCPServerConfig(
        transport="streamable_http",
        url="https://example.com/mcp",
        trusted=True,
        headers={"Authorization": "secret://token"},
    )
    headers = build_http_headers("remote", config, secret_store=store, redactor=redactor)
    assert headers == {"Authorization": "Bearer header-secret-value"}
    assert "header-secret-value" not in redactor.redact("Bearer header-secret-value")

    injected = MCPServerConfig(
        transport="streamable_http",
        url="https://example.com/mcp",
        trusted=True,
        headers={"X-Bad": "ok\r\nInjected: yes"},
    )
    with pytest.raises(MCPUnavailable, match="newline"):
        build_http_headers("remote", injected)


def test_validate_server_url_rules() -> None:
    config = MCPServerConfig(
        transport="streamable_http", url="https://example.com/mcp", trusted=True
    )
    assert validate_server_url("remote", config) == "https://example.com/mcp"

    config.url = "http://127.0.0.1:8080/mcp"
    assert validate_server_url("remote", config) == "http://127.0.0.1:8080/mcp"

    config.url = "http://example.com/mcp"
    with pytest.raises(MCPUnavailable, match="https"):
        validate_server_url("remote", config)

    config.url = "https://user:pass@example.com/mcp"
    with pytest.raises(MCPUnavailable, match="credentials"):
        validate_server_url("remote", config)

    config.url = "https://example.com:not-a-port/mcp"
    with pytest.raises(MCPUnavailable, match="invalid url"):
        validate_server_url("remote", config)

    config.url = "ftp://example.com/mcp"
    with pytest.raises(MCPUnavailable, match="absolute http"):
        validate_server_url("remote", config)


def test_disabled_manager_reports_disabled_status(workspace: Path) -> None:
    config = MCPConfig(
        enabled=False,
        servers={"calc": MCPServerConfig(transport="stdio", command="python", trusted=True)},
    )
    registry = ToolRegistry()
    manager = MCPManager(config, registry, workspace=workspace)
    statuses = asyncio.run(manager.start())
    assert statuses["calc"].state == "disabled"
    assert statuses["calc"].tools == ()
    assert registry.names() == []


async def test_optional_start_failure_is_reported_not_raised(workspace: Path) -> None:
    store = InMemorySecretStore({"token": "startup-secret-value"})
    redactor = SecretRedactor()
    config = MCPConfig(
        servers={
            "broken": MCPServerConfig(
                transport="stdio",
                command="definitely-not-a-real-mcp-command-xyz",
                trusted=True,
                required=False,
                startup_timeout_s=5,
                env={"TOKEN": "secret://token", "PYTHONIOENCODING": "utf-8"},
            )
        }
    )
    registry = ToolRegistry()
    manager = MCPManager(
        config,
        registry,
        workspace=workspace,
        redactor=redactor,
        secret_store=store,
    )
    statuses = await manager.start()
    status = statuses["broken"]
    assert status.state == "error"
    assert status.last_error
    assert "startup-secret-value" not in (status.last_error or "")
    assert registry.names() == []


async def test_required_start_failure_raises_and_keeps_error_status(
    workspace: Path,
) -> None:
    config = MCPConfig(
        servers={
            "broken": MCPServerConfig(
                transport="stdio",
                command="definitely-not-a-real-mcp-command-xyz",
                trusted=True,
                required=True,
                startup_timeout_s=5,
            )
        }
    )
    registry = ToolRegistry()
    manager = MCPManager(config, registry, workspace=workspace)
    with pytest.raises(MCPUnavailable, match="required MCP server"):
        await manager.start()
    assert manager.status()["broken"].state == "error"
    assert registry.names() == []
    await manager.stop()
    assert manager.status()["broken"].state == "stopped"


async def test_call_tool_before_start_raises(workspace: Path) -> None:
    config = MCPConfig(
        servers={"calc": MCPServerConfig(transport="stdio", command="python", trusted=True)}
    )
    manager = MCPManager(config, ToolRegistry(), workspace=workspace)
    with pytest.raises(MCPUnavailable, match="not connected"):
        await manager.call_tool("calc", "echo", {"text": "hi"})
