"""`LLM_TEMPERATURE` reaches the model client, and defaults to the historical 0.0.

Why this test exists. The temperature was hardcoded as a default argument, and three of the
five Azure deployments this project is evaluated on — gpt-5-mini, gpt-5.5, gpt-5.6-sol —
reject `temperature=0.0` with HTTP 400 ("Only the default (1) value is supported"). The
resulting failure is close to silent: the generation 400s, the agent still streams a reply,
and the run completes, gets graded, and reports zero tokens and zero cost. Nothing in the
response distinguishes it from a healthy run.

So two properties are pinned here:
  * the default is still 0.0, because determinism is what an operator wants and changing it
    would quietly alter every existing deployment's behaviour;
  * a configured value actually reaches the client, because a setting that is read but not
    passed through would leave those three models unusable while looking configurable.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import patch

from app.core import llm
from app.core.config import Settings


def test_default_temperature_is_zero():
    assert Settings().LLM_TEMPERATURE == 0.0


def test_configured_temperature_reaches_the_azure_client():
    captured = {}

    class FakeAzure:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    with patch.object(llm.settings, "LLM_TEMPERATURE", 1.0), \
         patch.object(llm.settings, "AZURE_OPENAI_ENDPOINT", "https://example.invalid/"), \
         patch.dict("sys.modules"):
        fake = types.ModuleType("langchain_openai")
        fake.AzureChatOpenAI = FakeAzure
        sys.modules["langchain_openai"] = fake
        llm._make_azure("gpt-5-mini")

    assert captured["temperature"] == 1.0


def test_explicit_argument_still_wins_over_the_setting():
    captured = {}

    class FakeAzure:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    with patch.object(llm.settings, "LLM_TEMPERATURE", 1.0), \
         patch.object(llm.settings, "AZURE_OPENAI_ENDPOINT", "https://example.invalid/"), \
         patch.dict("sys.modules"):
        fake = types.ModuleType("langchain_openai")
        fake.AzureChatOpenAI = FakeAzure
        sys.modules["langchain_openai"] = fake
        llm._make_azure("gpt-4o", temperature=0.0)

    # 0.0 is falsy; a `temperature or settings.LLM_TEMPERATURE` implementation would
    # discard it here and silently sample at 1.0.
    assert captured["temperature"] == 0.0
