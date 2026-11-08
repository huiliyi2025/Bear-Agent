"""Hand-rolled OpenAI-compatible chat client (no official SDK dependency).

Talks to any ``POST {base_url}/chat/completions`` endpoint that implements the
OpenAI Chat Completions protocol with native ``tools``/``tool_calls`` function
calling. DeepSeek / OpenAI / Qwen / Ollama all expose this shape, so a single
client works everywhere by changing ``base_url`` and ``api_key``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from bear_agent.llm.base import (
    LLMClient,
    LLMError,
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
)


def _parse_tool_calls(raw: list[dict[str, Any]] | None) -> list[ToolCall] | None:
    """Convert the API ``tool_calls`` payload into typed ``ToolCall`` objects."""
    if not raw:
        return None
    calls: list[ToolCall] = []
    for item in raw:
        function = item.get("function") or {}
        raw_args = function.get("arguments") or "{}"
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            args = {"_raw_arguments": raw_args}
        calls.append(
            ToolCall(
                id=item.get("id") or "",
                name=function.get("name") or "",
                arguments=args,
            )
        )
    return calls


class OpenAIChatClient(LLMClient):
    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str = "",
        timeout: float = 120.0,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._http = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _build_payload(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        temperature: float | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_openai() for m in messages],
            "temperature": self.temperature if temperature is None else temperature,
        }
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if tools:
            payload["tools"] = [t.to_openai() for t in tools]
            payload["tool_choice"] = "auto"
        return payload

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        temperature: float | None = None,
    ) -> LLMResponse:
        payload = self._build_payload(messages, tools, temperature)
        url = f"{self.base_url}/chat/completions"
        try:
            response = self._http.post(url, headers=self._headers(), json=payload)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            raise LLMError(
                f"HTTP {exc.response.status_code}: {exc.response.text[:500]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Request failed: {exc}") from exc

        try:
            choice = data["choices"][0]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Unexpected response shape: {data}") from exc
        message = choice.get("message") or {}
        return LLMResponse(
            content=message.get("content"),
            tool_calls=_parse_tool_calls(message.get("tool_calls")),
            finish_reason=choice.get("finish_reason") or "",
            usage=data.get("usage"),
        )
