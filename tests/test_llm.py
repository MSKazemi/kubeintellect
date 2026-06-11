"""Unit tests for the LLM factory (app/core/llm.py)."""

from unittest.mock import MagicMock, patch


def test_openai_base_url_is_forwarded_when_set(monkeypatch):
    """OPENAI_BASE_URL must be passed to ChatOpenAI so self-hosted / Ollama
    OpenAI-compatible endpoints work."""
    from app.core import config, llm

    monkeypatch.setattr(config.settings, "OPENAI_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setattr(config.settings, "OPENAI_API_KEY", "ollama")

    fake_cls = MagicMock()
    with patch("langchain_openai.ChatOpenAI", fake_cls):
        llm._make_openai("qwen3:30b-a3b")

    _, kwargs = fake_cls.call_args
    assert kwargs["base_url"] == "http://localhost:11434/v1"
    assert kwargs["model"] == "qwen3:30b-a3b"


def test_openai_base_url_is_none_when_empty(monkeypatch):
    """Empty OPENAI_BASE_URL must become None so the real OpenAI default is used."""
    from app.core import config, llm

    monkeypatch.setattr(config.settings, "OPENAI_BASE_URL", "")
    monkeypatch.setattr(config.settings, "OPENAI_API_KEY", "sk-test")

    fake_cls = MagicMock()
    with patch("langchain_openai.ChatOpenAI", fake_cls):
        llm._make_openai("gpt-4o")

    _, kwargs = fake_cls.call_args
    assert kwargs["base_url"] is None
