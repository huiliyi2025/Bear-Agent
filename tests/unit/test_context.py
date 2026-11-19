from __future__ import annotations

from pathlib import Path

import pytest

from bear_agent.context.builder import ContextBuilder
from bear_agent.context.tokens import estimate_text_tokens
from bear_agent.core.errors import ContextOverflowError
from bear_agent.core.messages import (
    AssistantMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)


def _builder(tmp_path: Path, context_window: int = 4_000) -> ContextBuilder:
    return ContextBuilder(
        cwd=tmp_path,
        context_window=context_window,
        max_output_tokens=500,
        project_instructions="",
    )


def test_cjk_estimate_is_not_quartered() -> None:
    assert estimate_text_tokens("你好世界") >= 4
    assert estimate_text_tokens("hello world") >= 2


def test_build_includes_system_and_latest(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    messages = [UserMessage.of("hello")]
    built = builder.build(messages, [])
    assert built.messages[0].role == "system"
    assert built.messages[-1].text_content() == "hello"
    assert built.dropped_messages == 0


def test_old_turns_are_dropped_but_recent_pairs_kept(tmp_path: Path) -> None:
    builder = _builder(tmp_path, context_window=2_200)
    messages = []
    for index in range(20):
        messages.append(UserMessage.of(f"old user {index} " + "x" * 200))
        messages.append(AssistantMessage.of("old answer " + "y" * 200))
    messages.append(UserMessage.of("latest question"))
    messages.append(AssistantMessage.of("latest answer"))
    built = builder.build(messages, [])
    assert built.dropped_messages > 0
    assert built.messages[-2].text_content() == "latest question"
    assert built.messages[-1].text_content() == "latest answer"


def test_tool_pair_is_never_split(tmp_path: Path) -> None:
    builder = _builder(tmp_path, context_window=2_200)
    messages = []
    for index in range(10):
        messages.append(UserMessage.of(f"u{index} " + "a" * 120))
        messages.append(
            AssistantMessage.with_tool_calls(
                None, [ToolCall(id=f"c{index}", name="read_file", arguments={})]
            )
        )
        messages.append(ToolMessage.of(f"c{index}", "r" * 120))
    built = builder.build(messages, [])
    roles = [message.role for message in built.messages]
    assistant_indices = [i for i, role in enumerate(roles) if role == "assistant"]
    for index in assistant_indices:
        assert roles[index + 1] == "tool"


def test_large_tool_result_is_truncated(tmp_path: Path) -> None:
    builder = _builder(tmp_path, context_window=8_000)
    messages = [
        UserMessage.of("read it"),
        AssistantMessage.with_tool_calls(None, [ToolCall(id="c1", name="read_file", arguments={})]),
        ToolMessage.of("c1", "z" * 100_000),
    ]
    built = builder.build(messages, [])
    assert built.truncated
    assert built.messages[-1].role == "tool"
    result = built.messages[-1].tool_result()
    assert result is not None and "truncated" in result.content


def test_huge_user_message_raises(tmp_path: Path) -> None:
    builder = _builder(tmp_path, context_window=1_200)
    with pytest.raises(ContextOverflowError):
        builder.build([UserMessage.of("q" * 50_000)], [])
