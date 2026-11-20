"""Crash recovery for a session.

Recovery runs after the new run has taken the session lease and before any new
turn starts. It never re-executes tools and never marks running tools failed
blindly. Instead it:

* verifies the audit chain when required and fails closed if it is broken;
* reconciles the transcript: an assistant tool call without a tool result is
  answered with a synthetic tool message that explains the interrupted state,
  but only when the call is at the transcript tail and appending cannot
  misorder the conversation;
* terminates orphaned shell processes whose identity can be verified;
* releases file claims left behind by the previous lease owner;
* records every decision as an event and, when enabled, an audit record.

The lease is the lock: two processes cannot recover the same session because
the second ``acquire_lease`` fails. All writes still carry the fence so a
process that loses the lease mid-recovery cannot keep writing.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from typing import Any

from bear_agent.audit.service import AuditService
from bear_agent.config.models import RecoveryConfig
from bear_agent.core.clock import Clock, SystemClock
from bear_agent.core.errors import AuditError, RecoveryError
from bear_agent.core.events import EventType
from bear_agent.core.fencing import LeaseFence
from bear_agent.core.messages import Message, ToolCall, ToolMessage
from bear_agent.observability.recorder import EventRecorder
from bear_agent.sessions.checkpoints import CheckpointService
from bear_agent.sessions.claims import ClaimService
from bear_agent.sessions.service import SessionService
from bear_agent.shell.process_registry import ProcessRegistry


@dataclass
class RecoveryReport:
    recovered: bool
    checkpoint_id: str | None = None
    running_tools: int = 0
    synthesized_tools: list[str] = field(default_factory=list)
    released_claims: int = 0
    terminated_processes: list[int] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    audit_verified: bool = False
    # True when the transcript is left in a state that a model request cannot
    # safely consume (e.g. an unrepaired mid-transcript tool call). The runtime
    # refuses to start a new turn until a human inspects or forks the session.
    blocked: bool = False


@dataclass(frozen=True, slots=True)
class _DanglingCall:
    assistant_index: int
    assistant_seq: int | None
    call: ToolCall


class RecoveryService:
    def __init__(
        self,
        *,
        sessions: SessionService,
        claims: ClaimService | None,
        processes: ProcessRegistry | None,
        checkpoints: CheckpointService | None,
        recorder: EventRecorder,
        audit: AuditService | None = None,
        config: RecoveryConfig | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.sessions = sessions
        self.claims = claims
        self.processes = processes
        self.checkpoints = checkpoints
        self.recorder = recorder
        self.audit = audit
        self.config = config or RecoveryConfig()
        self.clock = clock or SystemClock()

    async def recover_if_needed(
        self,
        session_id: str,
        *,
        fence: LeaseFence,
        run_id: str | None = None,
    ) -> RecoveryReport:
        """Recover a crashed session, or return a cheap no-op report.

        The caller must hold the session lease described by ``fence``. An idle
        session (no running tools, no running processes and no dangling tool
        calls in the transcript) performs reads only and writes no events.
        """
        if not self.config.enabled:
            return RecoveryReport(recovered=False)

        running_rows = await self.sessions.running_tool_calls(session_id)
        process_records = (
            await self.processes.repository.list_running(session_id)
            if self.processes is not None
            else []
        )
        messages = await self.sessions.load_messages(session_id)
        dangling = _find_dangling_calls(messages)
        if not running_rows and not process_records and not dangling:
            return RecoveryReport(recovered=False)

        report = RecoveryReport(recovered=True, running_tools=len(running_rows))
        started_payload: dict[str, Any] = {
            "lease_owner": fence.owner,
            "epoch": fence.epoch,
            "running_tools": len(running_rows),
            "running_processes": len(process_records),
            "dangling_tool_calls": len(dangling),
        }
        try:
            await self.recorder.record(
                session_id,
                EventType.RECOVERY_STARTED,
                run_id=run_id,
                payload=started_payload,
                fence=fence,
            )
            await self._audit("recovery.started", session_id=session_id, detail=started_payload)

            await self._verify_audit_chain(report)
            await self._load_checkpoint_diagnostic(session_id, report)
            await self._reconcile(
                session_id,
                messages,
                dangling,
                running_rows,
                report,
                fence=fence,
                run_id=run_id,
            )
            await self._terminate_orphan_processes(session_id, report, fence=fence)
            await self._release_stale_claims(session_id, report, fence=fence, run_id=run_id)

            completed_payload = self._report_payload(report)
            await self.recorder.record(
                session_id,
                EventType.RECOVERY_COMPLETED,
                run_id=run_id,
                payload=completed_payload,
                fence=fence,
            )
            await self._audit("recovery.completed", session_id=session_id, detail=completed_payload)
            return report
        except RecoveryError as exc:
            await self._failure_notice(
                session_id, report, run_id=run_id, message=str(exc), fence=fence
            )
            raise
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            await self._failure_notice(
                session_id, report, run_id=run_id, message=message, fence=fence
            )
            raise RecoveryError(f"recovery failed for session {session_id}: {message}") from exc

    # ------------------------------------------------------------------ steps

    async def _verify_audit_chain(self, report: RecoveryReport) -> None:
        if self.audit is None or not self.audit.enabled or not self.config.require_audit:
            return
        verification = await self.audit.verify()
        if not verification.ok:
            raise RecoveryError(
                "audit chain verification failed at record "
                f"{verification.first_bad_id}: {verification.reason}"
            )
        report.audit_verified = True

    async def _load_checkpoint_diagnostic(self, session_id: str, report: RecoveryReport) -> None:
        if self.checkpoints is None:
            return
        checkpoint = await self.checkpoints.latest_valid(session_id)
        if checkpoint is not None:
            report.checkpoint_id = checkpoint.id
            report.actions.append(
                f"loaded checkpoint {checkpoint.id} at message seq {checkpoint.message_seq}"
            )

    async def _reconcile(
        self,
        session_id: str,
        messages: list[Message],
        dangling: list[_DanglingCall],
        running_rows: list[dict[str, Any]],
        report: RecoveryReport,
        *,
        fence: LeaseFence,
        run_id: str | None,
    ) -> None:
        for item in dangling:
            call = item.call
            row = await self.sessions.tool_calls.get(call.id)
            name = str(row["name"]) if row is not None else call.name
            idempotent = bool(row["idempotent"]) if row is not None else False
            if not _repairable_at_tail(messages, item.assistant_index):
                report.blocked = True
                report.issues.append(
                    f"tool call {call.id} ({name}) from assistant message seq "
                    f"{item.assistant_seq} is unanswered but is not at the transcript tail; "
                    "no synthetic result was written. Inspect the session or fork from a checkpoint."
                )
                continue
            content = _synthetic_tool_content(
                call_id=call.id,
                name=name,
                idempotent=idempotent,
                has_execution_record=row is not None,
                args_preview=_args_preview(row, call),
            )
            _, event = await self.sessions.append_tool_message(
                session_id,
                ToolMessage.of(call.id, content),
                run_id=run_id,
                payload={
                    "synthetic": True,
                    "reason": "crash_recovery",
                    "tool_call_id": call.id,
                },
                fence=fence,
            )
            if row is not None:
                await self.sessions.tool_calls.finish(
                    call.id,
                    "unknown",
                    result_summary="interrupted by crash; not retried",
                    error_kind="recovery_interrupted",
                    fence=fence,
                )
            message_seq = event.payload.get("message_seq")
            await self.recorder.record(
                session_id,
                EventType.RECOVERY_TOOL_RECONCILED,
                run_id=run_id,
                payload={
                    "tool_call_id": call.id,
                    "name": name,
                    "idempotent": idempotent,
                    "retry_safe": idempotent,
                    "message_seq": message_seq,
                },
                fence=fence,
            )
            await self._audit(
                "recovery.tool_reconciled",
                session_id=session_id,
                resource=name,
                detail={
                    "tool_call_id": call.id,
                    "name": name,
                    "idempotent": idempotent,
                    "retry_safe": idempotent,
                    "message_seq": message_seq,
                },
            )
            report.synthesized_tools.append(call.id)
            report.actions.append(f"synthesized interrupted tool result for {call.id} ({name})")

        synthesized = set(report.synthesized_tools)
        answered = _answered_call_ids(messages)
        for row in running_rows:
            call_id = str(row["id"])
            if call_id in synthesized:
                continue
            if call_id in answered:
                # The result is already in the transcript, but the row was
                # never finalized before the crash. We cannot know whether the
                # tool returned normally or the result was partially written,
                # so record "unknown" rather than pretending success/failure.
                await self.sessions.tool_calls.finish(
                    call_id,
                    "unknown",
                    result_summary="result recorded, completion status unknown after crash",
                    error_kind="recovery_status_unknown",
                    fence=fence,
                )
                await self.recorder.record(
                    session_id,
                    EventType.RECOVERY_TOOL_RECONCILED,
                    run_id=run_id,
                    payload={
                        "tool_call_id": call_id,
                        "name": str(row["name"]),
                        "status": "unknown",
                        "reason": "completion_status_unknown_after_crash",
                        "synthetic_message": False,
                    },
                    fence=fence,
                )
                await self._audit(
                    "recovery.tool_reconciled",
                    session_id=session_id,
                    resource=str(row["name"]),
                    detail={
                        "tool_call_id": call_id,
                        "status": "unknown",
                        "synthetic_message": False,
                    },
                )
                report.actions.append(
                    f"marked running tool {call_id} unknown; its result was already recorded"
                )
                continue
            report.blocked = True
            report.issues.append(
                f"running tool call {call_id} was not reconciled: no safely appendable "
                "transcript position was found; its status is left unchanged"
            )

    async def _terminate_orphan_processes(
        self, session_id: str, report: RecoveryReport, *, fence: LeaseFence
    ) -> None:
        if self.processes is None or not self.config.kill_orphan_processes:
            return
        results = await self.processes.terminate_orphans(
            session_id, lease_owner=fence.owner, fence=fence
        )
        for result in results:
            if result.terminated:
                report.terminated_processes.append(result.pid)
                report.actions.append(f"terminated orphan process {result.pid} ({result.reason})")
            else:
                report.issues.append(
                    f"orphan process {result.pid} was not terminated: {result.reason}"
                )

    async def _release_stale_claims(
        self,
        session_id: str,
        report: RecoveryReport,
        *,
        fence: LeaseFence,
        run_id: str | None,
    ) -> None:
        if self.claims is None:
            return
        released = await self.claims.release_stale(session_id, fence.owner, fence=fence)
        if not released:
            return
        report.released_claims = len(released)
        report.actions.append(f"released {len(released)} stale file claim(s)")
        payload = {"paths": released, "lease_owner": fence.owner}
        await self.recorder.record(
            session_id,
            EventType.RECOVERY_CLAIMS_RELEASED,
            run_id=run_id,
            payload=payload,
            fence=fence,
        )
        await self._audit("recovery.claims.released", session_id=session_id, detail=payload)

    # ---------------------------------------------------------------- support

    @staticmethod
    def _report_payload(report: RecoveryReport) -> dict[str, Any]:
        return {
            "checkpoint_id": report.checkpoint_id,
            "running_tools": report.running_tools,
            "synthesized_tools": list(report.synthesized_tools),
            "released_claims": report.released_claims,
            "terminated_processes": list(report.terminated_processes),
            "issues": list(report.issues),
            "actions": list(report.actions),
            "audit_verified": report.audit_verified,
            "blocked": report.blocked,
        }

    async def _failure_notice(
        self,
        session_id: str,
        report: RecoveryReport,
        *,
        run_id: str | None,
        message: str,
        fence: LeaseFence,
    ) -> None:
        payload = {"error": message, "issues": list(report.issues)}
        with contextlib.suppress(Exception):
            await self.recorder.record(
                session_id,
                EventType.RECOVERY_FAILED,
                run_id=run_id,
                payload=payload,
                fence=fence,
            )
        if self.audit is not None and self.audit.enabled:
            with contextlib.suppress(Exception):
                await self.audit.append(
                    actor="agent",
                    action="recovery.failed",
                    detail=payload,
                    session_id=session_id,
                )

    async def _audit(
        self,
        action: str,
        *,
        session_id: str,
        detail: dict[str, Any],
        resource: str | None = None,
    ) -> None:
        if self.audit is None or not self.audit.enabled:
            return
        try:
            await self.audit.append(
                actor="agent",
                action=action,
                resource=resource,
                detail=detail,
                session_id=session_id,
            )
        except AuditError as exc:
            if self.config.require_audit:
                raise RecoveryError(f"security audit unavailable; recovery aborted: {exc}") from exc


# --------------------------------------------------------------------- helpers


def _find_dangling_calls(messages: list[Message]) -> list[_DanglingCall]:
    result_indices: dict[str, list[int]] = {}
    for index, message in enumerate(messages):
        if message.role != "tool":
            continue
        result = message.tool_result()
        if result is not None:
            result_indices.setdefault(result.tool_call_id, []).append(index)

    dangling: list[_DanglingCall] = []
    for index, message in enumerate(messages):
        if message.role != "assistant":
            continue
        for call in message.tool_calls():
            answered = any(result_index > index for result_index in result_indices.get(call.id, ()))
            if not answered:
                dangling.append(
                    _DanglingCall(
                        assistant_index=index,
                        assistant_seq=message.seq,
                        call=call,
                    )
                )
    return dangling


def _answered_call_ids(messages: list[Message]) -> set[str]:
    answered: set[str] = set()
    for message in messages:
        if message.role != "tool":
            continue
        result = message.tool_result()
        if result is not None:
            answered.add(result.tool_call_id)
    return answered


def _repairable_at_tail(messages: list[Message], assistant_index: int) -> bool:
    """A synthetic result may only be appended when nothing but tools follow.

    A later user or assistant message means the conversation moved on; writing
    a tool result at the end would be misordered, so recovery reports the issue
    instead of guessing.
    """
    return all(message.role == "tool" for message in messages[assistant_index + 1 :])


def _args_preview(row: dict[str, Any] | None, call: ToolCall) -> str | None:
    if row is not None:
        raw = row.get("args_json")
        if isinstance(raw, str) and raw:
            return _truncate(raw)
    if call.arguments:
        try:
            text = json.dumps(call.arguments, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return None
        return _truncate(text)
    return None


def _truncate(text: str, limit: int = 500) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _synthetic_tool_content(
    *,
    call_id: str,
    name: str,
    idempotent: bool,
    has_execution_record: bool,
    args_preview: str | None,
) -> str:
    if not has_execution_record:
        retry_safety = (
            "no execution record was found; the call likely did not start, but verify "
            "the workspace before retrying"
        )
    elif idempotent:
        retry_safety = "tool is idempotent; retrying is considered safe"
    else:
        retry_safety = (
            "non-idempotent or side-effecting; inspect the workspace and ask the user "
            "before retrying"
        )
    safe_name = _truncate(name, 100)
    safe_call_id = _truncate(call_id, 80)
    lines = [
        f"[recovery] Tool call {safe_call_id} ({safe_name}) was interrupted by a crash.",
        "Status unknown: the tool may or may not have completed.",
        "It was NOT automatically retried.",
        f"Retry safety: {retry_safety}.",
    ]
    if args_preview:
        lines.append(f"Original arguments preview: {args_preview}")
    return "\n".join(lines)


__all__ = ["RecoveryReport", "RecoveryService"]
