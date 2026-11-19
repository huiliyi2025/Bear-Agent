"""The shell tool.

Execution is a pipeline, not a single string concatenation: command policy
preflight -> sandbox selection -> shell argv -> bounded process runner. The
runtime sees stdout/stderr/exit status and a record of whether a real OS
boundary was applied.
"""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import BaseModel, Field

from bear_agent.config.models import ShellConfig
from bear_agent.core.errors import SandboxError
from bear_agent.core.modes import ToolSideEffect
from bear_agent.policy.path_guard import PathAccessError
from bear_agent.shell.environment import build_shell_env, shell_argv
from bear_agent.shell.policy import CommandAction, CommandPolicy
from bear_agent.shell.runner import ShellResult, ShellRunner
from bear_agent.shell.sandbox.base import SandboxManager
from bear_agent.tools.base import (
    ClaimRequest,
    PreflightAction,
    PreflightDecision,
    Tool,
    ToolContext,
    ToolResult,
)


class BashArgs(BaseModel):
    command: str = Field(description="Shell command to execute.")
    timeout_s: float | None = Field(
        default=None,
        ge=0.1,
        description="Optional timeout in seconds; capped by configuration.",
    )
    cwd: str = Field(default=".", description="Working directory inside the workspace.")
    description: str | None = Field(
        default=None,
        max_length=500,
        description="Short human-readable purpose of the command (for audit).",
    )


class BashTool(Tool):
    name = "bash"
    description = (
        "Run a shell command in the workspace. On Windows this uses PowerShell "
        "or cmd according to configuration. Commands are policy-checked, require "
        "approval when configured, and may be rejected if no OS sandbox is available. "
        "Use for builds, tests, git inspection and filesystem operations that the "
        "dedicated tools do not cover."
    )
    args_model = BashArgs
    side_effect = ToolSideEffect.EXEC
    idempotent = False
    concurrency_safe = False

    def __init__(
        self,
        *,
        config: ShellConfig,
        workspace: str | Path,
        sandbox: SandboxManager,
        runner: ShellRunner | None = None,
        platform: str | None = None,
    ) -> None:
        self.config = config
        self.workspace = Path(workspace).resolve()
        self.sandbox = sandbox
        self.runner = runner or ShellRunner()
        self.platform = platform or sys.platform
        self.policy = CommandPolicy(config, platform=self.platform, workspace=self.workspace)
        self.timeout_s = config.max_timeout_s + 10.0

    async def preflight(self, args: BaseModel, ctx: ToolContext) -> PreflightDecision | None:
        assert isinstance(args, BashArgs)
        try:
            backend = self.sandbox.ensure_available()
        except SandboxError as exc:
            return PreflightDecision(
                PreflightAction.DENY,
                f"sandbox unavailable: {exc}",
                risk="critical",
                details={"sandbox_error": str(exc)},
            )
        filesystem = backend.capabilities.filesystem
        decision = self.policy.evaluate(args.command, sandbox_filesystem=filesystem)
        action = {
            CommandAction.ALLOW: PreflightAction.ALLOW,
            CommandAction.ASK: PreflightAction.ASK,
            CommandAction.DENY: PreflightAction.DENY,
        }[decision.action]
        return PreflightDecision(
            action,
            decision.reason,
            risk=decision.risk,
            details={
                "matched_rules": list(decision.matched_rules),
                "sandbox_backend": backend.name,
                "sandbox_filesystem": filesystem,
                "sandbox_capabilities": backend.capabilities.as_dict(),
            },
        )

    def claim_paths(self, args: BaseModel) -> list[ClaimRequest]:
        assert isinstance(args, BashArgs)
        return [ClaimRequest(path=".", scope="tree", mode="exclusive")]

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, BashArgs)
        if not self.config.enabled:
            return _error("shell execution is disabled in configuration", "disabled")
        try:
            cwd = ctx.path_guard.resolve(args.cwd or ".", mode="read")
        except PathAccessError as exc:
            return _error(str(exc), "path_denied")
        if not cwd.is_dir():
            return _error(f"working directory does not exist: {cwd}", "invalid_argument")

        backend = None
        try:
            backend = self.sandbox.ensure_available()
            filesystem = backend.capabilities.filesystem
            decision = self.policy.evaluate(args.command, sandbox_filesystem=filesystem)
            # DENY is a hard boundary. ASK is accepted only when the executor
            # marked this context approved; direct callers cannot bypass approval.
            if decision.action == CommandAction.DENY:
                return _error(f"command denied by policy: {decision.reason}", "command_denied")
            if decision.action == CommandAction.ASK and not ctx.approved:
                return _error(
                    "command requires approval, but this tool was invoked outside "
                    "the tool executor approval chain",
                    "approval_required",
                )
        except SandboxError as exc:
            return _error(f"sandbox unavailable: {exc}", "sandbox_unavailable")

        timeout = min(
            args.timeout_s or self.config.default_timeout_s,
            self.config.max_timeout_s,
        )
        env = build_shell_env(self.config, self.workspace)
        argv = shell_argv(self.config, args.command, self.platform)
        try:
            sandboxed = self.sandbox.wrap(argv, cwd)
        except SandboxError as exc:
            return _error(f"sandbox unavailable: {exc}", "sandbox_unavailable")

        result = await self.runner.run(
            args.command,
            sandboxed,
            cwd=cwd,
            env=env,
            timeout_s=timeout,
            cancel=ctx.cancel,
            max_output_bytes=self.config.max_output_bytes,
        )
        return _to_tool_result(result)


def _to_tool_result(result: ShellResult) -> ToolResult:
    parts: list[str] = [f"$ {result.command}"]
    if result.stdout:
        parts.append(result.stdout.rstrip("\n"))
    if result.stderr:
        parts.append("[stderr]\n" + result.stderr.rstrip("\n"))
    if result.error:
        parts.append(f"[error] {result.error}")
    if result.timed_out:
        parts.append(f"[timed out after {result.duration_ms} ms]")
    if result.cancelled:
        parts.append("[cancelled]")
    if result.exit_code is not None:
        parts.append(f"[exit code {result.exit_code}]")
    if result.truncated:
        parts.append("[output truncated]")
    parts.append(
        f"[sandbox: {result.sandbox_backend}"
        + ("" if result.sandboxed else ", NOT OS-ISOLATED")
        + "]"
    )
    if result.sandbox_note:
        parts.append(f"[sandbox note] {result.sandbox_note}")
    content = "\n".join(parts)
    is_error = (
        bool(result.error)
        or result.timed_out
        or result.cancelled
        or (result.exit_code not in (0, None))
    )
    error_kind: str | None = None
    if result.error:
        error_kind = "sandbox_or_spawn_error"
    elif result.cancelled:
        error_kind = "cancelled"
    elif result.timed_out:
        error_kind = "timeout"
    elif result.exit_code not in (0, None):
        error_kind = "exec_failure"
    return ToolResult(
        content=content,
        display=content,
        is_error=is_error,
        error_kind=error_kind,
        meta={
            "command": result.command,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "timed_out": result.timed_out,
            "cancelled": result.cancelled,
            "truncated": result.truncated,
            "sandbox_backend": result.sandbox_backend,
            "sandboxed": result.sandboxed,
            "sandbox_capabilities": result.sandbox_capabilities,
            "sandbox_note": result.sandbox_note,
        },
    )


def _error(message: str, kind: str) -> ToolResult:
    return ToolResult(content=message, is_error=True, error_kind=kind)
