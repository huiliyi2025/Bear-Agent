"""Test support: scripted provider, settings and container construction."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

from bear_agent.bootstrap import Container, build_container
from bear_agent.config.models import AgentConfig, ProviderConfig, Settings, StorageConfig
from bear_agent.core.cancel import CancelToken
from bear_agent.core.events import Event
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
    def __init__(self, scripts: list[list[ModelEvent]]) -> None:
        self.name = "fake"
        self.capabilities = ModelCapabilities(context_window=32_000, max_output_tokens=1_000)
        self.scripts = [list(script) for script in scripts]
        self.requests: list[ModelRequest] = []
        self.closed = False

    async def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        if not self.scripts:
            yield TextDelta(text="(script exhausted)")
            yield FinishEvent(reason="stop")
            return
        for event in self.scripts.pop(0):
            cancel.raise_if_cancelled()
            await asyncio.sleep(0)
            yield event

    async def aclose(self) -> None:
        self.closed = True


def usage(input_tokens: int = 10, output_tokens: int = 5) -> UsageEvent:
    return UsageEvent(usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens))


def text_script(text: str) -> list[ModelEvent]:
    return [TextDelta(text=text), usage(), FinishEvent(reason="stop")]


def tool_script(
    call_id: str,
    name: str,
    arguments: dict[str, object],
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


def default_settings(tmp_path: Path, **agent_overrides: object) -> Settings:
    agent = AgentConfig(
        default_provider="fake",
        default_model="fake-model",
        max_turns=8,
        max_tool_calls=50,
        context_window=32_000,
        max_output_tokens=1_000,
        **agent_overrides,
    )
    return Settings(
        providers={
            "fake": ProviderConfig(
                base_url="http://fake.local/v1",
                api_key="test",
                models=["fake-model"],
                max_retries=0,
            )
        },
        agent=agent,
        storage=StorageConfig(data_dir=tmp_path / "data"),
    )


def make_container(
    settings: Settings,
    cwd: Path,
    scripts: list[list[ModelEvent]],
    *,
    approver: Approver | None = None,
) -> tuple[Container, ScriptedProvider]:
    provider = ScriptedProvider(scripts)
    container = build_container(
        settings,
        cwd=cwd,
        approver=approver or AutoApprover(),
        provider=provider,
    )
    return container, provider


async def new_session(container: Container, cwd: Path):
    return await container.sessions.create(
        cwd=cwd,
        model=container.settings.agent.default_model,
        provider=container.settings.agent.default_provider,
    )


def collect_events(container: Container) -> list[Event]:
    events: list[Event] = []

    async def handler(event: Event) -> None:
        events.append(event)

    container.bus.subscribe(handler)
    return events


def supervised(settings: Settings) -> Settings:
    settings.agent.permission_mode = PermissionMode.SUPERVISED
    return settings
