"""Behavioral tests for the Agent Loop, driven by a scripted fake LLM."""

from typing import Any

import pytest

from bear_agent.agent.loop import AgentLoop, MaxStepsExceeded
from bear_agent.agent.session import Session
from bear_agent.llm.base import LLMResponse, ToolCall
from bear_agent.policy.permissions import PermissionPolicy
from bear_agent.tools.base import Tool, ToolResult
from bear_agent.tools.builtin import builtin_tools
from bear_agent.tools.registry import ToolRegistry


def make_registry(tmp_path) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in builtin_tools(tmp_path):
        registry.register(tool)
    return registry


class ScriptedLLM:
    """Fake LLM that returns canned responses in order."""

    def __init__(self, script: list[LLMResponse]) -> None:
        self.script = list(script)

    def chat(
        self,
        messages: list[Any],
        tools: list[Any] | None = None,
        *,
        temperature: float | None = None,
    ) -> LLMResponse:
        return self.script.pop(0)


class StaticToolCallingLLM:
    """Always requests one tool call; used to force a step-limit abort."""

    @staticmethod
    def chat(
        messages: list[Any],
        tools: list[Any] | None = None,
        *,
        temperature: float | None = None,
    ) -> LLMResponse:
        return LLMResponse(
            tool_calls=[
                ToolCall(id="c1", name="read_file", arguments={"path": "data.txt"})
            ],
            finish_reason="tool_calls",
        )


def _content(session_message) -> str:
    assert session_message.content is not None
    return session_message.content


def _read_tool(tool):
    assert tool is not None
    return tool


def test_loop_full_read_then_answer(tmp_path):
    (tmp_path / "data.txt").write_text("hello world\n", encoding="utf-8")
    registry = make_registry(tmp_path)
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="c1", name="read_file", arguments={"path": "data.txt"})
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="The file says: hello world", finish_reason="stop"),
        ]
    )
    session = Session()
    loop = AgentLoop(llm=llm, registry=registry, policy=PermissionPolicy(), max_steps=5)
    answer = loop.run(session, "read data.txt")

    assert answer == "The file says: hello world"
    roles = [m.role for m in session.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    tool_msg = session.messages[2]
    assert tool_msg.tool_call_id == "c1"
    assert "hello world" in _content(tool_msg)


def test_loop_denied_mutation_is_reported_back(tmp_path):
    registry = make_registry(tmp_path)
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="write_file",
                        arguments={"path": "x.txt", "content": "hi"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="I could not write because of plan mode.", finish_reason="stop"
            ),
        ]
    )
    session = Session()
    loop = AgentLoop(
        llm=llm,
        registry=registry,
        policy=PermissionPolicy(plan_mode=True),
        max_steps=5,
    )
    answer = loop.run(session, "write x.txt")

    assert "plan mode" in answer
    assert "permission denied" in _content(session.messages[2])
    assert not (tmp_path / "x.txt").exists()


def test_loop_confirm_approved_then_executes(tmp_path):
    registry = make_registry(tmp_path)

    def do_touch(arguments):
        path = tmp_path / arguments["path"]
        path.write_text("x", encoding="utf-8")
        return ToolResult(ok=True, content=f"created {path}")

    registry.register(
        Tool(
            name="touch",
            description="create a marker file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            fn=do_touch,
            permission="dangerous",
        )
    )
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="c1", name="touch", arguments={"path": "marker.txt"})
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )
    session = Session()
    loop = AgentLoop(
        llm=llm,
        registry=registry,
        policy=PermissionPolicy(confirm_fn=lambda _tool, _args: True),
        max_steps=5,
    )
    assert loop.run(session, "touch a marker") == "done"
    assert (tmp_path / "marker.txt").exists()


def test_loop_confirm_denied_then_recovered(tmp_path):
    registry = make_registry(tmp_path)

    def do_touch(arguments):
        path = tmp_path / arguments["path"]
        path.write_text("x", encoding="utf-8")
        return ToolResult(ok=True, content=f"created {path}")

    invoked = {"count": 0}

    def tracking(arguments):
        invoked["count"] += 1
        return do_touch(arguments)

    registry.register(
        Tool(
            name="touch",
            description="create a marker file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            fn=tracking,
            permission="dangerous",
        )
    )
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="c1", name="touch", arguments={"path": "marker.txt"})
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="Denied by user, cannot create.", finish_reason="stop"),
        ]
    )
    session = Session()
    loop = AgentLoop(
        llm=llm,
        registry=registry,
        policy=PermissionPolicy(confirm_fn=lambda _tool, _args: False),
        max_steps=5,
    )
    answer = loop.run(session, "touch a marker")

    assert answer == "Denied by user, cannot create."
    assert "permission denied: user declined" in _content(session.messages[2])
    assert invoked["count"] == 0
    assert not (tmp_path / "marker.txt").exists()


def test_loop_unknown_tool_is_reported_not_crash(tmp_path):
    registry = make_registry(tmp_path)
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="c1", name="nope", arguments={})],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="That tool does not exist.", finish_reason="stop"),
        ]
    )
    session = Session()
    loop = AgentLoop(llm=llm, registry=registry, policy=PermissionPolicy(), max_steps=5)
    answer = loop.run(session, "use nope")

    assert "not exist" in answer
    assert "unknown tool: nope" in _content(session.messages[2])


def test_loop_failing_tool_does_not_crash(tmp_path):
    registry = make_registry(tmp_path)
    tool = _read_tool(registry.get("read_file"))
    tool.fn = lambda args: ToolResult(ok=False, error="simulated failure")  # type: ignore[method-assign]
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="c1", name="read_file", arguments={"path": "a"})
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="The read failed; I will adapt.", finish_reason="stop"),
        ]
    )
    loop = AgentLoop(llm=llm, registry=registry, policy=PermissionPolicy(), max_steps=5)
    answer = loop.run(Session(), "read")
    assert answer == "The read failed; I will adapt."


def test_loop_max_steps_exceeded(tmp_path):
    registry = make_registry(tmp_path)
    loop = AgentLoop(
        llm=StaticToolCallingLLM(),
        registry=registry,
        policy=PermissionPolicy(),
        max_steps=3,
    )
    with pytest.raises(MaxStepsExceeded):
        loop.run(Session(), "loop forever")


def test_loop_emits_progress_events(tmp_path):
    registry = make_registry(tmp_path)
    (tmp_path / "d.txt").write_text("hi", encoding="utf-8")
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="c1", name="read_file", arguments={"path": "d.txt"})
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )
    events: list[tuple[str, dict[str, Any]]] = []
    loop = AgentLoop(
        llm=llm,
        registry=registry,
        policy=PermissionPolicy(),
        max_steps=5,
        emit=lambda event, payload: events.append((event, payload)),
    )
    loop.run(Session(), "read")
    assert [e for e, _ in events] == ["step", "decision", "tool_result", "step"]
