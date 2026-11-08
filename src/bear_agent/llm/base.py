"""Core types shared across the LLM layer (independent of any provider SDK)."""

from __future__ import annotations

import json
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]


class ToolSpec(BaseModel):
    """A tool's JSON Schema, sent to the model as an OpenAI-style function tool."""

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolCall(BaseModel):
    """A single tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    """One chat turn. Models both our internal state and the API payload."""

    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    def to_openai(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role, "content": self.content or ""}
        if self.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in self.tool_calls
            ]
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        if self.name:
            payload["name"] = self.name
        return payload


class LLMResponse(BaseModel):
    """Parsed reply from a model call."""

    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    finish_reason: str = ""
    usage: dict[str, int] | None = None

    @property
    def wants_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class LLMError(RuntimeError):
    """Raised on transport / protocol failure from the upstream API."""


class FinishReason:
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"


class LLMClient(Protocol):
    """The only contract the Agent Loop depends on.

    Anything implementing this protocol can drive the loop: OpenAI-compatible
    REST endpoints, Ollama, mock/fake clients in tests, or future providers.
    """

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        temperature: float | None = None,
    ) -> LLMResponse: ...
