"""The core Agent Loop — the harness that drives reasoning + tool use.

The loop is intentionally stateless about *what* the model is; it depends on the
LLMClient protocol, the ToolRegistry, and the PermissionPolicy. This keeps the
runtime testable with a scripted fake client and lets new models/backends plug
in without touching the loop.

Flow per step:
1. rebuild context (system prompt + history)
2. call the LLM with the available tool specs
3. if the model returns tool calls -> append assistant message, then for each
   call: ask the policy, execute (or deny), append the tool result; continue.
4. if the model returns plain text (finish) -> append assistant message, return.
5. if step budget is exhausted -> raise MaxStepsExceeded.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bear_agent.agent.context import build_context
from bear_agent.agent.session import Session
from bear_agent.llm.base import LLMClient, Message
from bear_agent.policy.permissions import Decision, PermissionPolicy
from bear_agent.tools.base import ToolResult
from bear_agent.tools.registry import ToolRegistry


class MaxStepsExceeded(RuntimeError):
    pass


EventEmitter = Callable[[str, dict[str, Any]], None]


def _noop_emitter(event: str, payload: dict[str, Any]) -> None:
    return None


class AgentLoop:
    def __init__(
        self,
        *,
        llm: LLMClient,
        registry: ToolRegistry,
        policy: PermissionPolicy,
        max_steps: int = 24,
        emit: EventEmitter = _noop_emitter,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.policy = policy
        self.max_steps = max_steps
        self.emit = emit

    def run(self, session: Session, user_input: str) -> str:
        session.messages.append(Message(role="user", content=user_input))
        session.turn_count += 1

        for step in range(1, self.max_steps + 1):
            session.step_count += 1
            self.emit("step", {"step": step})
            response = self.llm.chat(
                build_context(session), tools=self.registry.specs()
            )

            if not response.tool_calls:
                final_text = response.content or ""
                session.messages.append(Message(role="assistant", content=final_text))
                return final_text

            session.messages.append(
                Message(
                    role="assistant",
                    content=response.content or None,
                    tool_calls=response.tool_calls,
                )
            )
            for call in response.tool_calls:
                result = self._execute_call(call.id, call.name, call.arguments)
                session.messages.append(
                    Message(
                        role="tool",
                        content=result.to_content(),
                        tool_call_id=call.id,
                        name=call.name,
                    )
                )

        raise MaxStepsExceeded(
            f"agent reached the step budget ({self.max_steps}) without a final answer"
        )

    def _execute_call(
        self, call_id: str, name: str, arguments: dict[str, Any]
    ) -> ToolResult:
        tool = self.registry.get(name)
        if tool is None:
            return ToolResult(ok=False, error=f"unknown tool: {name}")

        decision = self.policy.decide(tool, arguments)
        self.emit("decision", {"tool": name, "decision": decision.value})

        if decision is Decision.ALLOW:
            result = tool.call(arguments)
        elif decision is Decision.CONFIRM:
            if self.policy.confirm(tool, arguments):
                result = tool.call(arguments)
            else:
                result = ToolResult(
                    ok=False, error=f"permission denied: user declined {name}"
                )
        else:  # DENY
            result = ToolResult(
                ok=False,
                error=(
                    f"permission denied: blocked by plan mode "
                    f"(plan_mode={self.policy.plan_mode})"
                ),
            )
        self.emit("tool_result", {"tool": name, "ok": result.ok})
        return result
