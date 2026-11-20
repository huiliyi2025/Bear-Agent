"""Tool adapter for MCP server tools.

Every remote tool is exposed through the normal :class:`Tool` contract so the
executor's permission, approval, audit and redaction pipeline applies without
special cases. The adapter:

* validates arguments locally against the server-provided JSON Schema before a
  call is made (invalid arguments never reach the server);
* passes arguments through untouched (no shell interpretation);
* renders all MCP content block types into a bounded, redacted text result;
* never retries a call, because remote side effects are unknown.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import TYPE_CHECKING, Any

import jsonschema  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict

from bear_agent.core.cancel import RunCancelled
from bear_agent.core.errors import MCPError, MCPToolError, MCPUnavailable
from bear_agent.core.modes import ToolSideEffect
from bear_agent.core.tool_spec import ToolSpec
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.tools.base import Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from bear_agent.mcp.manager import MCPManager

# A single tool call with more than this much serialized input is almost
# certainly a model error (or an attempt to exhaust the server). Reject it
# locally instead of forwarding it.
MAX_ARGUMENT_BYTES = 256 * 1024
MAX_RESULT_BLOCK_CHARS = 64_000
MAX_STRUCTURED_CHARS = 64_000
MAX_TOOL_NAME_LENGTH = 64
_RESOURCE_PREVIEW_CHARS = 200
_INVALID_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")


class MCPArguments(BaseModel):
    """Permissive argument envelope.

    The executor calls ``args_model.model_validate(call.arguments)`` before a
    tool runs. MCP schemas are dynamic, so the envelope accepts any object and
    the real validation happens against the server schema inside ``run``.
    """

    model_config = ConfigDict(extra="allow")


def sanitize_tool_name(name: str) -> str:
    cleaned = _INVALID_NAME_CHARS.sub("_", name or "")
    return cleaned or "tool"


def namespaced_tool_name(server_name: str, tool_name: str) -> str:
    """Build ``mcp__<server>__<tool>`` with a hard 64-char limit.

    Provider APIs cap function names. Long server/tool pairs are shortened and
    disambiguated with a digest of the original pair.
    """
    server = sanitize_tool_name(server_name)
    tool = sanitize_tool_name(tool_name)
    prefix = f"mcp__{server}__"
    if len(prefix) + len(tool) <= MAX_TOOL_NAME_LENGTH:
        return prefix + tool
    digest = hashlib.sha256(f"{server_name}\x00{tool_name}".encode()).hexdigest()[:8]
    suffix = f"_{digest}"
    budget = MAX_TOOL_NAME_LENGTH - len("mcp__") - len("__") - len(suffix)
    server_budget = min(len(server), max(1, budget // 2))
    tool_budget = max(1, budget - server_budget)
    return f"mcp__{server[:server_budget]}__{tool[:tool_budget]}{suffix}"


def _decoded_size(data: Any) -> int:
    if not isinstance(data, str) or not data:
        return 0
    # Compute decoded size without materializing the base64 payload; a
    # compromised server must not be able to force a huge allocation here.
    padding = len(data) - len(data.rstrip("="))
    return max(0, (len(data) * 3) // 4 - padding)


def _render_block(block: dict[str, Any]) -> str:
    block_type = block.get("type")
    if block_type == "text":
        return str(block.get("text", ""))
    if block_type in ("image", "audio"):
        mime = block.get("mime_type") or block.get("mimeType") or "unknown"
        size = _decoded_size(block.get("data"))
        return f"[{block_type} mime={mime} {size} bytes]"
    if block_type == "resource":
        resource = block.get("resource")
        if not isinstance(resource, dict):
            return "[resource]"
        uri = resource.get("uri", "")
        mime = resource.get("mime_type") or resource.get("mimeType") or "unknown"
        if "text" in resource:
            preview = str(resource.get("text", ""))[:_RESOURCE_PREVIEW_CHARS]
            return f"[resource uri={uri} mime={mime} text={preview}]"
        if "blob" in resource:
            size = _decoded_size(resource.get("blob"))
            return f"[resource uri={uri} mime={mime} blob {size} bytes]"
        return f"[resource uri={uri} mime={mime}]"
    if block_type == "resource_link":
        return f"[resource_link name={block.get('name', '')} uri={block.get('uri', '')}]"
    if block_type:
        return f"[{block_type} content]"
    return "[unknown content]"


def _validation_error(schema: dict[str, Any], arguments: dict[str, Any]) -> str | None:
    try:
        validator_cls = jsonschema.validators.validator_for(schema)
        validator_cls.check_schema(schema)
        validator_cls(schema).validate(arguments)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(part) for part in exc.absolute_path) or "(root)"
        return f"{path}: {exc.message}"
    except jsonschema.SchemaError as exc:
        return f"server provided an invalid tool schema: {exc.message}"
    except Exception as exc:  # defensive: a malformed schema must not crash the tool
        return f"could not validate arguments: {type(exc).__name__}: {exc}"
    return None


class MCPTool(Tool):
    """A namespaced proxy for one tool exposed by an MCP server."""

    def __init__(
        self,
        *,
        manager: MCPManager,
        server_name: str,
        remote_name: str,
        description: str = "",
        input_schema: dict[str, Any] | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self._manager = manager
        self.server_name = server_name
        self.remote_name = remote_name
        self.name = namespaced_tool_name(server_name, remote_name)
        detail = f": {description}" if description else ""
        self.description = f"MCP tool {remote_name!r} from server {server_name!r}{detail}"
        self.args_model: type[BaseModel] = MCPArguments
        self.input_schema = dict(input_schema or {"type": "object", "properties": {}})
        # Remote side effects are opaque: every call is treated as exec,
        # non-idempotent and unsafe to run concurrently with itself.
        self.side_effect = ToolSideEffect.EXEC
        self.idempotent = False
        self.concurrency_safe = False
        self.timeout_s = timeout_s

    @property
    def spec(self) -> ToolSpec:
        # Do not expose the permissive envelope schema; providers must see the
        # server's original input schema.
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=self.input_schema,
        )

    async def preflight(self, args: BaseModel, ctx: ToolContext) -> None:
        # No extra decision: side_effect=EXEC already routes through the
        # permission engine and approval service in supervised/auto modes.
        return None

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        if not ctx.approved:
            return ToolResult(
                content=(f"{self.name} requires approval and must run through ToolExecutor"),
                is_error=True,
                error_kind="approval_required",
            )

        arguments = args.model_dump(mode="python")
        try:
            encoded = json.dumps(arguments, ensure_ascii=False, default=str).encode("utf-8")
        except (TypeError, ValueError) as exc:
            return ToolResult(
                content=f"invalid arguments for {self.name}: not JSON serializable ({exc})",
                is_error=True,
                error_kind="invalid_arguments",
            )
        if len(encoded) > MAX_ARGUMENT_BYTES:
            return ToolResult(
                content=(
                    f"invalid arguments for {self.name}: serialized arguments are "
                    f"{len(encoded)} bytes, exceeding the {MAX_ARGUMENT_BYTES}-byte limit"
                ),
                is_error=True,
                error_kind="invalid_arguments",
            )
        problem = _validation_error(self.input_schema, arguments)
        if problem is not None:
            return ToolResult(
                content=f"invalid arguments for {self.name}: {problem}",
                is_error=True,
                error_kind="invalid_arguments",
            )

        started = time.monotonic()
        try:
            payload = await self._manager.call_tool(
                self.server_name,
                self.remote_name,
                arguments,
                timeout_s=self.timeout_s,
                cancel=ctx.cancel,
            )
        except (RunCancelled, asyncio.CancelledError):
            raise
        except MCPToolError as exc:
            return self._failure(exc, "mcp_tool_error", started, ctx)
        except MCPUnavailable as exc:
            return self._failure(exc, "mcp_unavailable", started, ctx)
        except MCPError as exc:
            return self._failure(exc, "mcp_error", started, ctx)
        latency_ms = int((time.monotonic() - started) * 1000)
        return self._convert(payload, ctx, latency_ms)

    def _failure(
        self,
        exc: BaseException,
        error_kind: str,
        started: float,
        ctx: ToolContext,
    ) -> ToolResult:
        latency_ms = int((time.monotonic() - started) * 1000)
        redactor = self._redactor(ctx)
        message = redactor.redact(str(exc)) if redactor is not None else str(exc)
        return ToolResult(
            content=message or type(exc).__name__,
            is_error=True,
            error_kind=error_kind,
            meta={
                "server": self.server_name,
                "tool": self.remote_name,
                "is_error": True,
                "structured": False,
                "latency_ms": latency_ms,
            },
        )

    def _redactor(self, ctx: ToolContext | None) -> SecretRedactor | None:
        if ctx is not None:
            candidate = ctx.extras.get("redactor")
            if isinstance(candidate, SecretRedactor):
                return candidate
        return self._manager.redactor

    def _convert(self, payload: dict[str, Any], ctx: ToolContext, latency_ms: int) -> ToolResult:
        blocks = payload.get("content")
        parts: list[str] = []
        if isinstance(blocks, list):
            for block in blocks:
                rendered = _render_block(block) if isinstance(block, dict) else str(block)
                if len(rendered) > MAX_RESULT_BLOCK_CHARS:
                    rendered = (
                        rendered[:MAX_RESULT_BLOCK_CHARS] + f"\n... [mcp block truncated at "
                        f"{MAX_RESULT_BLOCK_CHARS} chars] ..."
                    )
                parts.append(rendered)
        structured = payload.get("structured_content")
        if structured is not None:
            rendered_structured = json.dumps(
                structured,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
            if len(rendered_structured) > MAX_STRUCTURED_CHARS:
                rendered_structured = (
                    rendered_structured[:MAX_STRUCTURED_CHARS]
                    + f"\n... [structured content truncated at "
                    f"{MAX_STRUCTURED_CHARS} chars] ..."
                )
            parts.append(rendered_structured)
        content = "\n".join(part for part in parts if part)
        is_error = bool(payload.get("is_error", False))
        if not content:
            content = (
                f"MCP tool {self.server_name}/{self.remote_name} reported an error with no content"
                if is_error
                else f"MCP tool {self.server_name}/{self.remote_name} returned no content"
            )
        redactor = self._redactor(ctx)
        if redactor is not None:
            content = redactor.redact(content)
        truncated = False
        limit = ctx.max_output_chars
        if limit > 0 and len(content) > limit:
            marker = f"\n... [truncated {len(content) - limit} chars] ...\n"
            budget = max(0, limit - len(marker))
            if budget == 0:
                content = content[:limit]
            else:
                head = budget * 2 // 3
                tail = budget - head
                content = content[:head] + marker + (content[-tail:] if tail else "")
            truncated = True
        return ToolResult(
            content=content,
            is_error=is_error,
            error_kind="mcp_tool_error" if is_error else None,
            meta={
                "server": self.server_name,
                "tool": self.remote_name,
                "is_error": is_error,
                "structured": structured is not None,
                "latency_ms": latency_ms,
            },
            truncated=truncated,
        )
