"""End-to-end harness benchmarks using the real Runtime, ToolExecutor and TaskEngine."""

from __future__ import annotations

import sys
from typing import Any, ClassVar

from bear_agent.benchmarks.fixtures import (
    FlakyProvider,
    ScriptedProvider,
    bench_settings,
    build_bench_container,
    new_session,
    text_script,
    tool_script,
)
from bear_agent.benchmarks.models import BenchmarkContext, BenchmarkSample
from bear_agent.config.models import MCPConfig, MCPServerConfig, PluginsConfig
from bear_agent.core.events import Event, EventType
from bear_agent.core.messages import AssistantMessage, ToolCall, ToolMessage
from bear_agent.core.usage import TokenUsage
from bear_agent.providers.base import (
    FinishEvent,
    ModelEvent,
    ToolCallArgumentsDelta,
    ToolCallEnd,
    ToolCallStart,
    UsageEvent,
)
from bear_agent.providers.retry import RetryPolicy
from bear_agent.providers.router import FallbackProvider, ProviderRoute
from bear_agent.runtime.stop_policy import RunStatus
from bear_agent.tasks.models import PlanDraft, TaskSpec, VerificationMode


class _E2EScenario:
    category = "e2e"
    kind = "scripted"
    requires_network = False
    default_params: ClassVar[dict[str, Any]] = {}
    settings: ClassVar[Any] = None

    async def prepare(self, ctx: BenchmarkContext) -> None:
        return None

    async def cleanup(self, ctx: BenchmarkContext) -> None:
        container = ctx.state.get("container")
        if container is not None:
            await container.close()

    def _events(self, container: Any) -> list[Event]:
        events: list[Event] = []

        async def handler(event: Event) -> None:
            events.append(event)

        container.bus.subscribe(handler)
        return events


class SingleToolScenario(_E2EScenario):
    id = "e2e.single_tool"
    name = "Single-tool agent run"
    description = "One read_file call and a final answer through the full Runtime."
    default_params = {"count": 5}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        (ctx.workspace / "hello.txt").write_text("hello e2e\n", encoding="utf-8")
        scripts: list[list[ModelEvent]] = []
        needed = max(1_000, int(ctx.params.get("count", 5)) + 10)
        for _ in range(needed):
            scripts.append(tool_script("c1", "read_file", {"path": "hello.txt"}))
            scripts.append(text_script("done"))
        container, _ = build_bench_container(ctx, scripts)
        session = await new_session(container)
        ctx.state.update(container=container, session=session, events=self._events(container))

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        container = ctx.state["container"]
        session = ctx.state["session"]
        count = int(ctx.params.get("count", 5))
        tokens = 0
        for _ in range(count):
            result = await container.runtime.run(session.id, "read hello.txt")
            if result.status is not RunStatus.COMPLETED:
                raise RuntimeError(f"run did not complete: {result.status} {result.error}")
            tokens += result.usage.total_tokens
        events = ctx.state["events"]
        failed = [event for event in events if event.type == EventType.TOOL_CALL_FAILED]
        completed = [event for event in events if event.type == EventType.TOOL_CALL_COMPLETED]
        if failed or not completed:
            raise RuntimeError(
                f"tool path did not succeed: failed={len(failed)} completed={len(completed)}"
            )
        return BenchmarkSample(
            operations=count,
            tokens=tokens,
            metadata={
                "tool_calls": len(completed),
                "turns": sum(1 for event in events if event.type == EventType.TURN_STARTED),
            },
        )


def _multi_tool_script() -> list[ModelEvent]:
    return [
        ToolCallStart(index=0, id="m1", name="read_file"),
        ToolCallArgumentsDelta(index=0, delta='{"path": "hello.txt"}'),
        ToolCallEnd(index=0),
        ToolCallStart(index=1, id="m2", name="list_dir"),
        ToolCallArgumentsDelta(index=1, delta='{"path": "."}'),
        ToolCallEnd(index=1),
        UsageEvent(usage=TokenUsage(input_tokens=20, output_tokens=10)),
        FinishEvent(reason="tool_calls"),
    ]


class MultiToolScenario(_E2EScenario):
    id = "e2e.multi_tool"
    name = "Multi-tool agent run"
    description = "Two tool calls in one assistant turn plus a final answer."
    default_params = {"count": 5}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        (ctx.workspace / "hello.txt").write_text("hello e2e\n", encoding="utf-8")
        scripts: list[list[ModelEvent]] = []
        needed = max(1_000, int(ctx.params.get("count", 5)) + 10)
        for _ in range(needed):
            scripts.append(_multi_tool_script())
            scripts.append(text_script("done"))
        container, _ = build_bench_container(ctx, scripts)
        session = await new_session(container)
        ctx.state.update(container=container, session=session, events=self._events(container))

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        container = ctx.state["container"]
        session = ctx.state["session"]
        count = int(ctx.params.get("count", 5))
        tokens = 0
        for _ in range(count):
            result = await container.runtime.run(session.id, "inspect the workspace")
            if result.status is not RunStatus.COMPLETED:
                raise RuntimeError(f"run did not complete: {result.status}")
            tokens += result.usage.total_tokens
        events: list[Event] = ctx.state.get("events") or []
        if any(event.type == EventType.TOOL_CALL_FAILED for event in events):
            raise RuntimeError("a multi-tool benchmark call failed")
        if not any(event.type == EventType.TOOL_CALL_COMPLETED for event in events):
            raise RuntimeError("multi-tool benchmark executed no successful tool call")
        return BenchmarkSample(operations=count, tokens=tokens)


class MultiTaskScenario(_E2EScenario):
    id = "e2e.multi_task"
    name = "Multi-task plan execution"
    description = "Create and execute a 3-task plan through Runtime.run_plan."
    default_params = {"tasks": 3, "count": 1}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        tasks = int(ctx.params.get("tasks", 3))
        settings = bench_settings(
            ctx,
            task_overrides={"verify_completion": False, "max_tasks_per_run": 20},
        )
        scripts: list[list[ModelEvent]] = []
        for index in range(tasks):
            scripts.append(
                tool_script(
                    f"task_{index}",
                    "task_complete",
                    {
                        "summary": f"benchmark task {index} completed",
                        "evidence": [f"task {index} evidence"],
                    },
                )
            )
            scripts.append(text_script("task finished"))
        container, _ = build_bench_container(ctx, scripts, settings=settings)
        session = await new_session(container)
        draft = PlanDraft(
            goal="Complete the benchmark plan",
            tasks=[
                TaskSpec(
                    key=f"T{index}",
                    title=f"Task {index}",
                    description=f"Perform task {index}.",
                    acceptance_criteria=f"Task {index} is done.",
                    verification=VerificationMode.EVIDENCE,
                )
                for index in range(1, tasks + 1)
            ],
        )
        await container.tasks.create_plan(session.id, draft)
        ctx.state.update(container=container, session=session, events=self._events(container))

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        container = ctx.state["container"]
        session = ctx.state["session"]
        count = int(ctx.params.get("count", 1))
        tokens = 0
        events: list[Event] = ctx.state["events"]
        for _ in range(count):
            start = len(events)
            report = await container.runtime.run_plan(session.id)
            if report.status != "completed":
                raise RuntimeError(f"plan did not complete: {report.status} {report.issues}")
            for event in events[start:]:
                if event.type != EventType.MODEL_RESPONSE:
                    continue
                usage = event.payload.get("usage") or {}
                tokens += int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        return BenchmarkSample(
            operations=int(ctx.params.get("tasks", 3)),
            tokens=tokens,
            metadata={"plan_status": report.status, "tasks_completed": report.tasks_completed},
        )


class CompactionRunScenario(_E2EScenario):
    id = "e2e.compaction"
    name = "Agent run with context compaction"
    description = "A real run over an oversized transcript triggers layered compaction."
    default_params = {"messages": 8}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        overrides = {
            "trigger_ratio": 0.5,
            "keep_recent_turns": 1,
            "l1_enabled": True,
            "l2_enabled": True,
            "l3_enabled": False,
            "l4_enabled": False,
            "l1_min_tool_chars": 100,
            "l1_keep_chars": 100,
        }
        settings = bench_settings(ctx, context_overrides=overrides)
        settings.agent.context_window = 16_000
        container, _ = build_bench_container(
            ctx, [text_script("done") for _ in range(10)], settings=settings
        )
        session = await new_session(container)
        for index in range(int(ctx.params.get("messages", 8))):
            await container.sessions.append_user_message(
                session.id, f"goal constraint {index} " + "u" * 200
            )
            await container.sessions.append_assistant_message(
                session.id, AssistantMessage.of("assistant " + "a" * 200)
            )
            await container.sessions.append_tool_message(
                session.id,
                _tool_message(f"tool_{index}", "tool output " * 20 + "z" * 6_000),
            )
        events = self._events(container)
        ctx.state.update(container=container, session=session, events=events)

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        container = ctx.state["container"]
        session = ctx.state["session"]
        events: list[Event] = ctx.state["events"]
        start = len(events)
        result = await container.runtime.run(session.id, "continue")
        if result.status is not RunStatus.COMPLETED:
            raise RuntimeError(f"run did not complete: {result.status}")
        new_events = events[start:]
        compactions = [event for event in new_events if event.type == EventType.CONTEXT_COMPACTED]
        if not compactions:
            raise RuntimeError("compaction fixture did not trigger during the run")
        return BenchmarkSample(
            operations=1,
            tokens=result.usage.total_tokens,
            metadata={
                "compactions": len(compactions),
                "levels": [event.payload.get("level") for event in compactions],
            },
        )


class ProviderRetryRunScenario(_E2EScenario):
    id = "e2e.provider_retry"
    name = "Agent run with provider retry"
    description = "Recover from two provider failures inside a real Runtime run."
    default_params = {"count": 5}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        provider = FlakyProvider(failures=2, category="rate_limit")
        router = FallbackProvider(
            [
                ProviderRoute(
                    name="primary",
                    model="bench-model",
                    provider=provider,
                    retry_policy=RetryPolicy(
                        max_attempts=3, base_delay_s=0.0, max_delay_s=0.0, jitter=0.0
                    ),
                    capabilities=provider.capabilities,
                )
            ],
            fallback_enabled=False,
        )
        settings = bench_settings(ctx)
        container, _ = build_bench_container(ctx, [], settings=settings, provider=router)
        session = await new_session(container)
        ctx.state.update(container=container, session=session, provider=provider)

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        container = ctx.state["container"]
        session = ctx.state["session"]
        count = int(ctx.params.get("count", 5))
        tokens = 0
        for _ in range(count):
            ctx.state["provider"].failures = 2
            result = await container.runtime.run(session.id, "answer")
            if result.status is not RunStatus.COMPLETED:
                raise RuntimeError(f"run did not complete: {result.status}")
            tokens += result.usage.total_tokens
        return BenchmarkSample(
            operations=count,
            tokens=tokens,
            metadata={"provider_calls": ctx.state["provider"].calls},
        )


class ProviderFallbackRunScenario(_E2EScenario):
    id = "e2e.provider_fallback"
    name = "Agent run with provider fallback"
    description = "The primary route fails and the Runtime succeeds via fallback."
    default_params = {"count": 5}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        failing = FlakyProvider(failures=10_000, category="rate_limit")
        healthy = ScriptedProvider([text_script("fallback answer") for _ in range(100)])
        router = FallbackProvider(
            [
                ProviderRoute(
                    name="primary",
                    model="bench-model",
                    provider=failing,
                    retry_policy=RetryPolicy(max_attempts=1),
                    capabilities=failing.capabilities,
                ),
                ProviderRoute(
                    name="secondary",
                    model="bench-model",
                    provider=healthy,
                    retry_policy=RetryPolicy(max_attempts=1),
                    capabilities=healthy.capabilities,
                ),
            ],
            fallback_enabled=True,
        )
        settings = bench_settings(ctx)
        container, _ = build_bench_container(ctx, [], settings=settings, provider=router)
        session = await new_session(container)
        ctx.state.update(container=container, session=session, router=router)

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        container = ctx.state["container"]
        session = ctx.state["session"]
        count = int(ctx.params.get("count", 5))
        tokens = 0
        for _ in range(count):
            result = await container.runtime.run(session.id, "answer")
            if result.status is not RunStatus.COMPLETED:
                raise RuntimeError(f"run did not complete: {result.status}")
            tokens += result.usage.total_tokens
        router: FallbackProvider = ctx.state["router"]
        route = router.last_route
        if route is None or route[0] != "secondary":
            raise RuntimeError(f"fallback route was not used: {route}")
        return BenchmarkSample(
            operations=count,
            tokens=tokens,
            metadata={"fallback_route": route[0]},
        )


class RecoveryRunScenario(_E2EScenario):
    id = "e2e.recovery"
    name = "Agent run after a crash"
    description = "Recover a running tool plus dangling transcript tail before the run."
    default_params: ClassVar[dict[str, Any]] = {}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        (ctx.workspace / "hello.txt").write_text("hello recovery\n", encoding="utf-8")
        container, _ = build_bench_container(ctx, [text_script("recovered") for _ in range(100)])
        session = await new_session(container)
        await container.sessions.tool_calls.start(
            session.id,
            None,
            "dangling_call",
            "read_file",
            {"path": "hello.txt"},
            True,
        )
        await container.sessions.append_assistant_message(
            session.id,
            AssistantMessage.with_tool_calls(
                None,
                [ToolCall(id="dangling_call", name="read_file", arguments={"path": "hello.txt"})],
            ),
        )
        events = self._events(container)
        ctx.state.update(container=container, session=session, events=events)

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        container = ctx.state["container"]
        session = ctx.state["session"]
        events: list[Event] = ctx.state["events"]
        start = len(events)
        result = await container.runtime.run(session.id, "continue after crash")
        if result.status is not RunStatus.COMPLETED:
            raise RuntimeError(f"run did not complete: {result.status} {result.error}")
        new_events = events[start:]
        recovered = any(event.type == EventType.RECOVERY_COMPLETED for event in new_events)
        if not recovered:
            raise RuntimeError("recovery did not run")
        return BenchmarkSample(
            operations=1,
            tokens=result.usage.total_tokens,
            metadata={"recovery_events": len(new_events)},
        )


class MCPToolRunScenario(_E2EScenario):
    id = "e2e.mcp_tool"
    name = "Agent run with MCP tool"
    description = "Discover and execute a real stdio MCP tool through ToolExecutor."
    default_params = {"count": 3}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        settings = bench_settings(ctx, mcp_enabled=True)
        settings.mcp = MCPConfig(
            enabled=True,
            servers={
                "bench": MCPServerConfig(
                    transport="stdio",
                    command=sys.executable,
                    args=["-I", "-m", "bear_agent.benchmarks.mcp_echo_server"],
                    trusted=True,
                    required=True,
                    timeout_s=20.0,
                )
            },
        )
        scripts: list[list[ModelEvent]] = []
        needed = max(100, int(ctx.params.get("count", 3)) + 10)
        for _ in range(needed):
            scripts.append(tool_script("mcp1", "mcp__bench__echo", {"text": "hello mcp"}))
            scripts.append(text_script("done"))
        container, _ = build_bench_container(ctx, scripts, settings=settings)
        await container.startup()
        session = await new_session(container)
        ctx.state.update(container=container, session=session, events=self._events(container))

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        container = ctx.state["container"]
        session = ctx.state["session"]
        count = int(ctx.params.get("count", 3))
        tokens = 0
        for _ in range(count):
            result = await container.runtime.run(session.id, "echo hello")
            if result.status is not RunStatus.COMPLETED:
                raise RuntimeError(f"run did not complete: {result.status}")
            tokens += result.usage.total_tokens
        events: list[Event] = ctx.state.get("events") or []
        if not any(
            event.type == EventType.TOOL_CALL_COMPLETED
            and (event.payload.get("name") or "").startswith("mcp__")
            for event in events
        ):
            raise RuntimeError("the MCP tool did not execute successfully")
        return BenchmarkSample(operations=count, tokens=tokens)


class PluginHookRunScenario(_E2EScenario):
    id = "e2e.plugin_hook"
    name = "Agent run with plugin hook"
    description = "Load a manifest plugin whose external hook runs on tool_before."
    default_params = {"count": 3}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        (ctx.workspace / "hello.txt").write_text("hello plugin\n", encoding="utf-8")
        plugin_root = ctx.root / "plugins"
        plugin_dir = plugin_root / "bench_hook"
        plugin_dir.mkdir(parents=True)
        hook_script = plugin_dir / "hook.py"
        hook_script.write_text(
            "import json\nimport sys\n"
            "json.load(sys.stdin)\n"
            "print(json.dumps({'action': 'continue'}))\n",
            encoding="utf-8",
        )
        (plugin_dir / "plugin.toml").write_text(
            (
                "schema_version = 1\n"
                'id = "bench_hook"\n'
                'name = "Benchmark hook"\n'
                'version = "0.1.0"\n'
                "enabled_by_default = true\n"
                "\n[permissions]\n"
                "hooks = true\n"
                "\n[[hooks]]\n"
                'id = "observe"\n'
                'event = "tool_before"\n'
                f"command = {json_quote(sys.executable)}\n"
                f"args = [{json_quote('-I')}, {json_quote(str(hook_script))}]\n"
                "timeout_s = 5.0\n"
                'fail_mode = "closed"\n'
            ),
            encoding="utf-8",
        )
        settings = bench_settings(ctx)
        settings.plugins = PluginsConfig(enabled=True, dirs=[plugin_root], allow_python=False)
        settings.hooks.enabled = True
        settings.hooks.allow_external = True
        scripts: list[list[ModelEvent]] = []
        needed = max(100, int(ctx.params.get("count", 3)) + 10)
        for _ in range(needed):
            scripts.append(tool_script("p1", "read_file", {"path": "hello.txt"}))
            scripts.append(text_script("done"))
        container, _ = build_bench_container(ctx, scripts, settings=settings)
        events = self._events(container)
        await container.startup()
        session = await new_session(container)
        ctx.state.update(container=container, session=session, events=events)

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        container = ctx.state["container"]
        session = ctx.state["session"]
        events: list[Event] = ctx.state["events"]
        count = int(ctx.params.get("count", 3))
        tokens = 0
        for _ in range(count):
            start = len(events)
            result = await container.runtime.run(session.id, "read hello.txt")
            if result.status is not RunStatus.COMPLETED:
                raise RuntimeError(f"run did not complete: {result.status}")
            tokens += result.usage.total_tokens
            hooks = [
                event
                for event in events[start:]
                if event.type
                in {
                    EventType.HOOK_COMPLETED,
                    EventType.HOOK_FAILED,
                    EventType.HOOK_DENIED,
                }
            ]
            if not any(event.type == EventType.HOOK_COMPLETED for event in hooks):
                report = (
                    container.plugin_report.as_dict() if container.plugin_report is not None else {}
                )
                raise RuntimeError(
                    f"plugin hook did not complete; plugin report={report}; "
                    f"hooks={container.hooks.status()}"
                )
            if any(event.type in {EventType.HOOK_FAILED, EventType.HOOK_DENIED} for event in hooks):
                raise RuntimeError("plugin hook failed or denied the tool")
        return BenchmarkSample(
            operations=count,
            tokens=tokens,
            metadata={
                "plugin_report": (
                    list(container.plugin_report.contributions)
                    if container.plugin_report is not None
                    else []
                )
            },
        )


def json_quote(value: str) -> str:
    import json

    return json.dumps(value)


def _tool_message(call_id: str, content: str) -> ToolMessage:
    return ToolMessage.of(call_id, content)


def e2e_scenarios() -> list[Any]:
    return [
        SingleToolScenario(),
        MultiToolScenario(),
        MultiTaskScenario(),
        CompactionRunScenario(),
        ProviderRetryRunScenario(),
        ProviderFallbackRunScenario(),
        RecoveryRunScenario(),
        MCPToolRunScenario(),
        PluginHookRunScenario(),
    ]
