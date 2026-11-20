from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from bear_agent.config.models import HooksConfig
from bear_agent.core.cancel import RunCancelled
from bear_agent.core.errors import HookError
from bear_agent.core.events import Event, EventType
from bear_agent.hooks import (
    HookDecision,
    HookEvent,
    HookManager,
    HookRegistration,
)


class FakeRecorder:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def record(
        self,
        session_id: str,
        event_type: EventType,
        *,
        payload: dict[str, Any] | None = None,
        run_id: str | None = None,
        turn_id: str | None = None,
        persist: bool | None = None,
        fence: object | None = None,
    ) -> None:
        self.records.append(
            {
                "session_id": session_id,
                "type": event_type,
                "payload": payload or {},
                "run_id": run_id,
                "turn_id": turn_id,
            }
        )


class FakeAudit:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    async def append(
        self,
        *,
        actor: str,
        action: str,
        resource: str | None = None,
        detail: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> None:
        self.entries.append(
            {
                "actor": actor,
                "action": action,
                "resource": resource,
                "detail": detail or {},
                "session_id": session_id,
            }
        )


class RaisingRecorder:
    async def record(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("recorder down")


class RaisingAudit:
    async def append(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("audit down")


def make_manager(
    tmp_path: Path,
    *,
    recorder: object | None = None,
    audit: object | None = None,
    **config: Any,
) -> tuple[HookManager, FakeRecorder, FakeAudit]:
    fake_recorder = FakeRecorder()
    fake_audit = FakeAudit()
    manager = HookManager(
        config=HooksConfig(**config),
        recorder=fake_recorder if recorder is None else recorder,  # type: ignore[arg-type]
        audit=fake_audit if audit is None else audit,  # type: ignore[arg-type]
        workspace=tmp_path,
    )
    return manager, fake_recorder, fake_audit


def _recorded_types(recorder: FakeRecorder) -> list[EventType]:
    return [record["type"] for record in recorder.records]


# --------------------------------------------------------------- event mapping


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        (EventType.SESSION_CREATED, HookEvent.SESSION_CREATED),
        (EventType.RUN_STARTED, HookEvent.RUN_STARTED),
        (EventType.TURN_STARTED, HookEvent.TURN_STARTED),
        (EventType.MODEL_RESPONSE, HookEvent.MODEL_AFTER),
        (EventType.MODEL_RETRY, HookEvent.MODEL_ERROR),
        (EventType.TOOL_CALL_COMPLETED, HookEvent.TOOL_AFTER),
        (EventType.TOOL_CALL_FAILED, HookEvent.TOOL_ERROR),
        (EventType.TASK_STARTED, HookEvent.TASK_STARTED),
        (EventType.TASK_COMPLETED, HookEvent.TASK_COMPLETED),
        (EventType.TASK_FAILED, HookEvent.TASK_FAILED),
        (EventType.TASK_BLOCKED, HookEvent.TASK_BLOCKED),
        (EventType.TASK_CANCELLED, HookEvent.TASK_CANCELLED),
        (EventType.TASK_PLAN_REVISED, HookEvent.PLAN_REVISED),
        (EventType.CONTEXT_COMPACTED, HookEvent.CONTEXT_COMPACTED),
        (EventType.CHECKPOINT_CREATED, HookEvent.CHECKPOINT_CREATED),
        (EventType.RECOVERY_STARTED, HookEvent.RECOVERY_STARTED),
        (EventType.RECOVERY_COMPLETED, HookEvent.RECOVERY_COMPLETED),
        (EventType.RUN_COMPLETED, HookEvent.RUN_COMPLETED),
        (EventType.RUN_FAILED, HookEvent.RUN_FAILED),
        (EventType.RUN_CANCELLED, HookEvent.RUN_CANCELLED),
        (EventType.HOOK_STARTED, None),
        (EventType.HOOK_COMPLETED, None),
        (EventType.HOOK_FAILED, None),
        (EventType.HOOK_DENIED, None),
        (EventType.MODEL_TEXT_DELTA, None),
        (EventType.MODEL_THINKING_DELTA, None),
        (EventType.TOOL_STARTED, None),
        (EventType.TOOL_FINISHED, None),
    ],
)
def test_from_event_type(event_type: EventType, expected: HookEvent | None) -> None:
    assert HookEvent.from_event_type(event_type) is expected


# --------------------------------------------------------------- registration


@pytest.mark.asyncio
async def test_order_is_priority_desc_then_registration(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path)
    seen: list[str] = []

    async def record(invocation: object) -> None:
        seen.append(invocation.hook_id)  # type: ignore[attr-defined]

    manager.register(
        HookRegistration(
            id="low", events=frozenset({HookEvent.TOOL_AFTER}), handler=record, priority=0
        )
    )
    manager.register(
        HookRegistration(
            id="high-first",
            events=frozenset({HookEvent.TOOL_AFTER}),
            handler=record,
            priority=10,
        )
    )
    manager.register(
        HookRegistration(
            id="high-second",
            events=frozenset({HookEvent.TOOL_AFTER}),
            handler=record,
            priority=10,
        )
    )

    await manager.emit(HookEvent.TOOL_AFTER)
    assert seen == ["high-first", "high-second", "low"]
    assert [entry["id"] for entry in manager.list()] == ["high-first", "high-second", "low"]


@pytest.mark.asyncio
async def test_only_matching_events_run(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path)
    seen: list[HookEvent] = []

    async def record(invocation: object) -> None:
        seen.append(invocation.event)  # type: ignore[attr-defined]

    manager.register(
        HookRegistration(id="after", events=frozenset({HookEvent.TOOL_AFTER}), handler=record)
    )
    await manager.emit(HookEvent.TASK_COMPLETED)
    assert seen == []
    await manager.emit(HookEvent.TOOL_AFTER)
    assert seen == [HookEvent.TOOL_AFTER]


def test_duplicate_ids_and_unregister_source(tmp_path: Path) -> None:
    async def noop(invocation: object) -> None:
        return None

    manager, _, _ = make_manager(tmp_path)
    manager.register(
        HookRegistration(
            id="one", events=frozenset({HookEvent.RUN_STARTED}), handler=noop, source="plugin"
        )
    )
    manager.register(
        HookRegistration(
            id="two", events=frozenset({HookEvent.RUN_FAILED}), handler=noop, source="plugin"
        )
    )
    manager.register(
        HookRegistration(
            id="three", events=frozenset({HookEvent.RUN_COMPLETED}), handler=noop, source="other"
        )
    )

    with pytest.raises(HookError, match="duplicate hook id"):
        manager.register(
            HookRegistration(
                id="one",
                events=frozenset({HookEvent.RUN_STARTED}),
                handler=noop,
                source="plugin",
            )
        )
    with pytest.raises(HookError, match="duplicate hook id"):
        manager.register_many(
            [
                HookRegistration(
                    id="four", events=frozenset({HookEvent.TURN_STARTED}), handler=noop
                ),
                HookRegistration(
                    id="four", events=frozenset({HookEvent.TURN_STARTED}), handler=noop
                ),
            ]
        )

    assert manager.unregister_source("plugin") == 2
    assert [entry["id"] for entry in manager.list()] == ["three"]
    assert manager.unregister("three") is True
    assert manager.unregister("three") is False


def test_registration_capability_validation(tmp_path: Path) -> None:
    async def noop(invocation: object) -> None:
        return None

    with pytest.raises(ValueError, match="may only deny"):
        HookRegistration(
            id="bad-deny",
            events=frozenset({HookEvent.TOOL_BEFORE, HookEvent.TOOL_AFTER}),
            handler=noop,
            can_deny=True,
        )
    with pytest.raises(ValueError, match="may only append context"):
        HookRegistration(
            id="bad-context",
            events=frozenset({HookEvent.TOOL_AFTER}),
            handler=noop,
            can_append_context=True,
        )
    with pytest.raises(ValueError, match="at least one event"):
        HookRegistration(id="empty", events=frozenset(), handler=noop)


# -------------------------------------------------------------- tool intercept


@pytest.mark.asyncio
async def test_before_tool_deny_records_event_and_audit(tmp_path: Path) -> None:
    manager, recorder, audit = make_manager(tmp_path)

    async def deny(invocation: object) -> HookDecision:
        return HookDecision(action="deny", reason="blocked by policy hook")

    manager.register(
        HookRegistration(
            id="deny",
            events=frozenset({HookEvent.TOOL_BEFORE}),
            handler=deny,
            can_deny=True,
        )
    )
    outcome = await manager.before_tool_call(
        tool_name="bash", arguments={"command": "ls"}, session_id="s1", run_id="r1"
    )

    assert outcome.denied is True
    assert outcome.reason == "blocked by policy hook"
    assert outcome.failed_closed is False
    assert outcome.hook_ids == ["deny"]
    types = _recorded_types(recorder)
    assert types == [EventType.HOOK_STARTED, EventType.HOOK_DENIED]
    assert audit.entries[0]["actor"] == "hook"
    assert audit.entries[0]["action"] == "hook.denied"


@pytest.mark.asyncio
async def test_deny_is_ignored_without_can_deny(tmp_path: Path) -> None:
    manager, recorder, _ = make_manager(tmp_path)

    async def deny(invocation: object) -> HookDecision:
        return HookDecision(action="deny", reason="nope")

    manager.register(
        HookRegistration(
            id="observer",
            events=frozenset({HookEvent.TOOL_BEFORE}),
            handler=deny,
        )
    )
    outcome = await manager.before_tool_call(tool_name="bash", session_id="s1")

    assert outcome.denied is False
    assert outcome.results[0].status == "skipped"
    assert outcome.results[0].error is not None
    assert "deny ignored" in outcome.results[0].error
    assert EventType.HOOK_DENIED not in _recorded_types(recorder)


@pytest.mark.asyncio
async def test_fail_open_continues(tmp_path: Path) -> None:
    manager, _, audit = make_manager(tmp_path)

    async def boom(invocation: object) -> None:
        raise RuntimeError("hook exploded")

    manager.register(
        HookRegistration(id="boom", events=frozenset({HookEvent.TOOL_BEFORE}), handler=boom)
    )
    outcome = await manager.before_tool_call(tool_name="bash", session_id="s1")

    assert outcome.denied is False
    assert outcome.failed_closed is False
    assert outcome.results[0].status == "failed"
    assert "RuntimeError" in (outcome.results[0].error or "")
    assert audit.entries[0]["action"] == "hook.failed"


@pytest.mark.asyncio
async def test_fail_closed_timeout_denies_tool(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path)

    async def slow(invocation: object) -> None:
        await asyncio.sleep(10)

    manager.register(
        HookRegistration(
            id="slow",
            events=frozenset({HookEvent.TOOL_BEFORE}),
            handler=slow,
            timeout_s=0.05,
            fail_mode="closed",
        )
    )
    started = time.monotonic()
    outcome = await manager.before_tool_call(tool_name="bash", session_id="s1")
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert outcome.denied is True
    assert outcome.failed_closed is True
    assert outcome.results[0].status == "timeout"
    assert "fail-closed" in outcome.reason


@pytest.mark.asyncio
async def test_timeout_cannot_be_defeated_by_swallowing_cancellation(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path)

    async def stubborn(invocation: object) -> HookDecision:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)
            return HookDecision(action="deny", reason="late deny")
        return HookDecision(action="continue")

    manager.register(
        HookRegistration(
            id="stubborn",
            events=frozenset({HookEvent.TOOL_BEFORE}),
            handler=stubborn,
            timeout_s=0.05,
            fail_mode="closed",
            can_deny=True,
        )
    )
    started = time.monotonic()
    outcome = await manager.before_tool_call(tool_name="bash", session_id="s1")
    assert time.monotonic() - started < 1.0
    assert outcome.results[0].status == "timeout"
    assert outcome.denied is True
    assert "late deny" not in outcome.reason


@pytest.mark.asyncio
async def test_cancellation_propagates(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path)

    async def cancelled(invocation: object) -> None:
        raise asyncio.CancelledError

    async def run_cancelled(invocation: object) -> None:
        raise RunCancelled("user cancelled")

    manager.register(
        HookRegistration(
            id="cancelled", events=frozenset({HookEvent.TOOL_BEFORE}), handler=cancelled
        )
    )
    with pytest.raises(asyncio.CancelledError):
        await manager.before_tool_call(tool_name="bash")

    manager.unregister("cancelled")
    manager.register(
        HookRegistration(
            id="run-cancelled",
            events=frozenset({HookEvent.TOOL_BEFORE}),
            handler=run_cancelled,
        )
    )
    with pytest.raises(RunCancelled):
        await manager.before_tool_call(tool_name="bash")


# ------------------------------------------------------------------- model hook


@pytest.mark.asyncio
async def test_model_context_append_is_bounded_and_redacted(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path, max_context_chars=60)

    async def append(invocation: object) -> HookDecision:
        return HookDecision(context_append="api_key=sk-abcdefghijklmnop " + "z" * 500)

    manager.register(
        HookRegistration(
            id="context",
            events=frozenset({HookEvent.MODEL_BEFORE}),
            handler=append,
            can_append_context=True,
        )
    )
    outcome = await manager.before_model_call(model="m", messages=3, tools=2, session_id="s1")

    assert 0 < len(outcome.context_append) <= 60
    assert "sk-abcdefghijklmnop" not in outcome.context_append
    assert "[REDACTED]" in outcome.context_append
    assert len(outcome.context_append) <= manager.config.max_context_chars


@pytest.mark.asyncio
async def test_context_append_budget_spans_multiple_hooks(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path, max_context_chars=30)

    async def first(invocation: object) -> HookDecision:
        return HookDecision(context_append="a" * 20)

    async def second(invocation: object) -> HookDecision:
        return HookDecision(context_append="b" * 20)

    manager.register(
        HookRegistration(
            id="first",
            events=frozenset({HookEvent.MODEL_BEFORE}),
            handler=first,
            can_append_context=True,
            priority=1,
        )
    )
    manager.register(
        HookRegistration(
            id="second",
            events=frozenset({HookEvent.MODEL_BEFORE}),
            handler=second,
            can_append_context=True,
        )
    )
    outcome = await manager.before_model_call(session_id="s1")

    assert len(outcome.context_append) <= 30
    assert outcome.context_append.startswith("a" * 20)
    assert "truncated" in (outcome.results[1].error or "")


@pytest.mark.asyncio
async def test_context_append_on_non_model_event_is_rejected(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path)

    async def append(invocation: object) -> HookDecision:
        return HookDecision(context_append="should not appear")

    manager.register(
        HookRegistration(
            id="tool-observer",
            events=frozenset({HookEvent.TOOL_AFTER}),
            handler=append,
        )
    )
    outcome = await manager.emit(HookEvent.TOOL_AFTER)

    assert outcome.context_append == ""
    assert outcome.results[0].status == "skipped"
    assert "context_append ignored" in (outcome.results[0].error or "")


# --------------------------------------------------------------- recursion guard


@pytest.mark.asyncio
async def test_self_triggering_event_is_rejected(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path)
    nested: list[object] = []

    async def reenter(invocation: object) -> None:
        nested.append(await manager.emit(HookEvent.TOOL_AFTER, session_id="s1"))

    manager.register(
        HookRegistration(id="reenter", events=frozenset({HookEvent.TOOL_AFTER}), handler=reenter)
    )
    await manager.emit(HookEvent.TOOL_AFTER, session_id="s1")

    assert len(nested) == 1
    result = nested[0]
    assert result.failed_closed is True  # type: ignore[attr-defined]
    assert "recursion" in result.reason  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_max_depth_stops_cross_event_recursion(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path, max_depth=2)
    reached: list[str] = []

    async def first(invocation: object) -> None:
        await manager.emit(HookEvent.TOOL_AFTER, session_id="s1")

    async def second(invocation: object) -> None:
        await manager.emit(HookEvent.TASK_STARTED, session_id="s1")

    async def third(invocation: object) -> None:
        reached.append("third")

    manager.register(
        HookRegistration(id="first", events=frozenset({HookEvent.TOOL_BEFORE}), handler=first)
    )
    manager.register(
        HookRegistration(id="second", events=frozenset({HookEvent.TOOL_AFTER}), handler=second)
    )
    manager.register(
        HookRegistration(id="third", events=frozenset({HookEvent.TASK_STARTED}), handler=third)
    )
    await manager.emit(HookEvent.TOOL_BEFORE, session_id="s1")

    assert reached == []


# ------------------------------------------------------------------ bus adapter


@pytest.mark.asyncio
async def test_bus_events_map_to_observation_hooks(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path)
    seen: list[tuple[HookEvent, str, dict[str, Any], object]] = []

    async def observe(invocation: object) -> None:
        seen.append(
            (
                invocation.event,  # type: ignore[attr-defined]
                invocation.session_id,  # type: ignore[attr-defined]
                invocation.payload,  # type: ignore[attr-defined]
                invocation.fence,  # type: ignore[attr-defined]
            )
        )

    manager.register(
        HookRegistration(
            id="observer",
            events=frozenset(
                {
                    HookEvent.TOOL_AFTER,
                    HookEvent.TASK_COMPLETED,
                    HookEvent.CHECKPOINT_CREATED,
                    HookEvent.RECOVERY_STARTED,
                }
            ),
            handler=observe,
        )
    )
    for event_type in (
        EventType.TOOL_CALL_COMPLETED,
        EventType.TASK_COMPLETED,
        EventType.CHECKPOINT_CREATED,
        EventType.RECOVERY_STARTED,
    ):
        await manager.on_bus_event(
            Event(session_id="s1", type=event_type, payload={"fact": event_type.value})
        )

    assert [entry[0] for entry in seen] == [
        HookEvent.TOOL_AFTER,
        HookEvent.TASK_COMPLETED,
        HookEvent.CHECKPOINT_CREATED,
        HookEvent.RECOVERY_STARTED,
    ]
    assert all(entry[1] == "s1" for entry in seen)
    assert all(entry[3] is None for entry in seen)

    # HOOK_* bookkeeping and deltas are never mapped back into hooks.
    await manager.on_bus_event(Event(session_id="s1", type=EventType.HOOK_COMPLETED))
    await manager.on_bus_event(Event(session_id="s1", type=EventType.MODEL_TEXT_DELTA))
    assert len(seen) == 4


@pytest.mark.asyncio
async def test_bus_observer_failure_never_escapes(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path)

    async def boom(invocation: object) -> None:
        raise RuntimeError("observer broken")

    manager.register(
        HookRegistration(id="boom", events=frozenset({HookEvent.TASK_COMPLETED}), handler=boom)
    )
    await manager.on_bus_event(Event(session_id="s1", type=EventType.TASK_COMPLETED))


# ------------------------------------------------------------ bookkeeping safety


@pytest.mark.asyncio
async def test_event_and_audit_payloads_are_redacted(tmp_path: Path) -> None:
    manager, recorder, audit = make_manager(tmp_path)

    async def deny(invocation: object) -> HookDecision:
        return HookDecision(action="deny", reason="blocked token=sk-abcdefghijklmnop")

    manager.register(
        HookRegistration(
            id="deny",
            events=frozenset({HookEvent.TOOL_BEFORE}),
            handler=deny,
            can_deny=True,
        )
    )
    await manager.before_tool_call(tool_name="bash", session_id="s1")

    serialized = json.dumps(recorder.records, default=str)
    assert "sk-abcdefghijklmnop" not in serialized
    assert "[REDACTED]" in serialized
    audit_serialized = json.dumps(audit.entries, default=str)
    assert "sk-abcdefghijklmnop" not in audit_serialized
    assert "[REDACTED]" in audit_serialized


@pytest.mark.asyncio
async def test_bookkeeping_failures_are_suppressed(tmp_path: Path) -> None:
    manager = HookManager(
        config=HooksConfig(),
        recorder=RaisingRecorder(),  # type: ignore[arg-type]
        audit=RaisingAudit(),  # type: ignore[arg-type]
        workspace=tmp_path,
    )

    async def deny(invocation: object) -> HookDecision:
        return HookDecision(action="deny", reason="blocked")

    manager.register(
        HookRegistration(
            id="deny",
            events=frozenset({HookEvent.TOOL_BEFORE}),
            handler=deny,
            can_deny=True,
        )
    )
    outcome = await manager.before_tool_call(tool_name="bash", session_id="s1")
    assert outcome.denied is True


@pytest.mark.asyncio
async def test_disabled_manager_emits_nothing(tmp_path: Path) -> None:
    manager, recorder, _ = make_manager(tmp_path, enabled=False)

    async def deny(invocation: object) -> HookDecision:
        return HookDecision(action="deny", reason="blocked")

    manager.register(
        HookRegistration(
            id="deny",
            events=frozenset({HookEvent.TOOL_BEFORE}),
            handler=deny,
            can_deny=True,
        )
    )
    outcome = await manager.before_tool_call(tool_name="bash", session_id="s1")
    assert outcome.denied is False
    assert recorder.records == []
