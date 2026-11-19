"""Working-context assembly with a token budget.

History is grouped by user turn so an assistant tool-call message is never
separated from its tool results. When the budget is tight the oldest complete
turn is dropped first; only the newest turn may have its tool output truncated.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bear_agent.context.prompt import build_system_prompt
from bear_agent.context.tokens import (
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_tool_specs_tokens,
)
from bear_agent.core.errors import ContextOverflowError
from bear_agent.core.messages import Message, SystemMessage, ToolResultPart
from bear_agent.core.tool_spec import ToolSpec

SAFETY_MARGIN_TOKENS = 1024
MIN_TOOL_RESULT_CHARS = 800


@dataclass(frozen=True)
class BuiltContext:
    messages: list[Message]
    system_tokens: int
    history_tokens: int
    estimated_tokens: int
    dropped_messages: int
    truncated: bool
    budget_tokens: int


class ContextBuilder:
    def __init__(
        self,
        *,
        cwd: str | Path,
        context_window: int,
        max_output_tokens: int,
        system_prompt_extra: str = "",
        project_instructions: str | None = None,
    ) -> None:
        self.cwd = Path(cwd).resolve()
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.system_prompt = build_system_prompt(
            self.cwd,
            extra=system_prompt_extra,
            project_instructions=project_instructions,
        )
        self.system_message = SystemMessage.of(self.system_prompt)

    def build(self, messages: list[Message], tools: list[ToolSpec]) -> BuiltContext:
        system_tokens = estimate_message_tokens(self.system_message)
        tool_tokens = estimate_tool_specs_tokens([tool.model_dump(mode="json") for tool in tools])
        budget = (
            self.context_window
            - self.max_output_tokens
            - SAFETY_MARGIN_TOKENS
            - system_tokens
            - tool_tokens
        )
        if budget <= 0:
            raise ContextOverflowError(
                "the system prompt and tool schemas do not fit in the configured "
                "context window; increase context_window or reduce max_output_tokens"
            )

        groups = _group_by_user_turn(messages)
        if not groups:
            return BuiltContext(
                messages=[self.system_message],
                system_tokens=system_tokens + tool_tokens,
                history_tokens=0,
                estimated_tokens=system_tokens + tool_tokens,
                dropped_messages=0,
                truncated=False,
                budget_tokens=budget,
            )

        selected: list[list[Message]] = []
        used = 0
        truncated = False
        for group in reversed(groups):
            group_tokens = estimate_messages_tokens(group)
            if not selected:
                if group_tokens > budget:
                    group, group_tokens, was_truncated = _shrink_group(group, budget)
                    truncated = truncated or was_truncated
                selected.append(group)
                used += group_tokens
            elif used + group_tokens <= budget:
                selected.append(group)
                used += group_tokens
            else:
                break
        selected.reverse()
        selected_messages = [message for group in selected for message in group]
        dropped = max(0, len(messages) - len(selected_messages))
        total = system_tokens + tool_tokens + used
        if total > self.context_window - self.max_output_tokens:
            raise ContextOverflowError(
                "the latest turn is too large for the context window even after "
                "truncating tool output; shorten the request or raise context_window"
            )
        return BuiltContext(
            messages=[self.system_message, *selected_messages],
            system_tokens=system_tokens + tool_tokens,
            history_tokens=used,
            estimated_tokens=total,
            dropped_messages=dropped,
            truncated=truncated,
            budget_tokens=budget,
        )


def _group_by_user_turn(messages: list[Message]) -> list[list[Message]]:
    groups: list[list[Message]] = []
    current: list[Message] = []
    for message in messages:
        if message.role == "system":
            continue
        if message.role == "user" and current:
            groups.append(current)
            current = []
        current.append(message)
    if current:
        groups.append(current)
    return groups


def _shrink_group(group: list[Message], budget: int) -> tuple[list[Message], int, bool]:
    """Truncate tool results inside the newest turn until it fits.

    The user's own message is never silently truncated: losing the request is
    worse than asking the user to shorten it.
    """
    working = [message.model_copy(deep=True) for message in group]
    truncated = False
    while estimate_messages_tokens(working) > budget:
        candidate: tuple[int, int] | None = None  # (message_index, part_index)
        largest = 0
        for message_index, message in enumerate(working):
            if message.role != "tool":
                continue
            for part_index, part in enumerate(message.parts):
                if isinstance(part, ToolResultPart) and len(part.content) > largest:
                    largest = len(part.content)
                    candidate = (message_index, part_index)
        if candidate is None or largest <= MIN_TOOL_RESULT_CHARS:
            raise ContextOverflowError(
                "the newest turn is too large for the context window; "
                "shorten the request or raise context_window"
            )
        message_index, part_index = candidate
        part = working[message_index].parts[part_index]
        assert isinstance(part, ToolResultPart)
        target = max(MIN_TOOL_RESULT_CHARS, len(part.content) // 2)
        head = target * 2 // 3
        tail = target - head
        shortened = (
            part.content[:head]
            + f"\n... [truncated {len(part.content) - target} chars to fit context] ...\n"
            + part.content[-tail:]
        )
        if len(shortened) >= len(part.content):
            raise ContextOverflowError(
                "the newest turn is too large for the context window; "
                "shorten the request or raise context_window"
            )
        working[message_index].parts[part_index] = part.model_copy(update={"content": shortened})
        truncated = True
    return working, estimate_messages_tokens(working), truncated
