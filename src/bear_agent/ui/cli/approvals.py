"""Interactive approval adapter."""

from __future__ import annotations

import asyncio
import json

from rich.console import Console
from rich.panel import Panel

from bear_agent.policy.models import ApprovalRequest


class CliApprover:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    async def request(self, request: ApprovalRequest) -> bool:
        return await asyncio.to_thread(self._ask, request)

    def _ask(self, request: ApprovalRequest) -> bool:
        arguments = json.dumps(request.arguments, ensure_ascii=False, indent=2, default=str)
        if len(arguments) > 3000:
            arguments = arguments[:3000] + "\n... [truncated]"
        self.console.print(
            Panel(
                f"[bold]{request.tool_name}[/bold]\n[dim]{request.reason}[/dim]\n\n{arguments}",
                title="Approval required",
                border_style="yellow",
            )
        )
        try:
            answer = input("Allow this action? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in {"y", "yes"}
