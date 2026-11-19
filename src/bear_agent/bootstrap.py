"""Composition root.

All concrete adapters are wired here; the kernel never constructs a database,
provider or UI. This is the only place that knows the whole object graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bear_agent.config.models import Settings
from bear_agent.context.builder import ContextBuilder
from bear_agent.core.modes import PermissionMode
from bear_agent.observability.bus import EventBus
from bear_agent.observability.recorder import EventRecorder
from bear_agent.policy.approver import Approver, AutoApprover
from bear_agent.policy.engine import PermissionEngine
from bear_agent.policy.path_guard import PathGuard
from bear_agent.providers.base import ModelProvider
from bear_agent.providers.registry import create_provider
from bear_agent.runtime.model_caller import ModelCaller
from bear_agent.runtime.runtime import AgentRuntime
from bear_agent.runtime.turn_executor import TurnExecutor
from bear_agent.sessions.service import SessionService
from bear_agent.storage.database import Database
from bear_agent.tools.builtin import create_builtin_tools
from bear_agent.tools.executor import ToolExecutor
from bear_agent.tools.registry import ToolRegistry


@dataclass
class Container:
    settings: Settings
    db: Database
    sessions: SessionService
    bus: EventBus
    recorder: EventRecorder
    provider: ModelProvider
    registry: ToolRegistry
    path_guard: PathGuard
    policy: PermissionEngine
    approver: Approver
    tool_executor: ToolExecutor
    context_builder: ContextBuilder
    model_caller: ModelCaller
    turn_executor: TurnExecutor
    runtime: AgentRuntime

    async def close(self) -> None:
        try:
            await self.provider.aclose()
        finally:
            self.db.close()


def build_container(
    settings: Settings,
    *,
    cwd: str | Path,
    approver: Approver | None = None,
    provider: ModelProvider | None = None,
    bus: EventBus | None = None,
) -> Container:
    data_dir = settings.ensure_data_dirs()
    db = Database(data_dir / "bear.db")
    try:
        sessions = SessionService(db)
        event_bus = bus or EventBus()
        recorder = EventRecorder(sessions, event_bus)
        model_provider = provider or create_provider(settings)

        registry = ToolRegistry()
        for tool in create_builtin_tools():
            registry.register(tool)

        path_guard = PathGuard(cwd)
        policy = PermissionEngine(settings.agent.permission_mode)
        chosen_approver = approver or AutoApprover()
        tool_executor = ToolExecutor(
            registry=registry,
            sessions=sessions,
            policy=policy,
            approver=chosen_approver,
            recorder=recorder,
            path_guard=path_guard,
        )
        context_builder = ContextBuilder(
            cwd=cwd,
            context_window=settings.agent.context_window,
            max_output_tokens=settings.agent.max_output_tokens,
            system_prompt_extra=settings.agent.system_prompt_extra,
        )
        model_caller = ModelCaller(model_provider)
        turn_executor = TurnExecutor(
            sessions=sessions,
            context_builder=context_builder,
            model_caller=model_caller,
            tool_executor=tool_executor,
            registry=registry,
            recorder=recorder,
            model=settings.agent.default_model,
            provider_name=settings.agent.default_provider,
            temperature=settings.agent.temperature,
            max_output_tokens=settings.agent.max_output_tokens,
            stream=settings.agent.stream,
        )
        runtime = AgentRuntime(
            sessions=sessions,
            turn_executor=turn_executor,
            recorder=recorder,
            max_turns=settings.agent.max_turns,
            max_tool_calls=settings.agent.max_tool_calls,
            max_run_seconds=settings.agent.max_run_seconds,
        )
        return Container(
            settings=settings,
            db=db,
            sessions=sessions,
            bus=event_bus,
            recorder=recorder,
            provider=model_provider,
            registry=registry,
            path_guard=path_guard,
            policy=policy,
            approver=chosen_approver,
            tool_executor=tool_executor,
            context_builder=context_builder,
            model_caller=model_caller,
            turn_executor=turn_executor,
            runtime=runtime,
        )
    except BaseException:
        db.close()
        raise


def permission_mode(value: str) -> PermissionMode:
    try:
        return PermissionMode(value)
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in PermissionMode)
        raise ValueError(f"unknown permission mode {value!r}; choose {allowed}") from exc
