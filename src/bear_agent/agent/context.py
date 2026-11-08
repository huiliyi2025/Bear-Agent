"""Context assembly: system prompt + conversation history.

Every step of the Agent Loop rebuilds the message list from the live ``Session``
state. This is the single injection point where Memory and Skills will later be
retrieved and spliced into the system prompt (Phase 2).
"""

from __future__ import annotations

from bear_agent.agent.session import Session
from bear_agent.llm.base import Message

SYSTEM_PROMPT = """\
You are Bear Agent, a self-evolving harness agent runtime.

Rules:
- Use the provided tools to inspect files, make precise edits, search code, and
  run shell commands when they genuinely help.
- Call tools step by step and always wait for the tool result before continuing.
- When a tool reports an error, read it carefully and adapt your approach.
- Never fabricate tool output; treat every tool result as ground truth for that step.
- Keep internal reasoning out of the final answer.
- Stop and answer the user only when the task is fully complete.
"""


def build_context(session: Session) -> list[Message]:
    return [Message(role="system", content=SYSTEM_PROMPT)] + session.messages
