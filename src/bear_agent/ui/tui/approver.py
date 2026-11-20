"""Approval UI for the TUI.

The TUI is only a presentation surface for the existing ApprovalService. It
never auto-approves, never weakens the policy engine, and always shows the
redacted tool arguments plus the requested scope options. A pending approval is
denied when the app exits or the request is cancelled.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, RichLog, Static

from bear_agent.policy.models import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalScope,
)

if TYPE_CHECKING:
    from bear_agent.ui.tui.app import BearTUI


def _args_text(request: ApprovalRequest) -> Text:
    try:
        rendered = json.dumps(request.arguments, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        rendered = str(request.arguments)
    if len(rendered) > 4_000:
        rendered = rendered[:4_000] + "\n... [truncated]"
    return Text(rendered)


class ApprovalScreen(ModalScreen[ApprovalDecision]):
    """Full-detail modal; buttons and single-key shortcuts resolve the future."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("o", "allow_once", "Allow once"),
        Binding("s", "allow_session", "Allow session"),
        Binding("p", "allow_project", "Allow project"),
        Binding("d,escape", "deny", "Deny"),
    ]

    AUTO_FOCUS = ""

    DEFAULT_CSS = """
    ApprovalScreen {
        align: center middle;
    }
    #approval {
        width: 92%;
        height: auto;
        max-height: 90%;
        border: round $warning;
        background: $surface;
        padding: 1 2;
    }
    #approval-title {
        text-style: bold;
        color: $warning;
    }
    #approval-meta {
        color: $text-muted;
        margin-bottom: 1;
    }
    #approval-args {
        height: 12;
        border: round $primary;
        padding: 0 1;
    }
    #approval-buttons {
        height: auto;
        margin-top: 1;
    }
    #approval-buttons Button {
        margin-right: 1;
    }
    """

    def __init__(self, request: ApprovalRequest) -> None:
        super().__init__()
        self.request = request

    def on_mount(self) -> None:
        # Never let Enter land on an approval Button. The full argument log is
        # focused instead, so an accidental Enter cannot authorize the call.
        log = self.query_one("#approval-args", RichLog)
        log.write(_args_text(self.request))
        log.focus()

    def compose(self) -> ComposeResult:
        project_available = (
            bool(self.request.project_id) and ApprovalScope.PROJECT in self.request.scope_options
        )
        session_available = ApprovalScope.SESSION in self.request.scope_options
        once_available = ApprovalScope.ONCE in self.request.scope_options
        with Vertical(id="approval"):
            yield Static(
                Text(f"Approval required: {self.request.tool_name}", style="bold yellow"),
                id="approval-title",
            )
            yield Static(
                Text(
                    f"risk={self.request.risk}  "
                    f"reason={self.request.reason or '(none)'}\n"
                    "The tool call will not run until you choose a scope.",
                    style="dim",
                ),
                id="approval-meta",
            )
            yield RichLog(
                id="approval-args",
                markup=False,
                wrap=True,
                auto_scroll=False,
            )
            with Horizontal(id="approval-buttons"):
                yield Button(
                    "Allow once [o]",
                    id="once",
                    variant="primary",
                    disabled=not once_available,
                )
                yield Button(
                    "Allow session [s]" if session_available else "Session unavailable",
                    id="session",
                    disabled=not session_available,
                )
                yield Button(
                    "Allow project [p]" if project_available else "Project unavailable",
                    id="project",
                    disabled=not project_available,
                )
                yield Button("Deny [d]", id="deny", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        mapping = {
            "once": ApprovalDecision(True, ApprovalScope.ONCE, "approved once"),
            "session": ApprovalDecision(True, ApprovalScope.SESSION, "approved for session"),
            "project": ApprovalDecision(True, ApprovalScope.PROJECT, "approved for project"),
            "deny": ApprovalDecision(False, ApprovalScope.DENY, "denied by user"),
        }
        decision = mapping.get(event.button.id or "")
        if decision is not None:
            self.dismiss(decision)

    def action_allow_once(self) -> None:
        if ApprovalScope.ONCE in self.request.scope_options:
            self.dismiss(ApprovalDecision(True, ApprovalScope.ONCE, "approved once"))

    def action_allow_session(self) -> None:
        if ApprovalScope.SESSION in self.request.scope_options:
            self.dismiss(ApprovalDecision(True, ApprovalScope.SESSION, "approved for session"))

    def action_allow_project(self) -> None:
        if self.request.project_id and ApprovalScope.PROJECT in self.request.scope_options:
            self.dismiss(ApprovalDecision(True, ApprovalScope.PROJECT, "approved for project"))

    def action_deny(self) -> None:
        self.dismiss(ApprovalDecision(False, ApprovalScope.DENY, "denied by user"))


class TuiApprover:
    """Approver port adapter backed by a Textual modal."""

    def __init__(self) -> None:
        self._app: BearTUI | None = None
        self._pending: set[asyncio.Future[ApprovalDecision]] = set()

    def attach(self, app: BearTUI) -> None:
        self._app = app

    def detach(self) -> None:
        self._app = None
        self.cancel_pending("application exited")

    def cancel_pending(self, reason: str) -> None:
        for future in list(self._pending):
            if not future.done():
                future.set_result(ApprovalDecision(False, ApprovalScope.DENY, reason))
        self._pending.clear()

    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        app = self._app
        if app is None:
            return ApprovalDecision(False, ApprovalScope.DENY, "approval UI unavailable")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalDecision] = loop.create_future()
        self._pending.add(future)
        try:
            app.submit_approval(request, future)
            return await future
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            raise
        finally:
            self._pending.discard(future)


def approval_callback(
    future: asyncio.Future[ApprovalDecision],
) -> Callable[[ApprovalDecision | None], None]:
    def resolve(decision: ApprovalDecision | None) -> None:
        if future.done():
            return
        future.set_result(
            decision or ApprovalDecision(False, ApprovalScope.DENY, "approval dismissed")
        )

    return resolve
