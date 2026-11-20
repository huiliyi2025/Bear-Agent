"""Deterministic providers and harness fixtures for benchmarks.

Everything here is production-neutral: fake providers never touch the network,
benchmark settings are copies, and each iteration has its own data directory.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from bear_agent.benchmarks.models import BenchmarkContext
from bear_agent.config.models import (
    AgentConfig,
    MCPConfig,
    PluginsConfig,
    ProviderConfig,
    Settings,
    StorageConfig,
)
from bear_agent.core.cancel import CancelToken
from bear_agent.core.errors import ProviderError
from bear_agent.core.modes import PermissionMode
from bear_agent.core.usage import TokenUsage
from bear_agent.policy.approver import Approver, AutoApprover
from bear_agent.providers.base import (
    FinishEvent,
    ModelCapabilities,
    ModelEvent,
    ModelProvider,
    ModelRequest,
    TextDelta,
    ToolCallArgumentsDelta,
    ToolCallEnd,
    ToolCallStart,
    UsageEvent,
)


class ScriptedProvider(ModelProvider):
    """Deterministic provider: one script per stream call."""

    def __init__(
        self,
        scripts: list[list[ModelEvent]],
        *,
        name: str = "bench",
        delay_s: float = 0.0,
    ) -> None:
        self.name = name
        self.capabilities = ModelCapabilities(context_window=32_000, max_output_tokens=1_000)
        self._scripts = [list(script) for script in scripts]
        self.delay_s = delay_s
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if not self._scripts:
            yield TextDelta(text="(script exhausted)")
            yield FinishEvent(reason="stop")
            return
        for event in self._scripts.pop(0):
            cancel.raise_if_cancelled()
            await asyncio.sleep(0)
            yield event

    async def aclose(self) -> None:
        return None


class FlakyProvider(ModelProvider):
    """Raises a fixed ProviderError before emitting events, then succeeds."""

    def __init__(
        self,
        failures: int = 1,
        *,
        category: str = "rate_limit",
        retryable: bool = True,
        text: str = "ok",
        delay_s: float = 0.0,
    ) -> None:
        self.name = "flaky"
        self.capabilities = ModelCapabilities(context_window=32_000, max_output_tokens=1_000)
        self.failures = failures
        self.category = category
        self.retryable = retryable
        self.text = text
        self.delay_s = delay_s
        self.calls = 0

    async def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        self.calls += 1
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.failures > 0:
            self.failures -= 1
            raise ProviderError(
                f"scripted {self.category} failure",
                category=self.category,
                retryable=self.retryable,
            )
        yield TextDelta(text=self.text)
        yield UsageEvent(usage=TokenUsage(input_tokens=8, output_tokens=3))
        yield FinishEvent(reason="stop")

    async def aclose(self) -> None:
        return None


def usage(input_tokens: int = 10, output_tokens: int = 5) -> UsageEvent:
    return UsageEvent(usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens))


def text_script(text: str) -> list[ModelEvent]:
    return [TextDelta(text=text), usage(), FinishEvent(reason="stop")]


def tool_script(
    call_id: str,
    name: str,
    arguments: dict[str, Any],
    *,
    text: str = "",
    finish: str = "tool_calls",
) -> list[ModelEvent]:
    events: list[ModelEvent] = []
    if text:
        events.append(TextDelta(text=text))
    events.extend(
        [
            ToolCallStart(index=0, id=call_id, name=name),
            ToolCallArgumentsDelta(index=0, delta=json.dumps(arguments)),
            ToolCallEnd(index=0),
            usage(),
            FinishEvent(reason=finish),
        ]
    )
    return events


def bench_settings(
    ctx: BenchmarkContext,
    *,
    data_dir: Path | None = None,
    permission_mode: PermissionMode = PermissionMode.AUTO,
    context_overrides: dict[str, Any] | None = None,
    task_overrides: dict[str, Any] | None = None,
    mcp_enabled: bool = False,
) -> Settings:
    """Copy user settings into a deterministic, offline benchmark profile."""
    settings = ctx.settings.model_copy(deep=True)
    settings.storage = StorageConfig(data_dir=data_dir or ctx.data_dir)
    settings.providers = {
        "bench": ProviderConfig(
            base_url="http://bench.local/v1",
            api_key="bench-only",
            models=["bench-model"],
            max_retries=0,
        )
    }
    settings.agent = AgentConfig(
        default_provider="bench",
        default_model="bench-model",
        max_turns=settings.agent.max_turns,
        max_tool_calls=settings.agent.max_tool_calls,
        max_run_seconds=0.0,
        context_window=settings.agent.context_window,
        max_output_tokens=settings.agent.max_output_tokens,
        permission_mode=permission_mode,
        stream=settings.agent.stream,
        provider_fallback_enabled=False,
    )
    settings.mcp = MCPConfig(enabled=mcp_enabled, servers={})
    settings.plugins = PluginsConfig(enabled=False)
    settings.hooks.enabled = False
    settings.hooks.commands = []
    # Harness tracing/metrics are opt-in for benchmarks; otherwise every
    # measured Runtime call would write diagnostic rows. The runner's --trace
    # option uses its own TraceService outside this profile.
    settings.observability.enabled = False
    settings.observability.traces_enabled = False
    settings.observability.metrics_enabled = False
    if context_overrides:
        for key, value in context_overrides.items():
            setattr(settings.context, key, value)
    if task_overrides:
        for key, value in task_overrides.items():
            setattr(settings.tasks, key, value)
    return settings


def build_bench_container(
    ctx: BenchmarkContext,
    scripts: list[list[ModelEvent]],
    *,
    settings: Settings | None = None,
    approver: Approver | None = None,
    provider: ModelProvider | None = None,
) -> tuple[Any, ModelProvider]:
    from bear_agent.bootstrap import build_container

    chosen = provider or ScriptedProvider(scripts)
    container = build_container(
        settings or bench_settings(ctx),
        cwd=ctx.workspace,
        approver=approver or AutoApprover(),
        provider=chosen,
    )
    # Register immediately so a failure in later fixture setup still lets the
    # runner close the database/handles instead of leaking the temp directory.
    ctx.state["container"] = container
    return container, chosen


async def new_session(container: Any) -> Any:
    return await container.sessions.create(
        cwd=container.cwd,
        model=container.settings.agent.default_model,
        provider=container.settings.agent.default_provider,
    )
