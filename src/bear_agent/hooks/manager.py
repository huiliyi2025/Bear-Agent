"""Hook registry, ordered dispatch and observation bus adapter.

Security properties:

* Registration grants capabilities explicitly. A hook cannot deny unless it was
  registered for exactly ``tool_before`` with ``can_deny``; it cannot append
  model context unless registered for exactly ``model_before`` with
  ``can_append_context``. Violations are ignored and recorded, not honoured.
* Dispatch is deterministic: priority DESC, then registration order.
* Fail modes are per hook. ``closed`` failures deny ``tool_before`` calls and
  mark the outcome ``failed_closed``; observation hooks never break a run.
* Recursion is stopped with a ContextVar stack (same event re-entry) and a
  configurable maximum nesting depth.
* Cancellation (``asyncio.CancelledError`` / ``RunCancelled``) is never
  swallowed. Timeouts are.
* Bookkeeping uses the persisted ``HOOK_*`` events; those events are excluded
  from bus-to-hook mapping, so subscribing ``on_bus_event`` cannot recurse.
"""

from __future__ import annotations

import asyncio
import builtins
import logging
import shutil
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from bear_agent.audit.service import AuditService
from bear_agent.config.models import HookCommandConfig, HooksConfig
from bear_agent.core.cancel import RunCancelled
from bear_agent.core.errors import HookError, HookTimeout
from bear_agent.core.events import Event, EventType
from bear_agent.core.fencing import LeaseFence
from bear_agent.hooks.command import run_command_hook
from bear_agent.hooks.events import HookEvent
from bear_agent.hooks.models import (
    HookDecision,
    HookExecution,
    HookInvocation,
    HookOutcome,
    HookRegistration,
    HookStatus,
)
from bear_agent.observability.recorder import EventRecorder
from bear_agent.secrets.redaction import SecretRedactor

logger = logging.getLogger(__name__)

_emit_stack: ContextVar[tuple[HookEvent, ...]] = ContextVar("bear_hook_emit_stack", default=())

_DENIAL_NOTE = "deny ignored: hook is not authorized to deny this event"
_CONTEXT_NOTE = (
    "context_append ignored: only model_before hooks with can_append_context may append context"
)
_DEFAULT_TIMEOUT_S = 5.0


class HookManager:
    def __init__(
        self,
        *,
        config: HooksConfig,
        recorder: EventRecorder | None = None,
        audit: AuditService | None = None,
        redactor: SecretRedactor | None = None,
        workspace: Path | None = None,
        safe_cwd: Path | None = None,
    ) -> None:
        self.config = config
        self.recorder = recorder
        self.audit = audit
        self.redactor = redactor or SecretRedactor()
        self.workspace = (workspace or Path.cwd()).resolve()
        # External commands must not execute from an agent-writable workspace by
        # default; operators can use absolute paths and the workspace is passed
        # to the hook as data.
        self.safe_cwd = (safe_cwd or Path.home()).resolve()
        self._registrations: builtins.list[HookRegistration] = []
        self._order: dict[str, int] = {}
        self._counter = 0
        self._command_errors: builtins.list[dict[str, Any]] = []
        self._load_command_hooks()

    # ------------------------------------------------------------ registration

    def register(self, registration: HookRegistration) -> HookRegistration:
        if registration.id in self._order:
            raise HookError(f"duplicate hook id: {registration.id!r}")
        self._counter += 1
        self._order[registration.id] = self._counter
        self._registrations.append(registration)
        return registration

    def register_many(self, registrations: builtins.list[HookRegistration]) -> None:
        known = set(self._order)
        seen: set[str] = set()
        for registration in registrations:
            if registration.id in known or registration.id in seen:
                raise HookError(f"duplicate hook id: {registration.id!r}")
            seen.add(registration.id)
        for registration in registrations:
            self.register(registration)

    def unregister(self, hook_id: str) -> bool:
        for index, registration in enumerate(self._registrations):
            if registration.id == hook_id:
                del self._registrations[index]
                self._order.pop(hook_id, None)
                return True
        return False

    def unregister_source(self, source: str) -> int:
        removed = [reg for reg in self._registrations if reg.source == source]
        for registration in removed:
            self.unregister(registration.id)
        return len(removed)

    def list(self) -> builtins.list[dict[str, Any]]:
        return [self._describe(reg) for reg in self._ordered()]

    def status(self) -> builtins.list[dict[str, Any]]:
        entries = self.list()
        entries.extend(dict(error) for error in self._command_errors)
        return entries

    def _ordered(self) -> builtins.list[HookRegistration]:
        return sorted(
            self._registrations,
            key=lambda reg: (-reg.priority, self._order.get(reg.id, 0)),
        )

    def _describe(self, registration: HookRegistration) -> dict[str, Any]:
        return {
            "id": registration.id,
            "kind": "command" if registration.source.startswith("command:") else "programmatic",
            "events": sorted(event.value for event in registration.events),
            "priority": registration.priority,
            "timeout_s": registration.timeout_s,
            "fail_mode": registration.fail_mode,
            "can_deny": registration.can_deny,
            "can_append_context": registration.can_append_context,
            "source": registration.source,
        }

    # ------------------------------------------------------- external commands

    def _load_command_hooks(self) -> None:
        commands = self.config.commands
        if not commands:
            return
        if not self.config.enabled:
            for command in commands:
                self._command_error(command, "hooks are disabled", status="disabled")
            return
        if not self.config.allow_external:
            for command in commands:
                logger.warning(
                    "external hook command %s ignored: hooks.allow_external is false",
                    command.id,
                )
                self._command_error(
                    command,
                    "external hooks are disabled (hooks.allow_external=false)",
                )
            return
        for command in commands:
            self._register_command(command)

    def register_command(self, command: HookCommandConfig) -> HookRegistration:
        """Register one external command hook added at runtime (e.g. a plugin)."""
        if not self.config.enabled:
            raise HookError("hooks are disabled")
        if not self.config.allow_external:
            raise HookError("external hook commands require hooks.allow_external=true")
        before = len(self._registrations)
        self._register_command(command)
        if len(self._registrations) == before:
            reason = (
                self._command_errors[-1].get("error", "unknown error")
                if self._command_errors
                else "unknown error"
            )
            raise HookError(f"cannot register command hook {command.id!r}: {reason}")
        return self._registrations[-1]

    def _register_command(self, command: HookCommandConfig) -> None:
        if not command.enabled:
            self._command_error(command, "command hook is disabled", status="disabled")
            return
        executable = Path(command.command)
        available = (
            executable.is_file()
            if executable.is_absolute()
            else shutil.which(command.command) is not None
        )
        if not available:
            self._command_error(
                command,
                f"hook command is not available: {command.command!r}",
            )
            raise HookError(
                f"command hook {command.id!r} executable is not available: {command.command!r}"
            )
        try:
            event = HookEvent.from_string(command.event)
        except ValueError as exc:
            message = self.redactor.redact(str(exc))
            self._command_error(command, message)
            raise HookError(f"command hook {command.id!r} has an invalid event: {message}") from exc

        async def handler(invocation: HookInvocation) -> HookDecision:
            return await run_command_hook(
                command,
                invocation,
                workspace=self.workspace,
                safe_cwd=self.safe_cwd,
                redactor=self.redactor,
            )

        registration = HookRegistration(
            id=f"cmd:{command.id}",
            events=frozenset({event}),
            handler=handler,
            # The runner enforces the real timeout and kills the process tree;
            # the extra half second lets it do so before the manager cancels it.
            timeout_s=command.timeout_s + 0.5,
            fail_mode=command.fail_mode,
            can_deny=event is HookEvent.TOOL_BEFORE,
            can_append_context=False,
            source=f"command:{command.id}",
        )
        try:
            self.register(registration)
        except HookError as exc:
            self._command_error(command, self.redactor.redact(str(exc)))

    def _command_error(
        self,
        command: HookCommandConfig,
        message: str,
        *,
        status: str = "error",
    ) -> None:
        self._command_errors.append(
            {
                "id": f"cmd:{command.id}",
                "kind": "command",
                "status": status,
                "event": command.event,
                "command": self.redactor.redact(command.command),
                "error": self.redactor.redact(message),
            }
        )

    # ------------------------------------------------------------------- emit

    async def emit(
        self,
        event: HookEvent,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        turn_id: str | None = None,
        payload: dict[str, Any] | None = None,
        fence: LeaseFence | None = None,
    ) -> HookOutcome:
        if not self.config.enabled:
            return HookOutcome()
        stack = _emit_stack.get()
        limit = max(1, self.config.max_depth)
        if event in stack or len(stack) >= limit:
            return await self._reject_recursive(
                event,
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
                fence=fence,
            )
        token = _emit_stack.set((*stack, event))
        try:
            return await self._emit_inner(
                event,
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
                payload=payload or {},
                fence=fence,
            )
        finally:
            _emit_stack.reset(token)

    async def _emit_inner(
        self,
        event: HookEvent,
        *,
        session_id: str | None,
        run_id: str | None,
        turn_id: str | None,
        payload: dict[str, Any],
        fence: LeaseFence | None,
    ) -> HookOutcome:
        matched = [reg for reg in self._ordered() if event in reg.events]
        outcome = HookOutcome()
        if not matched:
            return outcome
        await self._safe_record(
            session_id,
            EventType.HOOK_STARTED,
            {
                "event": event.value,
                "hook_ids": [reg.id for reg in matched],
                "count": len(matched),
                "phase": "started",
            },
            run_id=run_id,
            turn_id=turn_id,
            fence=fence,
        )
        for registration in matched:
            timeout = registration.timeout_s or self.config.timeout_s
            if timeout <= 0:
                timeout = _DEFAULT_TIMEOUT_S
            invocation = HookInvocation(
                event=event,
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
                payload=payload,
                workspace=self.workspace,
                fence=fence,
                hook_id=registration.id,
                source=registration.source,
            )
            started = time.monotonic()

            async def _invoke(
                reg: HookRegistration = registration,
                inv: HookInvocation = invocation,
            ) -> HookDecision | None:
                return await reg.handler(inv)

            task: asyncio.Task[HookDecision | None] = asyncio.create_task(_invoke())
            try:
                done, _pending = await asyncio.wait({task}, timeout=timeout)
            except BaseException:
                task.cancel()
                raise
            if not done:
                # A handler that swallows CancelledError must not defeat the
                # timeout. Cancel best-effort, ignore any decision it returns
                # later, and apply the configured failure policy now.
                task.cancel()
                await asyncio.wait({task}, timeout=0.2)
                duration_ms = _duration_ms(started)
                message = self.redactor.redact(
                    f"hook {registration.id} timed out after {timeout:g}s"
                )
                outcome.results.append(
                    HookExecution(
                        hook_id=registration.id,
                        source=registration.source,
                        event=event,
                        status="timeout",
                        duration_ms=duration_ms,
                        error=message,
                    )
                )
                if self._fail_closed(outcome, registration, event, "timed out"):
                    break
                continue
            try:
                decision = task.result()
            except (asyncio.CancelledError, RunCancelled):
                raise
            except HookTimeout as exc:
                duration_ms = _duration_ms(started)
                message = self.redactor.redact(str(exc))
                outcome.results.append(
                    HookExecution(
                        hook_id=registration.id,
                        source=registration.source,
                        event=event,
                        status="timeout",
                        duration_ms=duration_ms,
                        error=message,
                    )
                )
                if self._fail_closed(outcome, registration, event, "timed out"):
                    break
                continue
            except Exception as exc:
                duration_ms = _duration_ms(started)
                message = self.redactor.redact(f"{type(exc).__name__}: {exc}")
                outcome.results.append(
                    HookExecution(
                        hook_id=registration.id,
                        source=registration.source,
                        event=event,
                        status="failed",
                        duration_ms=duration_ms,
                        error=message,
                    )
                )
                if self._fail_closed(outcome, registration, event, "failed"):
                    break
                continue

            duration_ms = _duration_ms(started)
            if decision is not None and not isinstance(decision, HookDecision):
                outcome.results.append(
                    HookExecution(
                        hook_id=registration.id,
                        source=registration.source,
                        event=event,
                        status="failed",
                        duration_ms=duration_ms,
                        error="handler returned an invalid decision",
                    )
                )
                if self._fail_closed(outcome, registration, event, "failed"):
                    break
                continue

            status: HookStatus = "ok"
            notes: builtins.list[str] = []
            stop = False
            if decision is not None:
                if decision.action == "deny":
                    if event is HookEvent.TOOL_BEFORE and registration.can_deny:
                        reason = decision.reason or f"denied by hook {registration.id}"
                        outcome.denied = True
                        outcome.reason = self.redactor.redact(reason)
                        status = "denied"
                        outcome.results.append(
                            HookExecution(
                                hook_id=registration.id,
                                source=registration.source,
                                event=event,
                                status=status,
                                duration_ms=duration_ms,
                            )
                        )
                        stop = True
                    else:
                        notes.append(_DENIAL_NOTE)
                if decision.context_append:
                    if event is HookEvent.MODEL_BEFORE and registration.can_append_context:
                        accepted = self._append_context(outcome, decision.context_append)
                        if accepted < len(decision.context_append):
                            notes.append("context_append was truncated to max_context_chars")
                    else:
                        notes.append(_CONTEXT_NOTE)
            if stop:
                break
            if notes and status == "ok":
                status = "skipped"
            outcome.results.append(
                HookExecution(
                    hook_id=registration.id,
                    source=registration.source,
                    event=event,
                    status=status,
                    duration_ms=duration_ms,
                    error="; ".join(self.redactor.redact(note) for note in notes) or None,
                )
            )

        await self._record_terminal(
            event, outcome, session_id=session_id, run_id=run_id, turn_id=turn_id, fence=fence
        )
        await self._audit_terminal(event, outcome, session_id=session_id)
        return outcome

    def _fail_closed(
        self,
        outcome: HookOutcome,
        registration: HookRegistration,
        event: HookEvent,
        what: str,
    ) -> bool:
        """Apply a hook failure. Returns True when the caller must stop."""
        if registration.fail_mode != "closed":
            return False
        outcome.failed_closed = True
        if event is HookEvent.TOOL_BEFORE:
            outcome.denied = True
            if not outcome.reason:
                outcome.reason = self.redactor.redact(
                    f"hook {registration.id} {what}; denied (fail-closed)"
                )
            return True
        return False

    def _append_context(self, outcome: HookOutcome, text: str) -> int:
        """Append redacted context while respecting the global char budget."""
        safe = self.redactor.redact(text)
        limit = max(0, self.config.max_context_chars)
        if limit <= 0 or not safe:
            return 0
        if outcome.context_append:
            remaining = limit - len(outcome.context_append) - 1
            if remaining <= 0:
                return 0
            accepted = safe[:remaining]
            if accepted:
                outcome.context_append = f"{outcome.context_append}\n{accepted}"
            return len(accepted)
        accepted = safe[:limit]
        if accepted:
            outcome.context_append = accepted
        return len(accepted)

    async def _reject_recursive(
        self,
        event: HookEvent,
        *,
        session_id: str | None,
        run_id: str | None,
        turn_id: str | None,
        fence: LeaseFence | None,
    ) -> HookOutcome:
        reason = self.redactor.redact(f"hook recursion rejected for event {event.value}")
        outcome = HookOutcome(failed_closed=True, reason=reason)
        if event is HookEvent.TOOL_BEFORE:
            outcome.denied = True
        outcome.results.append(
            HookExecution(
                hook_id="<recursion>",
                source="manager",
                event=event,
                status="failed",
                duration_ms=0,
                error=reason,
            )
        )
        # Best effort only; never raises and never re-enters dispatch.
        await self._safe_record(
            session_id,
            EventType.HOOK_FAILED,
            {
                "event": event.value,
                "hook_ids": ["<recursion>"],
                "statuses": ["failed"],
                "denied": outcome.denied,
                "failed_closed": True,
                "reason": reason,
            },
            run_id=run_id,
            turn_id=turn_id,
            fence=fence,
        )
        return outcome

    # ------------------------------------------------------------ conveniences

    async def before_tool_call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        turn_id: str | None = None,
        fence: LeaseFence | None = None,
    ) -> HookOutcome:
        safe_arguments = self.redactor.redact_obj(arguments or {})
        return await self.emit(
            HookEvent.TOOL_BEFORE,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            payload={"tool": tool_name, "arguments": safe_arguments},
            fence=fence,
        )

    async def before_model_call(
        self,
        *,
        model: str = "",
        messages: int = 0,
        tools: int = 0,
        session_id: str | None = None,
        run_id: str | None = None,
        turn_id: str | None = None,
        fence: LeaseFence | None = None,
    ) -> HookOutcome:
        return await self.emit(
            HookEvent.MODEL_BEFORE,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            payload={"model": model, "messages": messages, "tools": tools},
            fence=fence,
        )

    async def on_bus_event(self, event: Event) -> None:
        """EventBus subscriber: observe lifecycle facts, never break the bus."""
        try:
            hook_event = HookEvent.from_event_type(event.type)
            if hook_event is None:
                return
            await self.emit(
                hook_event,
                session_id=event.session_id,
                run_id=event.run_id,
                turn_id=event.turn_id,
                payload=dict(event.payload),
                fence=None,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # observers must never break the event bus
            logger.warning("hook bus observer failed for %s", event.type.value)

    # -------------------------------------------------------------- bookkeeping

    async def _record_terminal(
        self,
        event: HookEvent,
        outcome: HookOutcome,
        *,
        session_id: str | None,
        run_id: str | None,
        turn_id: str | None,
        fence: LeaseFence | None,
    ) -> None:
        if outcome.denied:
            event_type = EventType.HOOK_DENIED
        elif outcome.failed_closed or any(
            result.status in ("failed", "timeout") for result in outcome.results
        ):
            event_type = EventType.HOOK_FAILED
        else:
            event_type = EventType.HOOK_COMPLETED
        await self._safe_record(
            session_id,
            event_type,
            {
                "event": event.value,
                "hooks": [
                    {
                        "id": result.hook_id,
                        "source": result.source,
                        "status": result.status,
                        "duration_ms": result.duration_ms,
                        "error": result.error,
                    }
                    for result in outcome.results
                ],
                "hook_ids": outcome.hook_ids,
                "denied": outcome.denied,
                "failed_closed": outcome.failed_closed,
                "reason": outcome.reason,
                "context_chars": len(outcome.context_append),
            },
            run_id=run_id,
            turn_id=turn_id,
            fence=fence,
        )

    async def _audit_terminal(
        self,
        event: HookEvent,
        outcome: HookOutcome,
        *,
        session_id: str | None,
    ) -> None:
        if self.audit is None:
            return
        failed = outcome.failed_closed or any(
            result.status in ("failed", "timeout") for result in outcome.results
        )
        if not (outcome.denied or failed):
            return
        action = "hook.denied" if outcome.denied else "hook.failed"
        detail = {
            "event": event.value,
            "hooks": [
                {"id": result.hook_id, "status": result.status} for result in outcome.results
            ],
            "reason": self.redactor.redact(outcome.reason)[:500],
            "failed_closed": outcome.failed_closed,
        }
        resource = ",".join(outcome.hook_ids)[:200] or None
        try:
            await self.audit.append(
                actor="hook",
                action=action,
                resource=resource,
                detail=detail,
                session_id=session_id,
            )
        except Exception:  # audit is best effort for hook bookkeeping
            logger.warning("hook audit record failed for event %s", event.value)

    async def _safe_record(
        self,
        session_id: str | None,
        event_type: EventType,
        payload: dict[str, Any],
        *,
        run_id: str | None,
        turn_id: str | None,
        fence: LeaseFence | None,
    ) -> None:
        if self.recorder is None or not session_id:
            return
        safe_payload = self.redactor.redact_obj(payload)
        try:
            await self.recorder.record(
                session_id,
                event_type,
                payload=safe_payload,
                run_id=run_id,
                turn_id=turn_id,
                fence=fence,
            )
        except Exception:  # hook bookkeeping must not break the run
            logger.debug("hook event recording failed for %s", event_type.value)


def _duration_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


__all__ = ["HookManager"]
