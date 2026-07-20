"""
Unit tests for validate_chat_completion_request() in chat_completions.py.

Covers title-detection heuristics and normal message extraction.
No LLM calls — the function is pure validation logic over Pydantic models.
"""
import asyncio
import pytest
from fastapi import HTTPException

# chat_completions.py is safe to import here because conftest.py has already
# stubbed psycopg2 and app.orchestration.workflow before collection begins.
from app.api.v1.endpoints.chat_completions import (
    ChatCompletionRequest,
    ChatMessage,
    validate_chat_completion_request,
)


def _run(coro):
    """Execute an async coroutine synchronously without extra test deps."""
    return asyncio.get_event_loop().run_until_complete(coro)


def _req(**kwargs):
    kwargs.setdefault("messages", [])
    return ChatCompletionRequest(**kwargs)


def _msgs(*pairs):
    return [ChatMessage(role=r, content=c) for r, c in pairs]


# ── Normal message extraction ──────────────────────────────────────────────

def test_returns_user_message_content():
    req = _req(messages=_msgs(("user", "show pods")))
    content, is_title = _run(validate_chat_completion_request(req))
    assert content == "show pods"
    assert is_title is False


def test_returns_last_user_message_when_multiple():
    req = _req(messages=_msgs(
        ("user", "first"),
        ("assistant", "ok"),
        ("user", "list deployments"),
    ))
    content, is_title = _run(validate_chat_completion_request(req))
    assert content == "list deployments"
    assert is_title is False


def test_user_message_content_is_stripped():
    req = _req(messages=_msgs(("user", "  scale pods  ")))
    content, _ = _run(validate_chat_completion_request(req))
    assert content == "scale pods"


def test_system_plus_user_returns_user_content():
    req = _req(messages=_msgs(
        ("system", "You are an assistant."),
        ("user", "get node status"),
    ))
    content, is_title = _run(validate_chat_completion_request(req))
    assert content == "get node status"
    assert is_title is False


# ── Validation errors ──────────────────────────────────────────────────────

def test_empty_messages_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        _run(validate_chat_completion_request(_req(messages=[])))
    assert exc_info.value.status_code == 400


def test_assistant_only_raises_400():
    req = _req(messages=_msgs(("assistant", "Hello!")))
    with pytest.raises(HTTPException) as exc_info:
        _run(validate_chat_completion_request(req))
    assert exc_info.value.status_code == 400


def test_system_only_without_title_keywords_raises_400():
    req = _req(messages=_msgs(("system", "You are a helpful assistant.")))
    with pytest.raises(HTTPException) as exc_info:
        _run(validate_chat_completion_request(req))
    assert exc_info.value.status_code == 400


# ── Title detection: max_tokens heuristic ─────────────────────────────────

def test_max_tokens_10_triggers_title_generation():
    req = _req(
        messages=_msgs(("system", "Generate a title for this conversation.")),
        max_tokens=10,
    )
    _, is_title = _run(validate_chat_completion_request(req))
    assert is_title is True


def test_max_tokens_20_triggers_title_generation():
    req = _req(
        messages=_msgs(("system", "Generate a concise title.")),
        max_tokens=20,
    )
    _, is_title = _run(validate_chat_completion_request(req))
    assert is_title is True


def test_max_tokens_21_with_user_message_not_title():
    req = _req(messages=_msgs(("user", "list pods")), max_tokens=21)
    _, is_title = _run(validate_chat_completion_request(req))
    assert is_title is False


# ── Title detection: system-only + keyword heuristic ──────────────────────

@pytest.mark.parametrize("content", [
    "Generate a title for this conversation",
    "Create a concise title based on the messages above",
    "Write a 5-word title for this chat",
    "Please generate a short title",
])
def test_system_only_with_title_keywords_is_title_gen(content):
    req = _req(messages=_msgs(("system", content)))
    result_content, is_title = _run(validate_chat_completion_request(req))
    assert is_title is True
    assert result_content == content.strip()


def test_title_gen_returns_system_message_content():
    msg = "Generate a concise 5-word title for this conversation"
    req = _req(messages=_msgs(("system", msg)))
    content, is_title = _run(validate_chat_completion_request(req))
    assert is_title is True
    assert content == msg
