"""Cheap, provider-neutral token estimation.

We intentionally do not depend on a vendor tokenizer: it would add a large
dependency and still be wrong for local/relay models. The estimate is used for
budgeting, not billing; real usage comes back from the provider.
"""

from __future__ import annotations

import json
import math

from bear_agent.core.messages import Message, TextPart, ToolCallPart, ToolResultPart


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for char in text if ord(char) >= 0x2E80)
    other = len(text) - cjk
    return max(1, cjk + math.ceil(other / 4))


def estimate_message_tokens(message: Message) -> int:
    total = 4  # role / framing overhead
    for part in message.parts:
        if isinstance(part, TextPart):
            total += estimate_text_tokens(part.text)
        elif isinstance(part, ToolCallPart):
            total += estimate_text_tokens(part.tool_call.name)
            total += estimate_text_tokens(json.dumps(part.tool_call.arguments, ensure_ascii=False))
        elif isinstance(part, ToolResultPart):
            total += estimate_text_tokens(part.content)
    return total


def estimate_messages_tokens(messages: list[Message]) -> int:
    return sum(estimate_message_tokens(message) for message in messages)


def estimate_tool_specs_tokens(specs: object) -> int:
    try:
        payload = json.dumps(specs, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        payload = str(specs)
    return estimate_text_tokens(payload)
