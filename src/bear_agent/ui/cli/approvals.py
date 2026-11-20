"""Interactive approval adapter with once/session/project scopes."""

from __future__ import annotations

import asyncio
import json

from rich.console import Console
from rich.panel import Panel

from bear_agent.policy.models import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalScope,
)
from bear_agent.secrets.redaction import SecretRedactor


class CliApprover:
    def __init__(
        self,
        console: Console | None = None,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self.console = console or Console()
        self.redactor = redactor or SecretRedactor()

    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        return await asyncio.to_thread(self._ask, request)

    def _ask(self, request: ApprovalRequest) -> ApprovalDecision:
        arguments = json.dumps(
            self.redactor.redact_obj(request.arguments),
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        reason = self.redactor.redact(request.reason)
        if len(arguments) > 3000:
            arguments = arguments[:3000] + "\n... [truncated]"
        project_hint = (
            "  [p]roject (same exact call in this project)"
            if request.project_id
            else "  [p]roject unavailable (no project id)"
        )
        self.console.print(
            Panel(
                f"[bold]{request.tool_name}[/bold]\n"
                f"[dim]risk={request.risk} · {reason}[/dim]\n\n{arguments}\n\n"
                "Allow:\n"
                "  [o]nce\n"
                "  [s]ession (same exact call in this session)\n"
                f"{project_hint}\n"
                "  [d]eny",
                title="Approval required",
                border_style="yellow",
            )
        )
        while True:
            try:
                answer = input("Choice [o/s/p/d]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return ApprovalDecision(False, ApprovalScope.DENY, "approval interrupted")
            if answer in {"o", "once"}:
                return ApprovalDecision(True, ApprovalScope.ONCE, "approved once")
            if answer in {"s", "session"}:
                return ApprovalDecision(True, ApprovalScope.SESSION, "approved for session")
            if answer in {"p", "project"}:
                if request.project_id:
                    return ApprovalDecision(True, ApprovalScope.PROJECT, "approved for project")
                self.console.print("[red]project scope requires a project id[/red]")
                continue
            if answer in {"d", "deny", ""}:
                return ApprovalDecision(False, ApprovalScope.DENY, "denied by user")
            self.console.print("[red]choose o, s, p or d[/red]")
