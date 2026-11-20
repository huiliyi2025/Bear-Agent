"""Task/Plan engine benchmarks."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from bear_agent.benchmarks.fixtures import (
    ScriptedProvider,
    bench_settings,
    build_bench_container,
    new_session,
    text_script,
)
from bear_agent.benchmarks.models import BenchmarkContext, BenchmarkSample
from bear_agent.core.fencing import LeaseFence
from bear_agent.runtime.model_caller import ModelCaller
from bear_agent.tasks.models import PlanDraft, TaskSpec, VerificationMode
from bear_agent.tasks.planner import PlanGenerator


def _draft(goal: str, count: int, *, chain: bool = False) -> PlanDraft:
    tasks = [
        TaskSpec(
            key=f"T{index}",
            title=f"Task {index}",
            description=f"Perform benchmark task {index}.",
            acceptance_criteria=f"Task {index} is done.",
            dependencies=[f"T{index - 1}"] if chain and index > 0 else [],
            verification=VerificationMode.EVIDENCE,
        )
        for index in range(1, count + 1)
    ]
    return PlanDraft(goal=goal, tasks=tasks)


class PlanGenerateScenario:
    id = "task.plan_generate"
    name = "Plan generation"
    description = "Generate and validate a 5-task plan from a ScriptedProvider."
    category = "task"
    kind = "scripted"
    requires_network = False
    default_params = {"tasks": 5, "count": 20}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        tasks = int(ctx.params.get("tasks", 5))
        payload = {
            "goal": "Benchmark goal",
            "tasks": [
                {
                    "key": f"T{index}",
                    "title": f"Task {index}",
                    "description": f"Perform task {index}.",
                    "acceptance_criteria": f"Task {index} done.",
                    "dependencies": [],
                    "verification": "evidence",
                }
                for index in range(1, tasks + 1)
            ],
            "notes": "",
        }
        needed = max(1_000, int(ctx.params.get("count", 20)) + 10)
        scripts = [text_script(json.dumps(payload)) for _ in range(needed)]
        provider = ScriptedProvider(scripts)
        caller = ModelCaller(provider)
        generator = PlanGenerator(caller, "bench-model", max_tasks=50)
        ctx.state["generator"] = generator

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        generator: PlanGenerator = ctx.state["generator"]
        count = int(ctx.params.get("count", 20))
        for _ in range(count):
            draft = await generator.generate("Benchmark goal")
            if not draft.tasks:
                raise RuntimeError("planner returned no tasks")
        return BenchmarkSample(operations=count)

    async def cleanup(self, ctx: BenchmarkContext) -> None:
        return None


class _TaskContainerScenario:
    category = "task"
    kind = "synthetic"
    requires_network = False
    default_params: ClassVar[dict[str, Any]] = {"tasks": 20}

    async def prepare(self, ctx: BenchmarkContext) -> None:
        settings = bench_settings(
            ctx,
            task_overrides={"verify_completion": False, "max_tasks": 100},
        )
        container, _ = build_bench_container(ctx, [], settings=settings)
        session = await new_session(container)
        epoch = await container.sessions.acquire_lease(session.id, "bench-owner", 300)
        fence = LeaseFence("bench-owner", epoch)
        count = int(ctx.params.get("tasks", 20))
        plan = await container.tasks.create_plan(
            session.id, _draft("Benchmark plan", count), fence=fence
        )
        ctx.state.update(
            container=container,
            session=session,
            fence=fence,
            plan=plan,
            task_count=count,
        )

    async def cleanup(self, ctx: BenchmarkContext) -> None:
        container = ctx.state.get("container")
        if container is not None:
            await container.close()


class TaskScheduleScenario(_TaskContainerScenario):
    id = "task.schedule"
    name = "Task scheduling"
    description = "Claim and complete N independent tasks through TaskService."
    default_params = {"tasks": 20}

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        container = ctx.state["container"]
        session = ctx.state["session"]
        fence: LeaseFence = ctx.state["fence"]
        count = int(ctx.params.get("tasks", 20))
        for index in range(count):
            claimed = await container.tasks.claim_next(session.id, fence=fence)
            if claimed is None:
                raise RuntimeError(f"task {index} was not claimable")
            task, attempt = claimed
            await container.tasks.complete_task(
                task.id,
                attempt.id,
                summary=f"benchmark task {task.task_key} completed",
                verification={"completed": True, "source": "benchmark"},
                fence=fence,
            )
        return BenchmarkSample(operations=count)


class TaskReviseScenario(_TaskContainerScenario):
    id = "task.revise"
    name = "Plan revision"
    description = "Persist a new plan version and merge stable task keys."
    default_params = {"tasks": 10, "count": 5}

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        container = ctx.state["container"]
        session = ctx.state["session"]
        fence: LeaseFence = ctx.state["fence"]
        count = int(ctx.params.get("count", 5))
        tasks = int(ctx.params.get("tasks", 10))
        for index in range(count):
            await container.tasks.revise_plan(
                session.id,
                _draft(f"Benchmark plan v{index + 2}", tasks),
                reason=f"benchmark revision {index + 1}",
                source="system",
                fence=fence,
            )
        return BenchmarkSample(operations=count)


class TaskRecoveryScenario(_TaskContainerScenario):
    id = "task.recovery"
    name = "Task crash reconciliation"
    description = "Reconcile a running attempt after a simulated crash."
    default_params = {"tasks": 5}

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        container = ctx.state["container"]
        session = ctx.state["session"]
        fence: LeaseFence = ctx.state["fence"]
        claimed = await container.tasks.claim_next(session.id, fence=fence)
        if claimed is None:
            raise RuntimeError("no task to reconcile")
        task, attempt = claimed
        reconciled = await container.tasks.reconcile_after_crash(session.id, fence=fence)
        if not reconciled:
            raise RuntimeError("running attempt was not reconciled")
        return BenchmarkSample(
            operations=1,
            metadata={
                "task_id": task.id,
                "attempt_id": attempt.id,
                "reconciled": len(reconciled),
            },
        )


class TaskCheckpointScenario(_TaskContainerScenario):
    id = "task.checkpoint"
    name = "Task checkpoint"
    description = "Snapshot Plan/Task state together with transcript control state."
    default_params: ClassVar[dict[str, Any]] = {"tasks": 5}

    async def run(self, ctx: BenchmarkContext) -> BenchmarkSample:
        container = ctx.state["container"]
        session = ctx.state["session"]
        fence: LeaseFence = ctx.state["fence"]
        record = await container.checkpoints.create(session.id, fence=fence, kind="task")
        return BenchmarkSample(
            operations=1,
            metadata={
                "checkpoint_id": record.id,
                "event_seq": record.event_seq,
                "message_seq": record.message_seq,
            },
        )


def task_scenarios() -> list[Any]:
    return [
        PlanGenerateScenario(),
        TaskScheduleScenario(),
        TaskReviseScenario(),
        TaskRecoveryScenario(),
        TaskCheckpointScenario(),
    ]
