"""Interactive terminal UI.

The TUI is a presentation/controller layer over the existing Container. It does
not create a second Runtime, ToolExecutor, policy engine or recovery path: every
agent action goes through the same Runtime, ApprovalService, ToolExecutor,
TaskService and persistence layers as the CLI.

The EventBus is the only live feed. Events are posted to the Textual message
loop, so slow rendering can never block a run, tool execution, approval or the
event bus. The app owns a single conversation task at a time; cancel/exit
cancels that task, waits for the Runtime's cleanup, then closes the Container.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Footer, Input, RichLog, Static

from bear_agent.config.models import Settings
from bear_agent.core.errors import BearError
from bear_agent.core.events import Event, EventType
from bear_agent.core.fencing import LeaseFence, reset_fence, set_fence
from bear_agent.diagnostics import DiagnosticsService
from bear_agent.policy.models import ApprovalDecision, ApprovalRequest, ApprovalScope
from bear_agent.runtime.stop_policy import RunStatus
from bear_agent.sessions.models import SessionRecord
from bear_agent.ui.tui.approver import ApprovalScreen, TuiApprover, approval_callback

HELP_TEXT = """\
/help                 show commands
/session              show current session
/sessions             list recent sessions
/new                  create a new session
/resume <id>          switch to an existing session
/fork [message_seq]   fork the current session
/plan <goal>          generate a Plan
/tasks                show Plan / Todo / Task status
/run-plan             execute the active Plan
/task <id>            show one Task
/cancel-plan          cancel the active Plan
/compact              force context compaction
/anchor <text>        pin a fact that compaction must keep
/anchors              list active anchors
/checkpoints          list checkpoints
/run <text>           run a prompt (same as typing it)
/cancel               cancel the active run or job
/permission <mode>    plan | supervised | auto | unrestricted
/tools                list registered tools
/metrics              live counters / histograms
/trace                latest trace for this session
/status               refresh the runtime panels
/mcp                  MCP server/tool status
/plugins              Plugin status
/hooks                Hook status
/doctor               run static diagnostics
/exit                 quit and clean up
"""


class TuiEvent(Message):
    """EventBus event delivered to the Textual message loop."""

    def __init__(self, event: Event) -> None:
        super().__init__()
        self.event = event


class BearTUI(App[None]):
    """Bear-Agent interactive terminal application."""

    TITLE = "Bear-Agent"
    SUB_TITLE = "interactive agent harness"

    CSS = """
    Screen {
        layout: vertical;
        background: $surface;
    }
    #status {
        height: 3;
        padding: 0 1;
        background: $panel;
        color: $text;
    }
    #body {
        height: 1fr;
        layout: horizontal;
    }
    #main {
        width: 3fr;
        layout: vertical;
    }
    #transcript {
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    #live {
        height: auto;
        max-height: 9;
        padding: 0 1;
        color: $text-muted;
    }
    #sidebar {
        width: 40;
        padding: 0 1;
        border-left: solid $primary;
        overflow-y: auto;
    }
    .side-title {
        text-style: bold;
        color: $accent;
        margin-top: 1;
    }
    #plan, #runtime, #extensions {
        height: auto;
        color: $text;
    }
    #prompt {
        dock: bottom;
        height: 3;
        margin: 0 1;
    }
    Footer {
        height: 1;
    }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("ctrl+q", "quit_app", "Quit"),
        Binding("ctrl+c", "cancel_active", "Cancel"),
        Binding("f1", "show_help", "Help"),
        Binding("f2", "show_tasks", "Tasks"),
        Binding("f3", "show_metrics", "Metrics"),
    ]

    def __init__(
        self,
        *,
        container: Any,
        session: SessionRecord,
        settings: Settings,
        workspace: Path,
        approver: TuiApprover,
    ) -> None:
        super().__init__()
        self.container = container
        self.session = session
        self.settings = settings
        self.workspace = workspace
        self.approver = approver
        self._unsubscribe: Any = None
        self._run_task: asyncio.Task[Any] | None = None
        self._job_task: asyncio.Task[Any] | None = None
        self._side_task: asyncio.Task[Any] | None = None
        self._exiting = False
        self._run_status = "idle"
        self._stream_text = ""
        self._stream_thinking = ""
        self._context_info: dict[str, Any] = {}
        self._last_route: str = ""
        self._approval_futures: set[asyncio.Future[ApprovalDecision]] = set()

    # --------------------------------------------------------------- lifecycle

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with Horizontal(id="body"):
            with Vertical(id="main"):
                yield RichLog(id="transcript", markup=True, wrap=True)
                yield Static(id="live")
            with VerticalScroll(id="sidebar"):
                yield Static("Plan", classes="side-title")
                yield Static(id="plan")
                yield Static("Runtime", classes="side-title")
                yield Static(id="runtime")
                yield Static("Extensions", classes="side-title")
                yield Static(id="extensions")
        yield Input(placeholder="Ask Bear-Agent or type /help", id="prompt")
        yield Footer()

    async def on_mount(self) -> None:
        self.approver.attach(self)
        self._unsubscribe = self.container.bus.subscribe(self._on_bus_event)
        self._refresh_header()
        prompt = self.query_one("#prompt", Input)
        prompt.focus()
        self._write(
            Text.assemble(
                ("Bear-Agent ", "bold cyan"),
                ("interactive session. Type /help for commands, /exit to quit.", ""),
            )
        )
        await self._refresh_side()

    async def on_unmount(self) -> None:
        self._exiting = True
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self.approver.detach()
        for future in list(self._approval_futures):
            if not future.done():
                future.set_result(ApprovalDecision(False, ApprovalScope.DENY, "application exited"))
        self._approval_futures.clear()
        try:
            await self._cancel_tasks()
        except BaseException:
            # A cancelled task may re-raise CancelledError; cleanup below must
            # still run and close the Container.
            pass
        finally:
            with contextlib.suppress(Exception):
                await self.container.close()

    async def _cancel_tasks(self) -> None:
        for task in (self._job_task, self._run_task, self._side_task):
            if task is None or task.done():
                continue
            task.cancel()
        active = self._run_task
        if active is not None and not active.done():
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(asyncio.shield(active), timeout=10)
        for task in (self._job_task, self._run_task, self._side_task):
            if task is not None and not task.done():
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(task, timeout=5)

    # --------------------------------------------------------------- rendering

    def _safe_error(self, exc: BaseException) -> str:
        return str(self.container.redactor.redact(f"{type(exc).__name__}: {exc}"))

    def _write(self, content: Text | str) -> None:
        if self._exiting:
            return
        with contextlib.suppress(Exception):
            log = self.query_one("#transcript", RichLog)
            log.write(content if isinstance(content, Text) else Text(content))

    def _update_live(self) -> None:
        if self._exiting:
            return
        try:
            widget = self.query_one("#live", Static)
            if self._stream_text:
                widget.update(Text(self._stream_text[-5_000:]))
            elif self._stream_thinking:
                widget.update(Text("thinking…", style="dim italic"))
            else:
                widget.update("")
        except Exception:
            pass

    def _finish_stream(self) -> None:
        if self._stream_text.strip():
            self._write(Text.assemble(("assistant\n", "bold green"), (self._stream_text, "")))
        self._stream_text = ""
        self._stream_thinking = ""
        self._update_live()

    def _refresh_header(self) -> None:
        if self._exiting:
            return
        context = ""
        estimated = self._context_info.get("estimated_tokens")
        budget = self._context_info.get("budget_tokens")
        if isinstance(estimated, int) and isinstance(budget, int) and budget > 0:
            context = f"  ctx {estimated}/{budget} ({estimated * 100 // budget}%)"
        status = Text.assemble(
            ("Bear-Agent ", "bold cyan"),
            ("v", "dim"),
            (self.app_version(), "dim"),
            ("  session ", "dim"),
            (self.session.id[:18], "cyan"),
            (f"  {self.session.status}", "dim"),
            ("  ", ""),
            (self._run_status, "yellow" if self._run_status != "idle" else "green"),
            (
                f"  {self.settings.agent.default_provider}/{self.settings.agent.default_model}",
                "dim",
            ),
            (f"  permission={self.container.policy.mode}", "dim"),
            (context, "dim"),
        )
        if self._last_route:
            status.append(f"  route={self._last_route}", style="dim")
        with contextlib.suppress(Exception):
            self.query_one("#status", Static).update(status)

    @staticmethod
    def app_version() -> str:
        from bear_agent import __version__

        return __version__

    async def _refresh_side(self) -> None:
        if self._exiting:
            return
        try:
            plan = await self.container.tasks.get_plan(self.session.id)
            tasks = await self.container.tasks.list_tasks(self.session.id, active_only=True)
            checkpoints = await self.container.checkpoints.list(self.session.id, limit=5)
        except Exception as exc:
            self._set_side("plan", f"(unavailable: {type(exc).__name__})")
            return
        plan_lines: list[str] = []
        if plan is None:
            plan_lines.append("no plan")
        else:
            plan_lines.append(f"{plan.status.value}  {plan.goal[:80]}")
            plan_lines.append(f"version {plan.version}  tasks {len(tasks)}")
            for task in tasks[:20]:
                deps = await self.container.tasks.dependencies(task.id)
                dep_text = f" deps={','.join(deps)}" if deps else ""
                plan_lines.append(
                    f"{task.task_key}  {task.status.value}{dep_text}  {task.title[:48]}"
                )
        self._set_side("plan", "\n".join(plan_lines))
        estimated = self._context_info.get("estimated_tokens")
        budget = self._context_info.get("budget_tokens")
        runtime_lines = [
            f"run: {self._run_status}",
            f"session: {self.session.status}",
            f"checkpoints: {len(checkpoints)}",
            f"context: {estimated if estimated is not None else '?'}"
            f" / {budget if budget is not None else '?'} tokens",
        ]
        if self._context_info.get("compacted"):
            runtime_lines.append(f"compacted: {self._context_info.get('levels')}")
        if self._last_route:
            runtime_lines.append(f"provider route: {self._last_route}")
        self._set_side("runtime", "\n".join(runtime_lines))
        extension_lines: list[str] = []
        manager = self.container.mcp
        if manager is None:
            extension_lines.append("MCP: none")
        else:
            statuses = manager.status()
            if not statuses:
                extension_lines.append("MCP: configured, not started")
            for name, status in sorted(statuses.items()):
                extension_lines.append(f"MCP {name}: {status.state}  tools={len(status.tools)}")
        report = self.container.plugin_report
        if report is None:
            extension_lines.append("Plugins: not loaded")
        else:
            extension_lines.append(
                f"Plugins: {len(report.loaded)} loaded, {len(report.failed)} failed"
            )
        hooks = self.container.hooks.status()
        extension_lines.append(f"Hooks: {len(hooks)} registered")
        self._set_side("extensions", "\n".join(extension_lines))

    def _set_side(self, name: str, value: str) -> None:
        if self._exiting:
            return
        with contextlib.suppress(Exception):
            self.query_one(f"#{name}", Static).update(Text(value))

    def _schedule_side_refresh(self) -> None:
        if self._exiting:
            return
        if self._side_task is None or self._side_task.done():
            self._side_task = asyncio.create_task(self._refresh_side())

    # --------------------------------------------------------------- event bus

    async def _on_bus_event(self, event: Event) -> None:
        if self._exiting:
            return
        try:
            self.post_message(TuiEvent(event))
        except Exception:
            # Rendering must never affect the runtime or event bus.
            return

    @on(TuiEvent)
    def _handle_event(self, message: TuiEvent) -> None:
        event = message.event
        payload = event.payload
        if event.type == EventType.MODEL_TEXT_DELTA:
            self._stream_text += str(payload.get("text") or "")
            self._update_live()
        elif event.type == EventType.MODEL_THINKING_DELTA:
            self._stream_thinking += str(payload.get("text") or "")
            if not self._stream_text:
                self._update_live()
        elif event.type == EventType.MODEL_RESPONSE:
            self._finish_stream()
            self._context_info["last_usage"] = payload.get("usage") or {}
            self._last_route = f"{payload.get('provider', '')}/{payload.get('model', '')}"
            self._refresh_header()
        elif event.type == EventType.TURN_STARTED:
            self._run_status = "thinking"
            self._refresh_header()
        elif event.type == EventType.TURN_COMPLETED:
            self._run_status = "running"
            self._refresh_header()
        elif event.type == EventType.RUN_STARTED:
            self._run_status = "running"
            self._finish_stream()
            self._refresh_header()
            self._schedule_side_refresh()
        elif event.type in (EventType.RUN_COMPLETED, EventType.RUN_CANCELLED, EventType.RUN_FAILED):
            self._finish_stream()
            status = str(payload.get("status") or event.type.value)
            self._run_status = "idle" if status == "completed" else status
            reason = str(payload.get("reason") or "")
            style = "dim" if status == "completed" else "yellow" if status == "cancelled" else "red"
            self._write(Text(f"run {status}: {reason}", style=style))
            self._refresh_header()
            self._schedule_side_refresh()
        elif event.type == EventType.TOOL_CALL_STARTED:
            self._finish_stream()
            name = str(payload.get("name") or "?")
            preview = str(payload.get("arguments_preview") or "")
            line = Text.assemble(("tool ", "bold cyan"), (name, "cyan"))
            if preview:
                line.append(f"  {preview[:300]}", style="dim")
            self._write(line)
        elif event.type == EventType.TOOL_FINISHED:
            status = str(payload.get("status") or "?")
            duration = payload.get("duration_ms")
            suffix = f" ({duration} ms)" if isinstance(duration, int) else ""
            style = "green" if status == "completed" else "red"
            self._write(Text(f"  result: {status}{suffix}", style=style))
        elif event.type in (EventType.TOOL_CALL_COMPLETED, EventType.TOOL_CALL_FAILED):
            preview = str(payload.get("content_preview") or "")
            if preview:
                self._write(Text(f"  {preview[:300]}", style="dim"))
        elif event.type in (EventType.POLICY_DECISION,):
            decision = str(payload.get("decision") or "")
            if decision != "allow":
                self._write(
                    Text(
                        f"policy: {decision}  {payload.get('reason') or ''}",
                        style="yellow" if decision == "ask" else "red",
                    )
                )
        elif event.type in (EventType.PROVIDER_RETRY, EventType.MODEL_RETRY):
            self._write(
                Text(
                    f"provider retry: {payload.get('category') or payload.get('reason') or ''}",
                    style="yellow",
                )
            )
        elif event.type == EventType.PROVIDER_FALLBACK:
            self._last_route = str(payload.get("to") or self._last_route)
            self._write(
                Text(
                    f"provider fallback: {payload.get('from')} -> {payload.get('to')} "
                    f"({payload.get('category', '')})",
                    style="yellow",
                )
            )
            self._refresh_header()
        elif event.type == EventType.PROVIDER_SELECTED:
            self._last_route = f"{payload.get('provider', '')}/{payload.get('model', '')}"
            self._refresh_header()
        elif event.type in (EventType.PROVIDER_CIRCUIT_OPEN, EventType.PROVIDER_FAILED):
            self._write(Text(f"{event.type.value}: {payload}", style="red"))
        elif event.type == EventType.CONTEXT_BUILT:
            self._context_info.update(
                {
                    "estimated_tokens": payload.get("estimated_tokens"),
                    "budget_tokens": payload.get("budget_tokens"),
                    "compacted": payload.get("compacted"),
                    "levels": payload.get("compaction_levels"),
                }
            )
            self._refresh_header()
            self._schedule_side_refresh()
        elif event.type == EventType.CONTEXT_COMPACTED:
            self._write(
                Text(
                    f"context compacted L{payload.get('level')}: "
                    f"{payload.get('before_tokens')} -> {payload.get('after_tokens')} tokens",
                    style="dim",
                )
            )
        elif event.type == EventType.CONTEXT_COMPACTION_FAILED:
            self._write(
                Text(f"context compaction failed: {payload.get('reason', '')}", style="yellow")
            )
        elif event.type == EventType.CHECKPOINT_CREATED:
            self._write(Text(f"checkpoint {payload.get('kind', '')} created", style="dim"))
            self._schedule_side_refresh()
        elif event.type == EventType.RECOVERY_STARTED:
            self._write(Text("recovery started", style="yellow"))
        elif event.type in (EventType.RECOVERY_COMPLETED, EventType.RECOVERY_FAILED):
            self._write(Text(f"recovery {event.type.value.split('.')[-1]}", style="yellow"))
        elif event.type == EventType.TASK_RUN_STARTED:
            self._run_status = "plan"
            self._write(Text("plan execution started", style="bold cyan"))
            self._refresh_header()
            self._schedule_side_refresh()
        elif event.type == EventType.TASK_STARTED:
            self._write(
                Text(
                    f"task {payload.get('task_key', '?')}: {payload.get('title', '')}"
                    f" (attempt {payload.get('attempt', '?')})",
                    style="cyan",
                )
            )
            self._schedule_side_refresh()
        elif event.type == EventType.TASK_COMPLETED:
            self._write(Text(f"task {payload.get('task_key', '?')} completed", style="green"))
            self._schedule_side_refresh()
        elif event.type in (
            EventType.TASK_FAILED,
            EventType.TASK_BLOCKED,
            EventType.TASK_CANCELLED,
        ):
            self._write(
                Text(
                    f"task {payload.get('task_key', '?')} {event.type.value.split('.')[-1]}",
                    style="red",
                )
            )
            self._schedule_side_refresh()
        elif event.type == EventType.TASK_PLAN_REVISED:
            self._write(Text(f"plan revised v{payload.get('version', '?')}", style="cyan"))
            self._schedule_side_refresh()
        elif event.type in (
            EventType.TASK_PLAN_COMPLETED,
            EventType.TASK_PLAN_BLOCKED,
            EventType.TASK_PLAN_FAILED,
        ):
            self._write(Text(f"plan {event.type.value.split('.')[-1]}", style="dim"))
            self._schedule_side_refresh()
        elif event.type == EventType.HOOK_FAILED:
            self._write(Text(f"hook failed: {payload.get('event', '')}", style="yellow"))
        elif event.type == EventType.HOOK_DENIED:
            self._write(Text(f"hook denied: {payload.get('event', '')}", style="yellow"))
        elif event.type == EventType.MCP_SERVER_FAILED:
            self._write(Text(f"MCP server failed: {payload.get('server', '')}", style="red"))
            self._schedule_side_refresh()
        elif event.type in (EventType.MCP_SERVER_STARTED, EventType.MCP_SERVER_RECONNECTED):
            self._write(Text(f"MCP server {payload.get('server', '')} ready", style="dim"))
            self._schedule_side_refresh()
        elif event.type == EventType.PLUGIN_FAILED:
            self._write(Text(f"plugin failed: {payload.get('plugin', '')}", style="red"))
        elif event.type == EventType.APPROVAL_REQUESTED:
            self._write(Text(f"approval requested: {payload.get('tool', '')}", style="yellow"))
        elif event.type == EventType.APPROVAL_DECIDED:
            approved = payload.get("approved")
            self._write(
                Text(
                    f"approval {'approved' if approved else 'denied'}: {payload.get('scope', '')}",
                    style="green" if approved else "red",
                )
            )

    # --------------------------------------------------------------- approvals

    def submit_approval(
        self, request: ApprovalRequest, future: asyncio.Future[ApprovalDecision]
    ) -> None:
        if self._exiting:
            future.set_result(ApprovalDecision(False, ApprovalScope.DENY, "application exited"))
            return
        arguments = self.container.redactor.redact_obj(request.arguments)
        reason = self.container.redactor.redact(request.reason)
        safe = ApprovalRequest(
            tool_name=request.tool_name,
            arguments=arguments if isinstance(arguments, dict) else {},
            reason=reason,
            risk=request.risk,
            session_id=request.session_id,
            project_id=request.project_id,
            fingerprint=request.fingerprint,
            scope_options=request.scope_options,
        )
        self._approval_futures.add(future)
        screen = ApprovalScreen(safe)
        callback = approval_callback(future)

        def done(decision: ApprovalDecision | None) -> None:
            self._approval_futures.discard(future)
            callback(decision)

        try:
            self.push_screen(screen, done)
        except Exception:
            self._approval_futures.discard(future)
            if not future.done():
                future.set_result(ApprovalDecision(False, ApprovalScope.DENY, "approval UI failed"))

    # --------------------------------------------------------------- input

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        event.input.value = ""
        if not value:
            return
        if value.startswith("/"):
            try:
                await self._dispatch_command(value)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._write(Text(f"command failed: {self._safe_error(exc)}", style="red"))
            finally:
                with contextlib.suppress(Exception):
                    self.query_one("#prompt", Input).focus()
            return
        if self._busy:
            self._write(Text("a run or job is active; use /cancel first", style="yellow"))
            return
        self._write(Text.assemble(("you\n", "bold cyan"), (value, "")))
        self._run_task = asyncio.create_task(self._run_conversation(value))

    @property
    def _busy(self) -> bool:
        return any(
            task is not None and not task.done() for task in (self._run_task, self._job_task)
        )

    async def _run_conversation(self, text: str) -> None:
        self._run_status = "running"
        self._refresh_header()
        try:
            result = await self.container.runtime.run(self.session.id, text)
            if not self._exiting:
                self._run_status = (
                    "idle" if result.status == RunStatus.COMPLETED else result.status.value
                )
                if result.status != RunStatus.COMPLETED:
                    self._write(
                        Text(
                            f"{result.status.value}: {result.stop_reason}",
                            style="yellow",
                        )
                    )
        except asyncio.CancelledError:
            if not self._exiting:
                self._write(Text("run cancelled", style="yellow"))
            raise
        except BearError as exc:
            if not self._exiting:
                self._write(Text(self._safe_error(exc), style="red"))
        except Exception as exc:
            if not self._exiting:
                self._write(Text(f"internal error: {self._safe_error(exc)}", style="red"))
        finally:
            if not self._exiting:
                with contextlib.suppress(Exception):
                    refreshed = await self.container.sessions.get(self.session.id)
                    if refreshed is not None:
                        self.session = refreshed
                if self._run_status != "cancelled":
                    self._run_status = "idle"
                self._refresh_header()
                self._schedule_side_refresh()
            self._run_task = None

    async def _run_plan(self) -> None:
        self._run_status = "plan"
        self._refresh_header()
        try:
            report = await self.container.runtime.run_plan(self.session.id)
            if not self._exiting:
                self._run_status = "idle"
                self._write(
                    Text(
                        f"plan {report.status} | run={report.tasks_run} "
                        f"completed={report.tasks_completed} failed={report.tasks_failed} "
                        f"blocked={report.tasks_blocked} revisions={report.revisions}",
                        style="bold" if report.status == "completed" else "yellow",
                    )
                )
                for issue in report.issues:
                    self._write(Text(f"issue: {issue}", style="yellow"))
        except asyncio.CancelledError:
            if not self._exiting:
                self._write(Text("plan run cancelled", style="yellow"))
            raise
        except Exception as exc:
            if not self._exiting:
                self._write(Text(f"plan run failed: {self._safe_error(exc)}", style="red"))
        finally:
            if not self._exiting:
                with contextlib.suppress(Exception):
                    refreshed = await self.container.sessions.get(self.session.id)
                    if refreshed is not None:
                        self.session = refreshed
                self._run_status = "idle"
                self._refresh_header()
                self._schedule_side_refresh()
            self._run_task = None

    # --------------------------------------------------------------- commands

    async def _dispatch_command(self, text: str) -> None:
        if text in {"/help", "/h"}:
            self._write(Text(HELP_TEXT))
            return
        if text in {"/exit", "/quit"}:
            self._write(Text("bye", style="dim"))
            self.exit()
            return
        if text == "/cancel":
            self._cancel_active()
            return
        if text == "/session":
            record = await self.container.sessions.get(self.session.id) or self.session
            self.session = record
            self._write(
                Text(
                    f"session {record.id} status={record.status} seq={record.last_seq} "
                    f"title={record.title or '(none)'}",
                    style="dim",
                )
            )
            self._refresh_header()
            return
        if text == "/sessions":
            await self._show_sessions()
            return
        if text.startswith("/resume "):
            await self._resume(text[len("/resume ") :].strip())
            return
        if text == "/new":
            await self._new_session()
            return
        if text == "/fork" or text.startswith("/fork "):
            await self._fork(text)
            return
        if text.startswith("/plan "):
            await self._start_job(self._plan_job(text[len("/plan ") :].strip()), "plan generation")
            return
        if text == "/tasks":
            await self._refresh_side()
            self._write(Text("task panel refreshed", style="dim"))
            return
        if text == "/run-plan":
            if self._busy:
                self._write(Text("a run or job is active; use /cancel first", style="yellow"))
                return
            self._run_task = asyncio.create_task(self._run_plan())
            return
        if text.startswith("/task "):
            await self._show_task(text[len("/task ") :].strip())
            return
        if text == "/cancel-plan":
            await self._start_job(self._cancel_plan_job(), "cancel plan")
            return
        if text == "/compact":
            await self._start_job(self._compact_job(), "context compaction")
            return
        if text.startswith("/anchor "):
            await self._start_job(self._anchor_job(text[len("/anchor ") :].strip()), "anchor")
            return
        if text == "/anchors":
            await self._show_anchors()
            return
        if text == "/checkpoints":
            await self._show_checkpoints()
            return
        if text.startswith("/run "):
            prompt = text[len("/run ") :].strip()
            if prompt:
                if self._busy:
                    self._write(Text("a run or job is active; use /cancel first", style="yellow"))
                    return
                self._write(Text.assemble(("you\n", "bold cyan"), (prompt, "")))
                self._run_task = asyncio.create_task(self._run_conversation(prompt))
            return
        if text.startswith("/permission"):
            if self._busy:
                self._write(Text("cannot change permission mode during a run/job", style="yellow"))
                return
            parts = text.split()
            if len(parts) != 2:
                self._write(
                    Text("usage: /permission plan|supervised|auto|unrestricted", style="dim")
                )
                return
            try:
                from bear_agent.bootstrap import permission_mode

                mode = permission_mode(parts[1])
            except ValueError as exc:
                self._write(Text(str(exc), style="red"))
                return
            self.container.policy.set_mode(mode)
            self._write(Text(f"permission mode: {mode.value}", style="dim"))
            self._refresh_header()
            return
        if text == "/tools":
            for name in self.container.registry.names():
                tool = self.container.registry.get(name)
                if tool is not None:
                    self._write(
                        Text.assemble(
                            (f"{name}  ", "cyan"),
                            (f"({tool.side_effect.value}) ", "dim"),
                            (tool.description, ""),
                        )
                    )
            return
        if text == "/metrics":
            self._show_metrics()
            return
        if text == "/trace":
            await self._show_trace()
            return
        if text == "/status":
            await self._refresh_side()
            self._refresh_header()
            self._write(Text("status refreshed", style="dim"))
            return
        if text == "/mcp":
            await self._show_mcp()
            return
        if text == "/plugins":
            self._show_plugins()
            return
        if text == "/hooks":
            self._show_hooks()
            return
        if text == "/doctor":
            await self._start_job(self._doctor_job(), "diagnostics")
            return
        self._write(Text(f"unknown command: {text}", style="red"))

    # --------------------------------------------------------------- commands helpers

    async def _show_sessions(self) -> None:
        records = await self.container.sessions.list_recent(20)
        for record in records:
            marker = "*" if record.id == self.session.id else " "
            self._write(
                Text(
                    f"{marker} {record.id}  {record.status:8s} "
                    f"{record.updated_at.strftime('%Y-%m-%d %H:%M')}  "
                    f"{record.title or '(untitled)'}"
                )
            )

    async def _resume(self, session_id: str) -> None:
        if self._busy:
            self._write(Text("cannot switch sessions while a run/job is active", style="yellow"))
            return
        record = await self.container.sessions.get(session_id)
        if record is None:
            self._write(Text(f"session not found: {session_id}", style="red"))
            return
        self.session = record
        self._context_info.clear()
        self._write(Text(f"resumed session {record.id}", style="green"))
        self._refresh_header()
        await self._refresh_side()

    async def _new_session(self) -> None:
        if self._busy:
            self._write(Text("cannot switch sessions while a run/job is active", style="yellow"))
            return
        self.session = await self.container.sessions.create(
            cwd=self.workspace,
            model=self.settings.agent.default_model,
            provider=self.settings.agent.default_provider,
            publish=self.container.bus.publish,
        )
        self._context_info.clear()
        self._write(Text(f"new session {self.session.id}", style="green"))
        self._refresh_header()
        await self._refresh_side()

    async def _fork(self, text: str) -> None:
        if self._busy:
            self._write(Text("cannot fork while a run/job is active", style="yellow"))
            return
        at: int | None = None
        if text.startswith("/fork "):
            try:
                at = int(text[len("/fork ") :].strip())
            except ValueError:
                self._write(Text("usage: /fork [message_seq]", style="dim"))
                return
        try:
            async with self.container.tracer.trace(
                "sessions.fork",
                kind="fork",
                session_id=self.session.id,
                attributes={"at_message_seq": at},
            ) as scope:
                result = await self.container.fork.fork(
                    self.session.id,
                    at_message_seq=at,
                    checkpoints=self.container.checkpoints,
                )
                if scope is not None:
                    scope.set_attribute("session_id", result.session.id)
        except BearError as exc:
            self._write(Text(f"fork failed: {self._safe_error(exc)}", style="red"))
            return
        self.session = result.session
        self._context_info.clear()
        self._write(
            Text(
                f"forked {result.session.id} at message {result.cut_message_seq}",
                style="green",
            )
        )
        self._refresh_header()
        await self._refresh_side()

    async def _show_task(self, task_id: str) -> None:
        task = await self.container.tasks.get_task(task_id)
        if task is None:
            self._write(Text(f"task not found: {task_id}", style="red"))
            return
        deps = await self.container.tasks.dependencies(task_id)
        self._write(
            Text(
                f"{task.task_key}  {task.title}\n"
                f"status={task.status.value} attempts={task.attempt_count}/{task.max_attempts}\n"
                f"deps={', '.join(deps) or '(none)'}\n"
                f"acceptance: {task.acceptance_criteria or '(none)'}\n"
                f"result: {task.result_summary or '(none)'}\n"
                f"error: {task.last_error or '(none)'}",
                style="dim",
            )
        )

    async def _show_anchors(self) -> None:
        anchors = await self.container.anchors.active(self.session.id)
        if not anchors:
            self._write(Text("no active anchors", style="dim"))
        for anchor in anchors:
            self._write(Text(f"{anchor.id}  [{anchor.kind}]  {anchor.text[:300]}"))

    async def _show_checkpoints(self) -> None:
        records = await self.container.checkpoints.list(self.session.id, limit=20)
        if not records:
            self._write(Text("no checkpoints", style="dim"))
        for record in records:
            self._write(
                Text(
                    f"{record.id}  {record.kind}  event={record.event_seq} "
                    f"message={record.message_seq}  {record.created_at.isoformat()}"
                )
            )

    def _show_metrics(self) -> None:
        snapshot = self.container.metrics_registry.snapshot()
        if not snapshot:
            self._write(Text("no live metrics yet", style="dim"))
            return
        for name in sorted(snapshot):
            family = snapshot[name]
            total = 0.0
            if family["kind"] == "histogram":
                total = sum(int(item["histogram"]["count"]) for item in family["series"])
            else:
                total = sum(float(item["value"]) for item in family["series"])
            self._write(Text(f"{name}: {total:g}"))

    async def _show_trace(self) -> None:
        traces = await self.container.tracer.find_by_session(self.session.id, limit=5)
        if not traces:
            self._write(Text("no traces for this session", style="dim"))
            return
        for trace in traces:
            self._write(
                Text(
                    f"{trace['trace_id']}  {trace['kind']}  {trace['status']}  "
                    f"{trace['duration_ms']} ms  spans={trace['span_count']}"
                )
            )

    async def _show_mcp(self) -> None:
        manager = self.container.mcp
        if manager is None:
            self._write(Text("MCP: no manager (not configured or not started)", style="dim"))
            return
        statuses = manager.status()
        if not statuses:
            self._write(Text("MCP: no enabled servers", style="dim"))
        for name, status in sorted(statuses.items()):
            self._write(
                Text(
                    f"{name}: {status.state}  tools={len(status.tools)}"
                    + (f"  error={status.last_error}" if status.last_error else "")
                )
            )

    def _show_plugins(self) -> None:
        report = self.container.plugin_report
        if report is None:
            self._write(Text("plugins: not loaded", style="dim"))
            return
        self._write(
            Text(
                f"loaded={report.loaded} skipped={report.skipped} "
                f"failed={report.failed} conflicts={report.conflicts}",
                style="dim",
            )
        )

    def _show_hooks(self) -> None:
        hooks = self.container.hooks.status()
        if not hooks:
            self._write(Text("no hooks registered", style="dim"))
        for hook in hooks:
            self._write(
                Text(
                    f"{hook.get('id')}  {','.join(hook.get('events') or [])}  "
                    f"{hook.get('source', '')}"
                )
            )

    # --------------------------------------------------------------- jobs

    async def _start_job(self, coro: Any, label: str) -> None:
        if self._busy:
            self._write(Text("a run or job is active; use /cancel first", style="yellow"))
            coro.close()
            return
        self._write(Text(f"{label}…", style="dim"))

        async def run() -> None:
            self._job_task = asyncio.current_task()
            try:
                await coro
            except asyncio.CancelledError:
                if not self._exiting:
                    self._write(Text(f"{label} cancelled", style="yellow"))
                raise
            except Exception as exc:
                if not self._exiting:
                    self._write(Text(f"{label} failed: {self._safe_error(exc)}", style="red"))
            finally:
                self._job_task = None
                if not self._exiting:
                    self._schedule_side_refresh()

        self._job_task = asyncio.create_task(run())

    async def _plan_job(self, goal: str) -> None:
        if not goal:
            self._write(Text("usage: /plan <goal>", style="dim"))
            return
        if await self.container.tasks.get_plan(self.session.id) is not None:
            self._write(Text("this session already has a plan; use /tasks", style="yellow"))
            return
        owner = f"tui-{os.getpid()}"
        epoch = await self.container.sessions.acquire_lease(self.session.id, owner, 900.0)
        fence = LeaseFence(owner, epoch)
        token = set_fence(fence)
        try:
            draft = await self.container.planner.generate(
                goal, context=f"Workspace: {self.workspace}"
            )
            created = await self.container.tasks.create_plan(
                self.session.id, draft, source="model", fence=fence
            )
            if self.session.title is None:
                await self.container.sessions.set_title(
                    self.session.id, " ".join(goal.split())[:80] or "Task plan", fence=fence
                )
        finally:
            reset_fence(token)
            with contextlib.suppress(Exception):
                await self.container.sessions.release_lease(self.session.id, owner, epoch)
        self._write(
            Text(
                f"plan {created.id} v{created.version} with {len(draft.tasks)} task(s)",
                style="green",
            )
        )
        await self._refresh_side()

    async def _cancel_plan_job(self) -> None:
        owner = f"tui-{os.getpid()}"
        epoch = await self.container.sessions.acquire_lease(self.session.id, owner, 60.0)
        fence = LeaseFence(owner, epoch)
        token = set_fence(fence)
        try:
            plan = await self.container.tasks.cancel_plan(
                self.session.id, reason="cancelled by user", fence=fence
            )
        finally:
            reset_fence(token)
            with contextlib.suppress(Exception):
                await self.container.sessions.release_lease(self.session.id, owner, epoch)
        self._write(Text("plan cancelled" if plan else "no plan to cancel", style="yellow"))
        await self._refresh_side()

    async def _compact_job(self) -> None:
        owner = f"tui-{os.getpid()}"
        epoch = await self.container.sessions.acquire_lease(self.session.id, owner, 900.0)
        fence = LeaseFence(owner, epoch)
        token = set_fence(fence)
        try:
            session = await self.container.sessions.get(self.session.id) or self.session
            messages = await self.container.sessions.load_messages(self.session.id)
            prepared = await self.container.compactor.force(
                session,
                messages,
                self.container.registry.specs(),
                fence=fence,
                turn_index=0,
            )
        finally:
            reset_fence(token)
            with contextlib.suppress(Exception):
                await self.container.sessions.release_lease(self.session.id, owner, epoch)
        self._write(
            Text(
                f"compacted levels={prepared.levels or 'none'} "
                f"tokens {prepared.before_tokens} -> {prepared.estimated_tokens}",
                style="green",
            )
        )
        for note in prepared.notes:
            self._write(Text(note, style="dim"))
        self._context_info.update(
            {
                "estimated_tokens": prepared.estimated_tokens,
                "compacted": prepared.compacted,
                "levels": prepared.levels,
            }
        )
        self._refresh_header()
        await self._refresh_side()

    async def _anchor_job(self, text: str) -> None:
        if not text:
            self._write(Text("usage: /anchor <text>", style="dim"))
            return
        owner = f"tui-{os.getpid()}"
        epoch = await self.container.sessions.acquire_lease(self.session.id, owner, 60.0)
        fence = LeaseFence(owner, epoch)
        token = set_fence(fence)
        try:
            anchor = await self.container.anchors.add(
                self.session.id, text, kind="manual", fence=fence
            )
        finally:
            reset_fence(token)
            with contextlib.suppress(Exception):
                await self.container.sessions.release_lease(self.session.id, owner, epoch)
        self._write(Text(f"anchor {anchor.id} pinned", style="green"))
        await self._refresh_side()

    async def _doctor_job(self) -> None:
        report = await DiagnosticsService(self.settings, cwd=self.workspace).run()
        counts = report.counts
        self._write(
            Text(
                f"doctor: {counts['pass']} pass, {counts['warn']} warn, "
                f"{counts['fail']} fail, {counts['skip']} skip",
                style="bold" if report.ok else "red",
            )
        )
        for check in report.checks:
            if check.status.value in {"fail", "warn"}:
                self._write(
                    Text(
                        f"{check.id}: {check.status.value}  {check.summary}"
                        + (f" — {check.detail}" if check.detail else ""),
                        style="yellow" if check.status.value == "warn" else "red",
                    )
                )

    # --------------------------------------------------------------- actions

    def _cancel_active(self) -> None:
        if isinstance(self.screen, ApprovalScreen):
            self.approver.cancel_pending("run cancelled")
            with contextlib.suppress(Exception):
                self.screen.dismiss(ApprovalDecision(False, ApprovalScope.DENY, "run cancelled"))
        if self._job_task is not None and not self._job_task.done():
            self._job_task.cancel()
            self._write(Text("cancelling job…", style="yellow"))
            return
        if self._run_task is not None and not self._run_task.done():
            self.container.runtime.cancel(self.session.id)
            self._write(Text("cancelling run…", style="yellow"))
            return
        self._write(Text("nothing to cancel", style="dim"))

    async def action_quit_app(self) -> None:
        self._write(Text("bye", style="dim"))
        self.exit()

    def action_cancel_active(self) -> None:
        self._cancel_active()

    def action_show_help(self) -> None:
        self._write(Text(HELP_TEXT))

    async def action_show_tasks(self) -> None:
        await self._refresh_side()
        self._write(Text("task panel refreshed", style="dim"))

    def action_show_metrics(self) -> None:
        self._show_metrics()
