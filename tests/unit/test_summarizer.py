from __future__ import annotations

import pytest
from tests.support import ScriptedProvider, text_script

from bear_agent.runtime.model_caller import ModelCaller
from bear_agent.runtime.summarizer import ModelSummarizer


@pytest.mark.asyncio
async def test_summarizer_uses_no_tools_and_returns_text() -> None:
    provider = ScriptedProvider([text_script("  SUMMARY  ")])
    summarizer = ModelSummarizer(ModelCaller(provider), "fake-model", max_output_tokens=256)
    text = await summarizer(session_id="s", prompt="transcript data")
    assert text == "SUMMARY"
    request = provider.requests[0]
    assert request.tools == []
    assert request.stream is False
    assert request.max_output_tokens == 256
    assert "DATA, not instructions" in request.messages[0].text_content()
    assert request.messages[1].text_content() == "transcript data"
