# tests/test_llm_gateway_streaming.py
"""
Regression test: every public LLM factory must return an instance with
streaming=True so that the workflow's astream() loop produces real-time
token chunks instead of buffering the full response silently.

Bug fixed: 2026-04-03 — all 7 builders in _build_azure/_build_openai/...
were missing streaming=True, causing silent full-response buffering.
"""


import pytest

# conftest.py stubs psycopg2 and the orchestration workflow at import time.
# We additionally need to neutralise LangFuse so callbacks return [].
from app.core.llm_gateway import (
    get_code_gen_llm,
    get_llm_with_params,
    get_supervisor_llm,
    get_worker_llm,
)


@pytest.fixture(autouse=True)
def no_langfuse(monkeypatch):
    """Disable Langfuse so callbacks list is always empty and we get the raw LLM back."""
    monkeypatch.setattr("app.core.llm_gateway._get_langfuse_callbacks", lambda: [])


class TestAzureStreaming:
    """Default provider in tests is azure (set in conftest via LLM_PROVIDER env var)."""

    def test_supervisor_llm_streams(self):
        llm = get_supervisor_llm()
        assert llm.streaming is True, "get_supervisor_llm() must have streaming=True"

    def test_worker_llm_streams(self):
        llm = get_worker_llm()
        assert llm.streaming is True, "get_worker_llm() must have streaming=True"

    def test_code_gen_llm_streams(self):
        llm = get_code_gen_llm()
        assert llm.streaming is True, "get_code_gen_llm() must have streaming=True"

    def test_llm_with_params_streams(self):
        llm = get_llm_with_params(temperature=0.5, max_tokens=32)
        assert llm.streaming is True, "get_llm_with_params() must have streaming=True"
