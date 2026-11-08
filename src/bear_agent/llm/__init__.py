"""LLM layer: provider-independent message types + a hand-rolled HTTP client."""

from bear_agent.llm.base import (
    FinishReason,
    LLMClient,
    LLMError,
    LLMResponse,
    Message,
    Role,
    ToolCall,
    ToolSpec,
)
from bear_agent.llm.openai_compat import OpenAIChatClient

__all__ = [
    "FinishReason",
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "Message",
    "OpenAIChatClient",
    "Role",
    "ToolCall",
    "ToolSpec",
]
