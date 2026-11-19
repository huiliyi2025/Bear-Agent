"""Model provider port and streaming event vocabulary."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from bear_agent.core.cancel import CancelToken
from bear_agent.core.messages import Message
from bear_agent.core.tool_spec import ToolSpec
from bear_agent.core.usage import TokenUsage


class ModelCapabilities(BaseModel):
    context_window: int
    max_output_tokens: int
    supports_tools: bool = True
    supports_streaming: bool = True
    supports_vision: bool = False
    supports_reasoning: bool = False


class ModelRequest(BaseModel):
    model: str
    messages: list[Message]
    tools: list[ToolSpec] = Field(default_factory=list)
    temperature: float = 0.0
    max_output_tokens: int = 4096
    stream: bool = True
    metadata: dict[str, str] = Field(default_factory=dict)


class TextDelta(BaseModel):
    type: Literal["text_delta"] = "text_delta"
    text: str


class ThinkingDelta(BaseModel):
    type: Literal["thinking_delta"] = "thinking_delta"
    text: str


class ToolCallStart(BaseModel):
    type: Literal["tool_call_start"] = "tool_call_start"
    index: int
    id: str
    name: str


class ToolCallArgumentsDelta(BaseModel):
    type: Literal["tool_call_arguments_delta"] = "tool_call_arguments_delta"
    index: int
    delta: str


class ToolCallEnd(BaseModel):
    type: Literal["tool_call_end"] = "tool_call_end"
    index: int


class UsageEvent(BaseModel):
    type: Literal["usage"] = "usage"
    usage: TokenUsage


class FinishEvent(BaseModel):
    type: Literal["finish"] = "finish"
    reason: str = "stop"


class ProviderErrorEvent(BaseModel):
    type: Literal["provider_error"] = "provider_error"
    message: str
    retryable: bool = False
    category: str = "unknown"


type ModelEvent = Annotated[
    TextDelta
    | ThinkingDelta
    | ToolCallStart
    | ToolCallArgumentsDelta
    | ToolCallEnd
    | UsageEvent
    | FinishEvent
    | ProviderErrorEvent,
    Field(discriminator="type"),
]


class ModelProvider(ABC):
    name: str
    capabilities: ModelCapabilities

    @abstractmethod
    def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        """Yield streaming events; raise ``ProviderError`` on failure."""
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release network resources. Safe to call multiple times."""
        return None
