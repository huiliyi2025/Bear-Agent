"""Kernel, tool and context benchmarks."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from bear_agent.benchmarks.fixtures import (
    ScriptedProvider,
    bench_settings,
    build_bench_container,
    new_session,
    text_script,
)
from bear_agent.benchmarks.models import (
    BenchmarkContext,
    BenchmarkSample,
    BenchmarkSkipped,
)
from bear_agent.config.models import ApprovalConfig
from bear_agent.context.builder import ContextBuilder
from bear_agent.core.cancel import CancelToken
from bear_agent.core.clock import SystemClock
from bear_agent.core.events import EventType
from bear_agent.core.fencing import LeaseFence
from bear_agent.core.messages import AssistantMessage, ToolCall, ToolMessage
from bear_agent.observability.trace import span
from bear_agent.observability.trace.service import TraceService
from bear_agent.policy.approvals import ApprovalService
from bear_agent.policy.approver import AutoApprover
from bear_agent.policy.models import ApprovalRequest
from bear_agent.policy.path_guard import PathGuard
from bear_agent.sessions.claims import ClaimService
from bear_agent.sessions.service import SessionService
from bear_agent.shell.sandbox.base import SandboxManager
from bear_agent.storage.database import Database
from bear_agent.storage.security_repositories import ApprovalRepository, ClaimRepository
from bear_agent.tools.builtin import create_builtin_tools
from bear_agent.tools.registry import ToolRegistry


class _Scenario:
    category = "kernel"
    kind = "synthetic"
    requires_network = False
    default_params: ClassVar[dict[str, Any]] = {}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        return None

    async def cleanup(self, ctx: BenchmarkContext) -> None:
        return None

    def _count(self, ctx: BenchmarkContext, key: str, fallback: int) -> int:
        value = int(ctx.params.get(key, fallback))
        if value <= 0:
            raise ValueError(f"{key} must be positive")
        return value


# --------------------------------------------------------------------- kernel


class SessionCreateScenario(_Scenario):
    id = "kernel.session_create"
    name = "Session create"
    description = "Create N sessions with the real SessionService/SQLite schema."
    default_params = {"count": 100}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        db = Database(ctx.data_dir / "sessions.sqlite")
        ctx.state["db"] = db
        ctx.state["sessions"] = SessionService(db)

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        sessions: SessionService = ctx.state["sessions"]
        count = self._count(ctx, "count", 100)
        for index in range(count):
            await sessions.create(
                cwd=ctx.workspace, model="bench-model", provider="bench", title=f"s{index}"
            )
        return BenchmarkSample(operations=count)

    async def cleanup(self, ctx: BenchmarkContext) -> None:
        db = ctx.state.get("db")
        if db is not None:
            db.close()


class EventWriteScenario(_Scenario):
    id = "kernel.event_write"
    name = "Event append"
    description = "Append N persisted fact events to one session transcript log."
    default_params = {"count": 200}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        db = Database(ctx.data_dir / "events.sqlite")
        sessions = SessionService(db)
        session = await sessions.create(cwd=ctx.workspace, model="m", provider="p")
        ctx.state.update(db=db, sessions=sessions, session=session)

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        sessions: SessionService = ctx.state["sessions"]
        session = ctx.state["session"]
        count = self._count(ctx, "count", 200)
        for index in range(count):
            await sessions.append_event(
                session.id,
                _event_type(index),
                payload={"index": index, "value": "x" * 32},
            )
        return BenchmarkSample(operations=count)

    async def cleanup(self, ctx: BenchmarkContext) -> None:
        db = ctx.state.get("db")
        if db is not None:
            db.close()


class TraceWriteScenario(_Scenario):
    id = "kernel.trace_write"
    name = "Trace/span write"
    description = "Create one root trace and N child spans through the real TraceService."
    default_params = {"count": 200}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        db = Database(ctx.data_dir / "trace.sqlite")
        tracer = TraceService(db, config=ctx.settings.observability, redactor=ctx.redactor)
        ctx.state.update(db=db, tracer=tracer)

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        tracer: TraceService = ctx.state["tracer"]
        if not tracer.enabled:
            raise BenchmarkSkipped("tracing is disabled by configuration")
        count = self._count(ctx, "count", 200)
        async with tracer.trace("bench", kind="benchmark", session_id="benchmark"):
            for index in range(count):
                async with span("unit", "internal", {"index": index}):
                    await asyncio.sleep(0)
        stats = await tracer.stats()
        if stats["traces"] == 0:
            raise BenchmarkSkipped("trace sampling skipped this benchmark run")
        if stats["spans"] < count + 1:
            raise RuntimeError(
                f"trace storage wrote {stats['spans']} span(s), expected at least {count + 1}"
            )
        return BenchmarkSample(
            operations=count,
            metadata={"spans": stats["spans"], "traces": stats["traces"]},
        )

    async def cleanup(self, ctx: BenchmarkContext) -> None:
        tracer = ctx.state.get("tracer")
        if tracer is not None:
            await tracer.close()
        db = ctx.state.get("db")
        if db is not None:
            db.close()


class TraceWriteDisabledScenario(TraceWriteScenario):
    id = "kernel.trace_write_disabled"
    name = "Trace disabled baseline"
    description = "The same loop as trace_write without starting a trace or spans."

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        count = self._count(ctx, "count", 200)
        for index in range(count):
            await asyncio.sleep(0)
            _ = index
        return BenchmarkSample(operations=count)


class RegistryLookupScenario(_Scenario):
    id = "kernel.tool_registry_lookup"
    name = "Tool registry lookup"
    description = "Resolve tool names from a real ToolRegistry N times."
    default_params = {"count": 20_000}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        registry = ToolRegistry()
        for tool in create_builtin_tools():
            registry.register(tool)
        ctx.state["registry"] = registry

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        registry: ToolRegistry = ctx.state["registry"]
        count = self._count(ctx, "count", 20_000)
        names = registry.names()
        for index in range(count):
            registry.get(names[index % len(names)])
        return BenchmarkSample(operations=count)


class ContextBudgetScenario(_Scenario):
    id = "kernel.context_budget"
    name = "Context budget build"
    description = "Build the token-budgeted model context from a fixed transcript."
    default_params = {"count": 200, "messages": 80}
    category = "context"

    async def prepare(self, ctx: BenchmarkContext) -> None:
        builder = ContextBuilder(
            cwd=ctx.workspace,
            context_window=128_000,
            max_output_tokens=4_096,
        )
        messages = [
            AssistantMessage.of(f"assistant message {index} " + "x" * 200)
            for index in range(int(ctx.params.get("messages", 80)))
        ]
        ctx.state.update(builder=builder, messages=messages)

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        builder: ContextBuilder = ctx.state["builder"]
        messages = ctx.state["messages"]
        count = self._count(ctx, "count", 200)
        for _ in range(count):
            builder.build(messages, [])
        return BenchmarkSample(operations=count)


# ----------------------------------------------------------------------- tools


class ToolReadScenario(_Scenario):
    id = "tool.read_file"
    name = "read_file through ToolExecutor"
    description = "Execute read_file through policy, claims, audit and persistence."
    category = "tool"
    default_params = {"calls": 50}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        (ctx.workspace / "hello.txt").write_text("hello bench\n" * 20, encoding="utf-8")
        container, _ = build_bench_container(ctx, [])
        ctx.state["container"] = container
        session = await new_session(container)
        epoch = await container.sessions.acquire_lease(session.id, "bench-owner", 300)
        ctx.state.update(session=session, epoch=epoch)

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        container = ctx.state["container"]
        session = ctx.state["session"]
        calls = self._count(ctx, "calls", 50)
        for index in range(calls):
            result = await container.tool_executor.execute(
                ToolCall(
                    id=f"call_read_{index}",
                    name="read_file",
                    arguments={"path": "hello.txt"},
                ),
                session_id=session.id,
                project_id=session.project_id,
                lease_owner="bench-owner",
                lease_epoch=ctx.state["epoch"],
                run_id="bench-run",
                turn_id="bench-turn",
                cancel=CancelToken(),
            )
            if result.is_error:
                raise RuntimeError(result.content)
        return BenchmarkSample(operations=calls)

    async def cleanup(self, ctx: BenchmarkContext) -> None:
        container = ctx.state.get("container")
        if container is not None:
            await container.close()


class ToolWriteScenario(ToolReadScenario):
    id = "tool.write_file"
    name = "write_file through ToolExecutor"
    description = "Execute write_file (fsync + atomic replace + claims/audit) N times."

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        container = ctx.state["container"]
        session = ctx.state["session"]
        calls = self._count(ctx, "calls", 50)
        for index in range(calls):
            result = await container.tool_executor.execute(
                ToolCall(
                    id=f"call_write_{index}",
                    name="write_file",
                    arguments={"path": f"out_{index}.txt", "content": "bench data\n" * 20},
                ),
                session_id=session.id,
                project_id=session.project_id,
                lease_owner="bench-owner",
                lease_epoch=ctx.state["epoch"],
                run_id="bench-run",
                turn_id="bench-turn",
                cancel=CancelToken(),
            )
            if result.is_error:
                raise RuntimeError(result.content)
        return BenchmarkSample(operations=calls)


class ToolSearchScenario(ToolReadScenario):
    id = "tool.grep"
    name = "grep through ToolExecutor"
    description = "Search a small workspace with the grep tool."
    default_params = {"calls": 20}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        for index in range(20):
            (ctx.workspace / f"file_{index}.py").write_text(
                "\n".join(f"line {line} needle {index}" for line in range(50)),
                encoding="utf-8",
            )
        await super().prepare(ctx)

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        container = ctx.state["container"]
        session = ctx.state["session"]
        calls = self._count(ctx, "calls", 20)
        for index in range(calls):
            result = await container.tool_executor.execute(
                ToolCall(
                    id=f"call_grep_{index}",
                    name="grep",
                    arguments={"pattern": "needle", "root": ".", "max_results": 50},
                ),
                session_id=session.id,
                project_id=session.project_id,
                lease_owner="bench-owner",
                lease_epoch=ctx.state["epoch"],
                run_id="bench-run",
                turn_id="bench-turn",
                cancel=CancelToken(),
            )
            if result.is_error:
                raise RuntimeError(result.content)
        return BenchmarkSample(operations=calls)


class ToolShellScenario(ToolReadScenario):
    id = "tool.shell"
    name = "Shell through ToolExecutor"
    description = "Run echo under the configured sandbox, command policy, claims and audit."
    default_params = {"calls": 5}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        await super().prepare(ctx)
        container = ctx.state["container"]
        try:
            container.sandbox.ensure_available()
        except Exception as exc:
            raise BenchmarkSkipped(
                f"OS sandbox unavailable on this host; shell is fail-closed: {exc}"
            ) from exc
        if not ctx.settings.shell.enabled:
            raise BenchmarkSkipped("shell is disabled in configuration")

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        container = ctx.state["container"]
        session = ctx.state["session"]
        calls = self._count(ctx, "calls", 5)
        backend = container.sandbox.describe().get("backend")
        for index in range(calls):
            result = await container.tool_executor.execute(
                ToolCall(
                    id=f"call_shell_{index}",
                    name="bash",
                    arguments={"command": "echo bear-bench"},
                ),
                session_id=session.id,
                project_id=session.project_id,
                lease_owner="bench-owner",
                lease_epoch=ctx.state["epoch"],
                run_id="bench-run",
                turn_id="bench-turn",
                cancel=CancelToken(),
            )
            if result.is_error:
                if result.error_kind in {
                    "sandbox_error",
                    "sandbox_unavailable",
                    "sandbox_or_spawn_error",
                    "exec_failure",
                    "policy_denied",
                    "approval_denied",
                    "hook_denied",
                }:
                    raise BenchmarkSkipped(
                        f"shell benchmark is unavailable under this policy: {result.content}"
                    )
                raise RuntimeError(result.content)
        description = container.sandbox.describe()
        capabilities = description.get("capabilities") or {}
        return BenchmarkSample(
            operations=calls,
            metadata={
                "sandbox_backend": backend,
                "filesystem_isolated": bool(
                    capabilities.get("filesystem", False)
                    if isinstance(capabilities, dict)
                    else False
                ),
            },
        )


class ApprovalScenario(_Scenario):
    id = "tool.approval"
    name = "Approval persistence"
    description = "Record an allow decision through the real ApprovalService/SQLite."
    category = "tool"
    default_params = {"count": 50}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        db = Database(ctx.data_dir / "approval.sqlite")
        service = ApprovalService(
            ApprovalRepository(db),
            AutoApprover(),
            clock=SystemClock(),
            session_ttl_s=ApprovalConfig().session_ttl_s,
            project_ttl_s=ApprovalConfig().project_ttl_s,
        )
        ctx.state.update(db=db, service=service)

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        service: ApprovalService = ctx.state["service"]
        count = self._count(ctx, "count", 50)
        for index in range(count):
            decision = await service.request(
                ApprovalRequest(
                    tool_name="bash",
                    arguments={"command": f"echo {index}"},
                    reason="benchmark",
                    risk="moderate",
                    session_id="bench-session",
                    project_id="bench-project",
                    fingerprint=f"bench-fingerprint-{index}",
                )
            )
            if not decision.approved:
                raise RuntimeError("benchmark approver denied")
        return BenchmarkSample(operations=count)

    async def cleanup(self, ctx: BenchmarkContext) -> None:
        db = ctx.state.get("db")
        if db is not None:
            db.close()


class ClaimScenario(_Scenario):
    id = "tool.claims"
    name = "File claim acquire/release"
    description = "Acquire and release a distinct file claim through the real ClaimService."
    category = "tool"
    default_params = {"count": 50}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        db = Database(ctx.data_dir / "claims.sqlite")
        sessions = SessionService(db)
        session = await sessions.create(cwd=ctx.workspace, model="m", provider="p")
        epoch = await sessions.acquire_lease(session.id, "bench-owner", 300)
        claims = ClaimService(
            ClaimRepository(db),
            PathGuard(ctx.workspace),
            clock=SystemClock(),
            ttl_seconds=300,
        )
        ctx.state.update(
            db=db,
            sessions=sessions,
            session=session,
            epoch=epoch,
            claims=claims,
        )

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        claims: ClaimService = ctx.state["claims"]
        session = ctx.state["session"]
        count = self._count(ctx, "count", 50)
        for index in range(count):
            path = str(ctx.workspace / f"claim_{index}.txt")
            await claims.acquire(session.id, "bench-owner", path)
            await claims.release_one(session.id, "bench-owner", path)
        return BenchmarkSample(operations=count)

    async def cleanup(self, ctx: BenchmarkContext) -> None:
        db = ctx.state.get("db")
        if db is not None:
            db.close()


class SandboxScenario(_Scenario):
    id = "tool.sandbox"
    name = "Sandbox initialization"
    description = "Select and validate the platform OS sandbox backend."
    category = "tool"
    default_params = {"count": 5}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        sandbox = SandboxManager(ctx.settings.sandbox, ctx.workspace)
        try:
            sandbox.ensure_available()
        except Exception as exc:
            raise BenchmarkSkipped(f"sandbox unavailable: {exc}") from exc
        ctx.state["sandbox"] = sandbox

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        sandbox: SandboxManager = ctx.state["sandbox"]
        count = self._count(ctx, "count", 5)
        for _ in range(count):
            sandbox.describe()
            sandbox.ensure_available()
        return BenchmarkSample(
            operations=count,
            metadata={"description": sandbox.describe()},
        )


# --------------------------------------------------------------------- context


class _CompactionScenario(_Scenario):
    category = "context"
    kind = "scripted"
    default_params = {"messages": 8, "tool_chars": 6_000}
    levels: ClassVar[set[int]] = set()

    async def prepare(self, ctx: BenchmarkContext) -> None:
        overrides = {
            "compaction_enabled": True,
            "trigger_ratio": 0.5,
            "keep_recent_turns": 0 if 3 in self.levels else 1,
            "l1_enabled": 1 in self.levels,
            "l2_enabled": 2 in self.levels,
            "l3_enabled": 3 in self.levels,
            "l4_enabled": False,
            "l1_min_tool_chars": 100,
            "l1_keep_chars": 100,
            "l3_max_input_tokens": 40_000,
            "l3_failure_limit": 0,
            "l3_cooldown_turns": 0,
        }
        settings = bench_settings(
            ctx,
            context_overrides=overrides,
        )
        settings.agent.context_window = 8_000
        if 3 in self.levels:
            provider = ScriptedProvider([text_script("summary: goal, decisions, pending work")])
        else:
            provider = ScriptedProvider([])
        container, _ = build_bench_container(ctx, [], settings=settings, provider=provider)
        session = await new_session(container)
        epoch = await container.sessions.acquire_lease(session.id, "bench-owner", 300)
        tool_chars = int(ctx.params.get("tool_chars", 6_000))
        for index in range(int(ctx.params.get("messages", 8))):
            await container.sessions.append_user_message(
                session.id, f"goal constraint {index} " + "u" * 200
            )
            await container.sessions.append_assistant_message(
                session.id, AssistantMessage.of("assistant " + "a" * 200)
            )
            await container.sessions.append_tool_message(
                session.id,
                ToolMessage.of(f"tool_{index}", ("tool output " * 20 + "z" * tool_chars)),
            )
        ctx.state.update(
            container=container,
            session=session,
            epoch=epoch,
            fence=LeaseFence("bench-owner", epoch),
        )

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        container = ctx.state["container"]
        session = ctx.state["session"]
        messages = await container.sessions.load_messages(session.id)
        prepared = await container.compactor.prepare(
            session,
            messages,
            [],
            turn_index=0,
            run_id="bench-run",
            turn_id="bench-turn",
            fence=ctx.state["fence"],
            cancel=CancelToken(),
        )
        if not prepared.compacted:
            raise RuntimeError(
                "context fixture did not trigger compaction: " + "; ".join(prepared.notes)
            )
        return BenchmarkSample(
            operations=1,
            metadata={
                "levels": prepared.levels,
                "before_tokens": prepared.before_tokens,
                "after_tokens": prepared.estimated_tokens,
                "compaction_ids": len(prepared.compaction_ids),
            },
        )

    async def cleanup(self, ctx: BenchmarkContext) -> None:
        container = ctx.state.get("container")
        if container is not None:
            await container.close()


class CompactionL1Scenario(_CompactionScenario):
    id = "context.l1_compaction"
    name = "L1 tool-output pruning"
    description = "Archive old tool output into content-addressed artifacts."
    levels = {1}


class CompactionL2Scenario(_CompactionScenario):
    id = "context.l2_compaction"
    name = "L2 deterministic summary"
    description = "Build a deterministic summary from an oversized transcript."
    levels = {2}


class CompactionL3Scenario(_CompactionScenario):
    id = "context.l3_compaction"
    name = "L3 model summary"
    description = "Summarize an oversized transcript with a ScriptedProvider."
    levels = {3}


class CheckpointCreateScenario(_Scenario):
    id = "context.checkpoint_create"
    name = "Checkpoint create"
    description = "Snapshot transcript/control state with checksum in one transaction."
    category = "context"

    async def prepare(self, ctx: BenchmarkContext) -> None:
        container, _ = build_bench_container(ctx, [])
        session = await new_session(container)
        epoch = await container.sessions.acquire_lease(session.id, "bench-owner", 300)
        for index in range(10):
            await container.sessions.append_user_message(session.id, f"message {index}")
        ctx.state.update(
            container=container,
            session=session,
            fence=LeaseFence("bench-owner", epoch),
        )

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        container = ctx.state["container"]
        session = ctx.state["session"]
        record = await container.checkpoints.create(
            session.id, fence=ctx.state["fence"], kind="bench"
        )
        return BenchmarkSample(
            operations=1,
            metadata={"checkpoint_id": record.id, "event_seq": record.event_seq},
        )

    async def cleanup(self, ctx: BenchmarkContext) -> None:
        container = ctx.state.get("container")
        if container is not None:
            await container.close()


class CheckpointRestoreScenario(CheckpointCreateScenario):
    id = "context.checkpoint_restore"
    name = "Checkpoint validate/restore"
    description = "Load and verify the newest checkpoint (checksum + bounds)."
    default_params: ClassVar[dict[str, Any]] = {}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        await super().prepare(ctx)
        container = ctx.state["container"]
        session = ctx.state["session"]
        await container.checkpoints.create(session.id, fence=ctx.state["fence"], kind="seed")

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        container = ctx.state["container"]
        session = ctx.state["session"]
        record = await container.checkpoints.latest_valid(session.id)
        if record is None:
            raise RuntimeError("checkpoint fixture is invalid")
        return BenchmarkSample(operations=1, metadata={"checkpoint_id": record.id})


def _event_type(index: int) -> EventType:
    return EventType.CONTEXT_ANCHOR_ADDED if index % 2 == 0 else EventType.CONTEXT_ANCHOR_REMOVED


def kernel_and_tool_scenarios() -> list[Any]:
    return [
        SessionCreateScenario(),
        EventWriteScenario(),
        TraceWriteScenario(),
        TraceWriteDisabledScenario(),
        RegistryLookupScenario(),
        ContextBudgetScenario(),
        ToolReadScenario(),
        ToolWriteScenario(),
        ToolSearchScenario(),
        ToolShellScenario(),
        ApprovalScenario(),
        ClaimScenario(),
        SandboxScenario(),
    ]


def context_scenarios() -> list[Any]:
    return [
        CompactionL1Scenario(),
        CompactionL2Scenario(),
        CompactionL3Scenario(),
        CheckpointCreateScenario(),
        CheckpointRestoreScenario(),
    ]
