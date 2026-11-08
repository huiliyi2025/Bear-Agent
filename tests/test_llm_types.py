"""Unit tests for the message/tool-call protocol layer."""

from bear_agent.llm.base import Message, ToolCall, ToolSpec
from bear_agent.llm.openai_compat import _parse_tool_calls


def test_tool_spec_to_openai():
    spec = ToolSpec(
        name="read_file", description="Read a file", parameters={"type": "object"}
    )
    payload = spec.to_openai()
    assert payload["type"] == "function"
    assert payload["function"]["name"] == "read_file"
    assert payload["function"]["parameters"] == {"type": "object"}


def test_message_to_openai_with_tool_calls():
    msg = Message(
        role="assistant",
        content=None,
        tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "hi"})],
    )
    payload = msg.to_openai()
    assert payload["content"] == ""
    assert payload["tool_calls"][0]["id"] == "call_1"
    assert payload["tool_calls"][0]["function"]["name"] == "echo"
    assert payload["tool_calls"][0]["function"]["arguments"] == '{"text": "hi"}'


def test_tool_message_to_openai():
    msg = Message(role="tool", content="ok", tool_call_id="call_1", name="echo")
    payload = msg.to_openai()
    assert payload["role"] == "tool"
    assert payload["tool_call_id"] == "call_1"
    assert payload["name"] == "echo"


def test_parse_tool_calls_valid_and_invalid_json():
    raw = [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
        },
        {
            "id": "c2",
            "type": "function",
            "function": {"name": "run_shell", "arguments": "not json"},
        },
        {},  # malformed entry must be skipped gracefully
    ]
    calls = _parse_tool_calls(raw)
    assert calls is not None
    assert calls[0].arguments == {"path": "a.py"}
    assert "_raw_arguments" in calls[1].arguments
    assert _parse_tool_calls(None) is None
    assert _parse_tool_calls([]) is None
