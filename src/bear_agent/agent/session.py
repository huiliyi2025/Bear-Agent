"""Session state: the conversation transcript plus counters.

Kept as a pydantic model so it can later be (de)serialized to disk for
save/restore, resumed across ``--resume`` runs, and compressed when the history
grows too long.
"""

from pydantic import BaseModel, Field

from bear_agent.llm.base import Message


class Session(BaseModel):
    messages: list[Message] = Field(default_factory=list)
    step_count: int = 0
    turn_count: int = 0
