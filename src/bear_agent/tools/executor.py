"""Tool execution pipeline.

Order is a security contract:
resolve -> validate -> generic policy -> tool preflight -> approval -> claims
-> persist start -> execute -> redact -> persist finish.

Unknown tools never execute. Invalid arguments never reach a tool. Denied
commands are audited. Security-state writes (policy, approval, claims, audit)
happen before side effects; audit failure before execution aborts the call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

from pydantic import ValidationError

from bear_agent.audit.service import AuditService
from bear_agent.core.cancel import CancelToken, RunCancelled
from bear_agent.core.errors import (
    AuditError,
    ClaimConflict,
    ClaimError,
    SandboxError,
    ToolExecutionError,
)
from bear_agent.core.events import EventType
from bear_agent.core.ids import new_id
from bear_agent.core.messages import ToolCall
from bear_agent.core.modes import PermissionMode
from bear_agent.observability.recorder import EventRecorder
from bear_agent.policy.approvals import ApprovalService, approval_fingerprint
from bear_agent.policy.engine import PermissionEngine
from bear_agent.policy.models import (
    ApprovalRequest,
    DecisionType,
)
from bear_agent.policy.path_guard import PathAccessError, PathGuard
from bear_agent.secrets.redaction import SecretRedactor
from bear_agent.sessions.claims import ClaimService
from bear_agent.sessions.service import SessionService
from bear_agent.tools.base import (
    ClaimRequest,
    PreflightAction,
    PreflightDecision,
    ToolContext,
    ToolResult,
)
from bear_agent.tools.registry import ToolRegistry


class ToolExecutor:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        sessions: SessionService,
        policy: PermissionEngine,
        approvals: ApprovalService,
        recorder: EventRecorder,
        path_guard: PathGuard,
        audit: AuditService | None = None,
        redactor: SecretRedactor | None = None,
        claims: ClaimService | None = None,
        max_output_chars: int = 24_000,
    ) -> None:
        self.registry = registry
        self.sessions = sessions
        self.policy = policy
        self.approvals = approvals
        self.recorder = recorder
        self.path_guard = path_guard
        self.audit = audit
        self.redactor = redactor or SecretRedactor()
        self.claims = claims
        self.max_output_chars = max_output_chars

    async def execute(
        self,
        call: ToolCall,
        *,
        session_id: str,
        project_id: str | None,
        lease_owner: str,
        run_id: str | None,
        turn_id: str | None,
        cancel: CancelToken,
    ) -> ToolResult:
        if not call.id:
            call = call.model_copy(update={"id": new_id("call")})
        tool = self.registry.get(call.name)
        idempotent = tool.idempotent if tool else False
        safe_arguments = self.redactor.redact_obj(call.arguments)
        await self.sessions.tool_calls.start(
            session_id,
            turn_id,
            call.id,
            call.name,
            safe_arguments,
            idempotent,
        )
        await self.recorder.record(
            session_id,
            EventType.TOOL_CALL_STARTED,
            run_id=run_id,
            turn_id=turn_id,
            payload={
                "tool_call_id": call.id,
                "name": call.name,
                "arguments_preview": _preview(safe_arguments),
            },
        )
        await self.recorder.record(
            session_id,
            EventType.TOOL_STARTED,
            run_id=run_id,
            turn_id=turn_id,
            persist=False,
            payload={"tool_call_id": call.id, "name": call.name},
        )

        if tool is None:
            return await self._fail(
                session_id,
                run_id,
                turn_id,
                call,
                "unknown_tool",
                f"unknown tool: {call.name}",
            )
        if call.arguments_error:
            return await self._fail(
                session_id,
                run_id,
                turn_id,
                call,
                "invalid_arguments",
                f"tool arguments were incomplete or unparsable: {call.arguments_error}",
            )

        try:
            args = tool.args_model.model_validate(call.arguments)
        except ValidationError as exc:
            return await self._fail(
                session_id,
                run_id,
                turn_id,
                call,
                "invalid_arguments",
                f"invalid arguments for {call.name}: {exc}",
            )

        cancel.raise_if_cancelled()
        ctx = ToolContext(
            cwd=self.path_guard.cwd,
            session_id=session_id,
            cancel=cancel,
            path_guard=self.path_guard,
            max_output_chars=self.max_output_chars,
        )
        generic = self.policy.decide(tool, args)
        preflight: PreflightDecision | None
        try:
            preflight = await tool.preflight(args, ctx)
        except Exception as exc:
            preflight = PreflightDecision(
                PreflightAction.DENY,
                f"tool preflight failed: {type(exc).__name__}: {exc}",
                risk="critical",
            )
        action, reason, risk = _combine(self.policy.mode, generic.decision, preflight)
        policy_payload: dict[str, Any] = {
            "tool_call_id": call.id,
            "tool": call.name,
            "decision": action.value,
            "reason": reason,
            "risk": risk,
            "generic_decision": generic.decision.value,
            "generic_reason": generic.reason,
            "preflight": preflight.action.value if preflight else None,
            "preflight_reason": preflight.reason if preflight else None,
            "matched_rules": list(preflight.details.get("matched_rules", [])) if preflight else [],
        }
        await self.recorder.record(
            session_id,
            EventType.POLICY_DECISION,
            run_id=run_id,
            turn_id=turn_id,
            payload=policy_payload,
        )
        try:
            await self._audit(
                "policy.decision",
                session_id=session_id,
                resource=call.name,
                detail=policy_payload,
            )
        except ToolExecutionError as exc:
            return await self._fail(
                session_id, run_id, turn_id, call, "audit_unavailable", str(exc)
            )
        if action == PreflightAction.DENY:
            return await self._fail(session_id, run_id, turn_id, call, "policy_denied", reason)

        sandbox_details = preflight.details if preflight else {}
        if sandbox_details.get("sandbox_backend"):
            sandbox_payload = {
                "tool_call_id": call.id,
                "tool": call.name,
                "backend": sandbox_details.get("sandbox_backend"),
                "filesystem_isolated": sandbox_details.get("sandbox_filesystem", False),
                "capabilities": sandbox_details.get("sandbox_capabilities", {}),
            }
            await self.recorder.record(
                session_id,
                EventType.SANDBOX_DECISION,
                run_id=run_id,
                turn_id=turn_id,
                payload=sandbox_payload,
            )
            try:
                await self._audit(
                    "sandbox.decision",
                    session_id=session_id,
                    resource=call.name,
                    detail=sandbox_payload,
                )
            except ToolExecutionError as exc:
                return await self._fail(
                    session_id, run_id, turn_id, call, "audit_unavailable", str(exc)
                )

        if action == PreflightAction.ASK:
            fingerprint = approval_fingerprint(call.name, call.arguments)
            request = ApprovalRequest(
                tool_name=call.name,
                arguments=call.arguments,
                reason=reason,
                risk=risk,
                session_id=session_id,
                project_id=project_id,
                fingerprint=fingerprint,
            )
            await self.recorder.record(
                session_id,
                EventType.APPROVAL_REQUESTED,
                run_id=run_id,
                turn_id=turn_id,
                payload={
                    "tool_call_id": call.id,
                    "tool": call.name,
                    "risk": risk,
                    "fingerprint": fingerprint,
                },
            )
            try:
                await self._audit(
                    "approval.requested",
                    session_id=session_id,
                    resource=call.name,
                    detail={"fingerprint": fingerprint, "risk": risk, "reason": reason},
                )
            except ToolExecutionError as exc:
                return await self._fail(
                    session_id, run_id, turn_id, call, "audit_unavailable", str(exc)
                )
            decision = await self.approvals.request(request)
            await self.recorder.record(
                session_id,
                EventType.APPROVAL_DECIDED,
                run_id=run_id,
                turn_id=turn_id,
                payload={
                    "tool_call_id": call.id,
                    "approved": decision.approved,
                    "scope": decision.scope.value,
                    "reason": decision.reason,
                },
            )
            try:
                await self._audit(
                    "approval.decided",
                    session_id=session_id,
                    resource=call.name,
                    detail={
                        "approved": decision.approved,
                        "scope": decision.scope.value,
                        "reason": decision.reason,
                    },
                )
            except ToolExecutionError as exc:
                return await self._fail(
                    session_id, run_id, turn_id, call, "audit_unavailable", str(exc)
                )
            if not decision.approved:
                return await self._fail(
                    session_id,
                    run_id,
                    turn_id,
                    call,
                    "approval_denied",
                    f"user denied {call.name}",
                )

        ctx.approved = True
        try:
            claim_requests = tool.claim_paths(args) if self.claims is not None else []
        except Exception as exc:
            return await self._fail(
                session_id,
                run_id,
                turn_id,
                call,
                "claim_error",
                f"tool claim declaration failed: {type(exc).__name__}: {exc}",
            )
        claim_error = await self._acquire_claims(
            claim_requests, session_id, lease_owner, run_id, turn_id, call
        )
        if claim_error is not None:
            return claim_error
        claims = self.claims
        if claim_requests and claims is not None:

            async def assert_write_allowed(path: str) -> None:
                await claims.assert_owned(session_id, lease_owner, path)

            ctx.assert_write_allowed = assert_write_allowed

        started = time.monotonic()
        try:
            await self._audit(
                "tool.started",
                session_id=session_id,
                resource=call.name,
                detail={
                    "tool_call_id": call.id,
                    "arguments_preview": _preview(safe_arguments),
                },
            )
        except ToolExecutionError as exc:
            return await self._fail(
                session_id, run_id, turn_id, call, "audit_unavailable", str(exc)
            )
        try:
            result = await asyncio.wait_for(tool.run(args, ctx), timeout=tool.timeout_s)
        except asyncio.CancelledError:
            await self._finish_row(call.id, "cancelled", "execution cancelled")
            await self._record_finish(session_id, run_id, turn_id, call, "cancelled", None, None)
            raise
        except RunCancelled:
            await self._finish_row(call.id, "cancelled", "run cancelled")
            await self._record_finish(session_id, run_id, turn_id, call, "cancelled", None, None)
            raise
        except TimeoutError:
            result = ToolResult(
                content=f"tool {call.name} timed out after {tool.timeout_s:.0f}s",
                is_error=True,
                error_kind="timeout",
            )
        except SandboxError as exc:
            result = ToolResult(
                content=f"sandbox error: {exc}",
                is_error=True,
                error_kind="sandbox_error",
            )
        except Exception as exc:
            result = ToolResult(
                content=f"{type(exc).__name__}: {exc}",
                is_error=True,
                error_kind="tool_error",
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        result = self._normalize(self._redact_result(result))
        status = "failed" if result.is_error else "completed"
        await self._finish_row(call.id, status, result.content, result.error_kind)
        await self._record_finish(session_id, run_id, turn_id, call, status, result, duration_ms)
        try:
            await self._audit(
                "tool.finished",
                session_id=session_id,
                resource=call.name,
                detail={
                    "tool_call_id": call.id,
                    "status": status,
                    "error_kind": result.error_kind,
                    "duration_ms": duration_ms,
                    "exit_code": result.meta.get("exit_code"),
                    "sandbox_backend": result.meta.get("sandbox_backend"),
                    "sandboxed": result.meta.get("sandboxed"),
                },
            )
        except ToolExecutionError as exc:
            result = result.model_copy(
                update={
                    "content": result.content + f"\n[audit error] {exc}",
                    "is_error": True,
                    "error_kind": result.error_kind or "audit_error",
                }
            )
        return result

    async def _acquire_claims(
        self,
        requests: list[ClaimRequest],
        session_id: str,
        lease_owner: str,
        run_id: str | None,
        turn_id: str | None,
        call: ToolCall,
    ) -> ToolResult | None:
        if self.claims is None:
            return None
        acquired: list[tuple[str, str]] = []
        for request in requests:
            try:
                claim = await self.claims.acquire(
                    session_id,
                    lease_owner,
                    request.path,
                    scope=request.scope,
                    mode=request.mode,
                )
                acquired.append((request.path, claim.path))
            except ClaimConflict as exc:
                await self.recorder.record(
                    session_id,
                    EventType.CLAIM_CONFLICT,
                    run_id=run_id,
                    turn_id=turn_id,
                    payload={
                        "tool_call_id": call.id,
                        "tool": call.name,
                        "path": request.path,
                        "holder": exc.holder,
                    },
                )
                try:
                    await self._audit(
                        "claim.conflict",
                        session_id=session_id,
                        resource=request.path,
                        detail={
                            "holder": exc.holder,
                            "tool": call.name,
                            "reason": str(exc),
                        },
                    )
                except ToolExecutionError as audit_exc:
                    return await self._fail(
                        session_id,
                        run_id,
                        turn_id,
                        call,
                        "audit_unavailable",
                        str(audit_exc),
                    )
                return await self._fail(
                    session_id, run_id, turn_id, call, "claim_conflict", str(exc)
                )
            except (ClaimError, PathAccessError) as exc:
                return await self._fail(session_id, run_id, turn_id, call, "claim_error", str(exc))
            except Exception as exc:
                return await self._fail(
                    session_id,
                    run_id,
                    turn_id,
                    call,
                    "claim_error",
                    f"claim acquisition failed: {type(exc).__name__}: {exc}",
                )
            await self.recorder.record(
                session_id,
                EventType.CLAIM_ACQUIRED,
                run_id=run_id,
                turn_id=turn_id,
                payload={
                    "tool_call_id": call.id,
                    "tool": call.name,
                    "path": claim.path,
                    "scope": claim.scope,
                },
            )
            try:
                await self._audit(
                    "claim.acquired",
                    session_id=session_id,
                    resource=claim.path,
                    detail={"tool": call.name, "scope": claim.scope, "mode": claim.mode},
                )
            except ToolExecutionError as exc:
                with contextlib.suppress(Exception):
                    await self.claims.release_one(session_id, lease_owner, claim.path)
                return await self._fail(
                    session_id, run_id, turn_id, call, "audit_unavailable", str(exc)
                )
        for _original, normalized in acquired:
            try:
                await self.claims.assert_owned(session_id, lease_owner, normalized)
            except ClaimError as exc:
                return await self._fail(session_id, run_id, turn_id, call, "claim_fenced", str(exc))
            except Exception as exc:
                return await self._fail(
                    session_id,
                    run_id,
                    turn_id,
                    call,
                    "claim_fenced",
                    f"claim check failed: {type(exc).__name__}: {exc}",
                )
        return None

    async def _fail(
        self,
        session_id: str,
        run_id: str | None,
        turn_id: str | None,
        call: ToolCall,
        kind: str,
        message: str,
    ) -> ToolResult:
        message = self.redactor.redact(message)
        result = ToolResult(content=message, is_error=True, error_kind=kind)
        await self._finish_row(call.id, "failed", message, kind)
        await self._record_finish(session_id, run_id, turn_id, call, "failed", result, None)
        with contextlib.suppress(ToolExecutionError):
            await self._audit(
                "tool.failed",
                session_id=session_id,
                resource=call.name,
                detail={"tool_call_id": call.id, "error_kind": kind},
            )
        return result

    async def _finish_row(
        self,
        call_id: str,
        status: str,
        summary: str | None,
        error_kind: str | None = None,
    ) -> None:
        await self.sessions.tool_calls.finish(
            call_id,
            status,
            result_summary=(summary or "")[:500] or None,
            error_kind=error_kind,
            error_message=(summary or "")[:1000] if status == "failed" else None,
        )

    async def _record_finish(
        self,
        session_id: str,
        run_id: str | None,
        turn_id: str | None,
        call: ToolCall,
        status: str,
        result: ToolResult | None,
        duration_ms: int | None,
    ) -> None:
        event_type = (
            EventType.TOOL_CALL_COMPLETED if status == "completed" else EventType.TOOL_CALL_FAILED
        )
        payload: dict[str, Any] = {
            "tool_call_id": call.id,
            "name": call.name,
            "status": status,
        }
        if result is not None:
            payload["is_error"] = result.is_error
            payload["error_kind"] = result.error_kind
            payload["content_preview"] = result.content[:300]
            payload["files_changed"] = result.files_changed
            payload["truncated"] = result.truncated
            payload["sandbox_backend"] = result.meta.get("sandbox_backend")
            payload["sandboxed"] = result.meta.get("sandboxed")
            payload["exit_code"] = result.meta.get("exit_code")
            payload["timed_out"] = result.meta.get("timed_out")
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        await self.recorder.record(
            session_id,
            event_type,
            run_id=run_id,
            turn_id=turn_id,
            payload=payload,
        )
        await self.recorder.record(
            session_id,
            EventType.TOOL_FINISHED,
            run_id=run_id,
            turn_id=turn_id,
            persist=False,
            payload={
                "tool_call_id": call.id,
                "name": call.name,
                "status": status,
                "duration_ms": duration_ms,
                "sandboxed": result.meta.get("sandboxed") if result else None,
                "exit_code": result.meta.get("exit_code") if result else None,
            },
        )

    async def _audit(
        self,
        action: str,
        *,
        session_id: str,
        resource: str | None,
        detail: dict[str, Any],
    ) -> None:
        if self.audit is None:
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
            raise ToolExecutionError(
                f"security audit unavailable; refusing the action: {exc}"
            ) from exc

    def _redact_result(self, result: ToolResult) -> ToolResult:
        return result.model_copy(
            update={
                "content": self.redactor.redact(result.content),
                "display": self.redactor.redact(result.display) if result.display else None,
                "meta": self.redactor.redact_obj(result.meta),
            }
        )

    def _normalize(self, result: ToolResult) -> ToolResult:
        if len(result.content) <= self.max_output_chars:
            return result
        limit = self.max_output_chars
        head = limit * 2 // 3
        tail = limit - head
        content = (
            result.content[:head]
            + f"\n... [truncated {len(result.content) - limit} chars] ...\n"
            + result.content[-tail:]
        )
        return result.model_copy(update={"content": content, "truncated": True})


def _combine(
    mode: PermissionMode,
    generic: DecisionType,
    preflight: PreflightDecision | None,
) -> tuple[PreflightAction, str, str]:
    if preflight is not None and preflight.action == PreflightAction.DENY:
        return PreflightAction.DENY, preflight.reason, preflight.risk
    if generic == DecisionType.DENY:
        return PreflightAction.DENY, "blocked by permission mode", "high"
    if preflight is not None and preflight.action == PreflightAction.ASK:
        if mode == PermissionMode.UNRESTRICTED:
            return (
                PreflightAction.ALLOW,
                f"unrestricted mode: {preflight.reason}",
                preflight.risk,
            )
        return PreflightAction.ASK, preflight.reason, preflight.risk
    if generic == DecisionType.ASK:
        if mode == PermissionMode.UNRESTRICTED:
            return PreflightAction.ALLOW, "unrestricted mode", "moderate"
        if (
            mode == PermissionMode.AUTO
            and preflight is not None
            and (preflight.action == PreflightAction.ALLOW)
        ):
            return PreflightAction.ALLOW, preflight.reason, preflight.risk
        return PreflightAction.ASK, "permission mode requires approval", "moderate"
    return PreflightAction.ALLOW, "allowed", preflight.risk if preflight else "low"


def _preview(arguments: dict[str, Any]) -> str:
    try:
        text = json.dumps(arguments, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(arguments)
    return text if len(text) <= 500 else text[:500] + "…"
