"""LLM_PROVIDER=qwen — first-class Alibaba DashScope alias."""
from __future__ import annotations

from app.core.config import Settings


def _settings(**kw) -> Settings:
    # _env_file=None isolates the test from a developer's real v4/.env
    return Settings(_env_file=None, **kw)


class TestQwenProviderAlias:
    def test_qwen_sets_dashscope_defaults(self):
        s = _settings(LLM_PROVIDER="qwen", OPENAI_API_KEY="sk-x")
        assert s.OPENAI_BASE_URL == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
        assert s.OPENAI_COORDINATOR_MODEL == "qwen-max"
        assert s.OPENAI_SUBAGENT_MODEL == "qwen-plus"

    def test_qwen_preserves_explicit_overrides(self):
        s = _settings(
            LLM_PROVIDER="qwen",
            OPENAI_API_KEY="sk-x",
            OPENAI_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1",
            OPENAI_COORDINATOR_MODEL="qwen-plus",
        )
        assert s.OPENAI_BASE_URL.endswith("dashscope.aliyuncs.com/compatible-mode/v1")
        assert s.OPENAI_COORDINATOR_MODEL == "qwen-plus"

    def test_dashscope_key_alias_resolves(self):
        s = _settings(LLM_PROVIDER="qwen", OPENAI_API_KEY="", DASHSCOPE_API_KEY="sk-ds")
        assert s.OPENAI_API_KEY == "sk-ds"

    def test_qwen_key_alias_resolves(self):
        s = _settings(LLM_PROVIDER="qwen", OPENAI_API_KEY="", QWEN_API_KEY="sk-q")
        assert s.OPENAI_API_KEY == "sk-q"

    def test_explicit_openai_key_wins_over_alias(self):
        s = _settings(LLM_PROVIDER="qwen", OPENAI_API_KEY="sk-explicit", DASHSCOPE_API_KEY="sk-ds")
        assert s.OPENAI_API_KEY == "sk-explicit"

    def test_openai_provider_unaffected(self):
        s = _settings(LLM_PROVIDER="openai", OPENAI_API_KEY="sk-x")
        assert s.OPENAI_COORDINATOR_MODEL == "gpt-4o"
        assert s.OPENAI_BASE_URL is None

    def test_invalid_provider_rejected(self):
        import pytest
        with pytest.raises(Exception):
            _settings(LLM_PROVIDER="bogus")
