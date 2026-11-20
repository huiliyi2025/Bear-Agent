"""Internal, provider-neutral message model.

Messages are made of typed parts instead of provider-specific dicts. The
provider adapter is the only place that knows how to map them to OpenAI /
Anthropic payloads. Keeping the internal shape stable is what allows us to add
vision, thinking blocks and other providers without rewriting the runtime.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    # Set when the provider stream ended before the arguments JSON was complete.
    # Such a call must never be executed.
    arguments_error: str | None = None


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ToolCallPart(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    tool_call: ToolCall


class ToolResultPart(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    content: str
    is_error: bool = False


type Part = Annotated[
    TextPart | ToolCallPart | ToolResultPart,
    Field(discriminator="type"),
]


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    parts: list[Part] = Field(default_factory=list)
    name: str | None = None
    # Local transcript sequence; intentionally ignored by provider adapters.
    # Assigned when loading from / appending to the session transcript.
    seq: int | None = None

    def text_content(self) -> str:
        return "".join(p.text for p in self.parts if isinstance(p, TextPart))

    def tool_calls(self) -> list[ToolCall]:
        return [p.tool_call for p in self.parts if isinstance(p, ToolCallPart)]

    def tool_result(self) -> ToolResultPart | None:
        for part in self.parts:
            if isinstance(part, ToolResultPart):
                return part
        return None

    def is_empty(self) -> bool:
        return not self.parts


class SystemMessage(Message):
    role: Literal["system"] = "system"

    @classmethod
    def of(cls, text: str) -> SystemMessage:
        return cls(parts=[TextPart(text=text)])


class UserMessage(Message):
    role: Literal["user"] = "user"

    @classmethod
    def of(cls, text: str) -> UserMessage:
        return cls(parts=[TextPart(text=text)])


class AssistantMessage(Message):
    role: Literal["assistant"] = "assistant"

    @classmethod
    def of(cls, text: str) -> AssistantMessage:
        return cls(parts=[TextPart(text=text)] if text else [])

    @classmethod
    def with_tool_calls(cls, text: str | None, tool_calls: list[ToolCall]) -> AssistantMessage:
        parts: list[Part] = []
        if text:
            parts.append(TextPart(text=text))
        parts.extend(ToolCallPart(tool_call=call) for call in tool_calls)
        return cls(parts=parts)


class ToolMessage(Message):
    role: Literal["tool"] = "tool"

    @classmethod
    def of(cls, tool_call_id: str, content: str, *, is_error: bool = False) -> ToolMessage:
        return cls(
            parts=[ToolResultPart(tool_call_id=tool_call_id, content=content, is_error=is_error)]
        )
