from unittest.mock import patch

import pytest

from app.core import llm


@pytest.fixture(autouse=True)
def clear_llm_caches():
    llm._coordinator_llm.cache_clear()
    llm._subagent_llm.cache_clear()
    yield
    llm._coordinator_llm.cache_clear()
    llm._subagent_llm.cache_clear()


def test_anthropic_v2_coordinator_fails_closed():
    with (
        patch.object(llm.settings, "LLM_PROVIDER", "anthropic"),
        patch.object(llm.settings, "CORTEX_V4_ENABLED", False),
        patch.object(llm, "_make_openai") as make_openai,
    ):
        with pytest.raises(RuntimeError, match="CORTEX_V4_ENABLED"):
            llm.get_coordinator_llm()

        make_openai.assert_not_called()


def test_anthropic_v2_subagent_fails_closed():
    with (
        patch.object(llm.settings, "LLM_PROVIDER", "anthropic"),
        patch.object(llm.settings, "CORTEX_V4_ENABLED", False),
        patch.object(llm, "_make_openai") as make_openai,
    ):
        with pytest.raises(RuntimeError, match="CORTEX_V4_ENABLED"):
            llm.get_subagent_llm()

        make_openai.assert_not_called()
