from __future__ import annotations

from bear_agent.core.messages import (
    AssistantMessage,
    Message,
    ToolCall,
    ToolMessage,
    UserMessage,
)


def test_round_trip_user_message() -> None:
    message = UserMessage.of("hello")
    restored = Message.model_validate_json(message.model_dump_json())
    assert restored.role == "user"
    assert restored.text_content() == "hello"


def test_tool_call_round_trip() -> None:
    message = AssistantMessage.with_tool_calls(
        "thinking", [ToolCall(id="c1", name="read_file", arguments={"path": "a"})]
    )
    restored = Message.model_validate_json(message.model_dump_json())
    assert restored.text_content() == "thinking"
    calls = restored.tool_calls()
    assert len(calls) == 1
    assert calls[0].arguments == {"path": "a"}


def test_tool_message() -> None:
    message = ToolMessage.of("c1", "result", is_error=True)
    result = message.tool_result()
    assert result is not None
    assert result.tool_call_id == "c1"
    assert result.is_error
