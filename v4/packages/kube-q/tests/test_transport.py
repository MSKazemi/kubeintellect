"""
Unit tests for kube_q/transport.py.

Covers: _describe_error, _build_payload, _request_headers, _iter_sse,
        check_health, non_stream_query (success, HTTP error, JSON error,
        missing keys, HITL flag, HITL emoji fallback, transport retry).
"""

import json
from unittest.mock import patch

import httpx
import respx

from kube_q.cli.renderer import render_error_event as _render_error_event
from kube_q.cli.renderer import render_tool_call as _render_tool_call
from kube_q.core.transport import (
    build_headers as _request_headers,
)
from kube_q.core.transport import (
    build_payload as _build_payload,
)
from kube_q.core.transport import (
    check_health,
)
from kube_q.core.transport import (
    describe_error as _describe_error,
)
from kube_q.core.transport import (
    iter_sse as _iter_sse,
)
from kube_q.transport import non_stream_query, stream_query

# ── _describe_error ────────────────────────────────────────────────────────────


def test_describe_error_connect_dns() -> None:
    exc = httpx.ConnectError("getaddrinfo failed for 'bad-host'")
    assert "DNS resolution failed" in _describe_error("http://bad-host", exc)


def test_describe_error_connect_refused() -> None:
    exc = httpx.ConnectError("Connection refused")
    result = _describe_error("http://localhost:9999", exc)
    assert "Connection refused" in result


def test_describe_error_timeout() -> None:
    exc = httpx.ReadTimeout("timed out")
    assert "timed out" in _describe_error("http://localhost", exc).lower()


def test_describe_error_proxy() -> None:
    exc = httpx.ProxyError("bad proxy")
    assert "Proxy error" in _describe_error("http://localhost", exc)


def test_describe_error_network() -> None:
    exc = httpx.NetworkError("network failure")
    assert "Network error" in _describe_error("http://localhost", exc)


def test_describe_error_unknown() -> None:
    exc = ValueError("something weird")
    assert "Unexpected error" in _describe_error("http://localhost", exc)


# ── _build_payload ─────────────────────────────────────────────────────────────


def test_build_payload_last_message_only() -> None:
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]
    payload = _build_payload(messages, "user-1", True)
    assert len(payload["messages"]) == 1
    assert payload["messages"][0]["content"] == "third"


def test_build_payload_single_message() -> None:
    messages = [{"role": "user", "content": "hello"}]
    payload = _build_payload(messages, "user-1", True)
    assert len(payload["messages"]) == 1
    assert payload["stream"] is True
    assert payload["user"] == "user-1"
    assert payload["model"] == "kubeintellect-v2"


def test_build_payload_no_conversation_id() -> None:
    payload = _build_payload([{"role": "user", "content": "hi"}], "user-1", False)
    assert "conversation_id" not in payload
    assert "user_id" not in payload
    assert "action_id" not in payload


def test_build_payload_user_field() -> None:
    payload = _build_payload([{"role": "user", "content": "hi"}], "cli-user-abc", False)
    assert payload["user"] == "cli-user-abc"
    assert payload["stream"] is False


def test_build_payload_stream_options_included_when_streaming() -> None:
    payload = _build_payload([{"role": "user", "content": "hi"}], "u", True)
    assert payload.get("stream_options") == {"include_usage": True}


def test_build_payload_stream_options_absent_when_not_streaming() -> None:
    payload = _build_payload([{"role": "user", "content": "hi"}], "u", False)
    assert "stream_options" not in payload


# ── _request_headers ──────────────────────────────────────────────────────────


def test_request_headers_minimal() -> None:
    h = _request_headers(None, "sess-1", "req-abc")
    assert h["X-Session-ID"] == "sess-1"
    assert h["X-Request-ID"] == "req-abc"
    assert "X-User-ID" not in h
    assert "Authorization" not in h
    assert "Accept" not in h


def test_request_headers_with_api_key() -> None:
    h = _request_headers("secret-key", "sess-1", "req-abc")
    assert h["Authorization"] == "Bearer secret-key"


def test_request_headers_with_accept() -> None:
    h = _request_headers(None, "sess-1", "req-abc", accept="text/event-stream")
    assert h["Accept"] == "text/event-stream"


def test_request_headers_no_user_id() -> None:
    h = _request_headers("key", "sess-1", "req-abc")
    assert "X-User-ID" not in h


# ── _iter_sse ─────────────────────────────────────────────────────────────────


class _FakeResponse:
    """Minimal httpx.Response stand-in that yields fixed text chunks."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    def iter_text(self) -> list[str]:
        return self._chunks


def _sse_chunk(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def test_iter_sse_single_event() -> None:
    event = {"choices": [{"delta": {"content": "hello"}}]}
    resp = _FakeResponse([_sse_chunk(event)])
    events = list(_iter_sse(resp))  # type: ignore[arg-type]
    assert len(events) == 1
    assert events[0]["choices"][0]["delta"]["content"] == "hello"


def test_iter_sse_stops_at_done() -> None:
    event = {"choices": [{"delta": {"content": "hi"}}]}
    resp = _FakeResponse([_sse_chunk(event), "data: [DONE]\n\n"])
    events = list(_iter_sse(resp))  # type: ignore[arg-type]
    assert len(events) == 1  # [DONE] not yielded


def test_iter_sse_skips_malformed_json() -> None:
    bad = "data: not-json\n\n"
    good = _sse_chunk({"choices": []})
    resp = _FakeResponse([bad, good])
    events = list(_iter_sse(resp))  # type: ignore[arg-type]
    assert len(events) == 1  # malformed line skipped, good line yielded


def test_iter_sse_multiple_events() -> None:
    chunks = [_sse_chunk({"id": i}) for i in range(3)]
    resp = _FakeResponse(chunks)
    events = list(_iter_sse(resp))  # type: ignore[arg-type]
    assert [e["id"] for e in events] == [0, 1, 2]


def test_iter_sse_split_across_chunks() -> None:
    # The SSE block arrives in two separate read chunks
    full = _sse_chunk({"choices": [{"delta": {"content": "split"}}]})
    mid = len(full) // 2
    resp = _FakeResponse([full[:mid], full[mid:]])
    events = list(_iter_sse(resp))  # type: ignore[arg-type]
    assert len(events) == 1


# ── check_health ──────────────────────────────────────────────────────────────

BASE = "http://localhost:8000"


@respx.mock
def test_check_health_ok() -> None:
    respx.get(f"{BASE}/healthz").mock(return_value=httpx.Response(200))
    ok, reason = check_health(BASE)
    assert ok is True
    assert reason == ""


@respx.mock
def test_check_health_non_200() -> None:
    respx.get(f"{BASE}/healthz").mock(return_value=httpx.Response(503))
    ok, reason = check_health(BASE)
    assert ok is False
    assert "503" in reason


@respx.mock
def test_check_health_with_api_key() -> None:
    route = respx.get(f"{BASE}/healthz").mock(return_value=httpx.Response(200))
    check_health(BASE, api_key="tok")
    assert route.calls[0].request.headers["Authorization"] == "Bearer tok"


@respx.mock
def test_check_health_connect_error() -> None:
    respx.get(f"{BASE}/healthz").mock(side_effect=httpx.ConnectError("refused"))
    ok, reason = check_health(BASE)
    assert ok is False
    assert reason  # non-empty error string


@respx.mock
def test_check_health_timeout() -> None:
    respx.get(f"{BASE}/healthz").mock(side_effect=httpx.ReadTimeout("timeout"))
    ok, reason = check_health(BASE)
    assert ok is False
    assert "timed out" in reason.lower()


# ── non_stream_query ──────────────────────────────────────────────────────────

_MESSAGES = [{"role": "user", "content": "list pods"}]
_SESSION_ID = "sess-abc"
_USER = "user-xyz"


def _ok_body(text: str, hitl: bool = False, action_id: str | None = None) -> dict:
    choice: dict = {"message": {"content": text}, "finish_reason": "stop"}
    if hitl:
        choice["hitl_required"] = True
        choice["action_id"] = action_id
    return {"choices": [choice]}


@respx.mock
def test_non_stream_query_success() -> None:
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_ok_body("pod list here"))
    )
    text, hitl, action_id, _usage = non_stream_query(BASE, _MESSAGES, _SESSION_ID, _USER)
    assert text == "pod list here"
    assert hitl is False
    assert action_id is None


@respx.mock
def test_non_stream_query_http_error() -> None:
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    text, hitl, action_id, _usage = non_stream_query(BASE, _MESSAGES, _SESSION_ID, _USER)
    assert text == ""
    assert hitl is False


@respx.mock
def test_non_stream_query_invalid_json() -> None:
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=b"not-json")
    )
    text, hitl, _, _usage = non_stream_query(BASE, _MESSAGES, _SESSION_ID, _USER)
    assert text == ""


@respx.mock
def test_non_stream_query_missing_keys() -> None:
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": []})  # empty choices
    )
    text, hitl, _, _usage = non_stream_query(BASE, _MESSAGES, _SESSION_ID, _USER)
    assert text == ""


@respx.mock
def test_non_stream_query_hitl_flag() -> None:
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json=_ok_body("action pending", hitl=True, action_id="act-7")
        )
    )
    text, hitl, action_id, _usage = non_stream_query(BASE, _MESSAGES, _SESSION_ID, _USER)
    assert text == "action pending"
    assert hitl is True
    assert action_id == "act-7"


@respx.mock
def test_non_stream_query_hitl_emoji_fallback() -> None:
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_ok_body("needs approval 🛑"))
    )
    text, hitl, _, _usage = non_stream_query(BASE, _MESSAGES, _SESSION_ID, _USER)
    assert hitl is True


@respx.mock
def test_non_stream_query_retry_then_succeed() -> None:
    """First attempt raises TransportError; second attempt succeeds."""
    respx.post(f"{BASE}/v1/chat/completions").mock(
        side_effect=[
            httpx.ConnectError("refused"),
            httpx.Response(200, json=_ok_body("ok on retry")),
        ]
    )
    with patch("kube_q.transport.time.sleep"):  # don't actually sleep
        text, hitl, _, _usage = non_stream_query(BASE, _MESSAGES, _SESSION_ID, _USER)
    assert text == "ok on retry"


@respx.mock
def test_non_stream_query_all_retries_fail() -> None:
    """All attempts raise TransportError → returns empty string."""
    respx.post(f"{BASE}/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("refused")
    )
    with patch("kube_q.transport.time.sleep"):
        text, hitl, _, _usage = non_stream_query(BASE, _MESSAGES, _SESSION_ID, _USER)
    assert text == ""


@respx.mock
def test_non_stream_query_sends_session_and_request_id() -> None:
    """v2: X-Session-ID and X-Request-ID must be present; X-User-ID must be absent."""
    route = respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_ok_body("ok"))
    )
    non_stream_query(BASE, _MESSAGES, _SESSION_ID, _USER, request_id="req-fixed")
    req_headers = route.calls[0].request.headers
    assert req_headers["X-Session-ID"] == _SESSION_ID
    assert req_headers["X-Request-ID"] == "req-fixed"
    assert "x-user-id" not in req_headers


@respx.mock
def test_non_stream_query_payload_v2() -> None:
    """v2: payload must have 'user', no 'user_id'/'conversation_id', only last message."""
    messages = [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "last"},
    ]
    route = respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_ok_body("ok"))
    )
    non_stream_query(BASE, messages, _SESSION_ID, _USER)
    body = json.loads(route.calls[0].request.content)
    assert body["user"] == _USER
    assert "user_id" not in body
    assert "conversation_id" not in body
    assert len(body["messages"]) == 1
    assert body["messages"][0]["content"] == "last"


# ── request_id auto-generation ────────────────────────────────────────────────


@respx.mock
def test_request_id_auto_generated() -> None:
    """stream_query without request_id must send a 'req-' prefixed X-Request-ID."""
    route = respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=b"data: [DONE]\n\n",
                                    headers={"Content-Type": "text/event-stream"})
    )
    with patch("kube_q.transport.Live"):  # suppress Rich output in tests
        stream_query(BASE, _MESSAGES, _SESSION_ID, _USER)
    req_headers = route.calls[0].request.headers
    assert req_headers["X-Request-ID"].startswith("req-")


# ── usage / token tracking ────────────────────────────────────────────────────


def _sse_body(*events: dict, done: bool = True) -> bytes:
    """Build a raw SSE response body from a list of event dicts."""
    parts = [f"data: {json.dumps(e)}\n\n" for e in events]
    if done:
        parts.append("data: [DONE]\n\n")
    return "".join(parts).encode()


@respx.mock
def test_stream_query_returns_usage_dict_when_present() -> None:
    usage = {"prompt_tokens": 120, "completion_tokens": 340, "total_tokens": 460, "model": "gpt-4o"}
    events = [
        {"choices": [{"delta": {"content": "hello"}, "finish_reason": "stop"}]},
        {"usage": usage},
    ]
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=_sse_body(*events),
                                    headers={"Content-Type": "text/event-stream"})
    )
    with patch("kube_q.transport.Live"):
        text, hitl, action_id, returned_usage = stream_query(BASE, _MESSAGES, _SESSION_ID, _USER)
    assert returned_usage is not None
    assert returned_usage["prompt_tokens"] == 120
    assert returned_usage["completion_tokens"] == 340


@respx.mock
def test_stream_query_returns_none_usage_when_absent() -> None:
    events = [{"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}]
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=_sse_body(*events),
                                    headers={"Content-Type": "text/event-stream"})
    )
    with patch("kube_q.transport.Live"):
        text, hitl, action_id, returned_usage = stream_query(BASE, _MESSAGES, _SESSION_ID, _USER)
    assert returned_usage is None


@respx.mock
def test_non_stream_query_returns_usage_dict_when_present() -> None:
    usage = {
        "prompt_tokens": 50,
        "completion_tokens": 100,
        "total_tokens": 150,
        "model": "gpt-4o-mini",
    }
    body = {
        "choices": [{"message": {"content": "response"}, "finish_reason": "stop"}],
        "usage": usage,
    }
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=body)
    )
    text, hitl, action_id, returned_usage = non_stream_query(BASE, _MESSAGES, _SESSION_ID, _USER)
    assert returned_usage is not None
    assert returned_usage["prompt_tokens"] == 50
    assert returned_usage["total_tokens"] == 150


@respx.mock
def test_non_stream_query_returns_none_usage_when_absent() -> None:
    body = {"choices": [{"message": {"content": "response"}, "finish_reason": "stop"}]}
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=body)
    )
    text, hitl, action_id, returned_usage = non_stream_query(BASE, _MESSAGES, _SESSION_ID, _USER)
    assert returned_usage is None


@respx.mock
def test_stream_query_error_returns_none_usage() -> None:
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(500, text="error")
    )
    with patch("kube_q.transport.Live"):
        text, hitl, action_id, returned_usage = stream_query(BASE, _MESSAGES, _SESSION_ID, _USER)
    assert text == ""
    assert returned_usage is None


@respx.mock
def test_non_stream_query_error_returns_none_usage() -> None:
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(500, text="error")
    )
    text, hitl, action_id, returned_usage = non_stream_query(BASE, _MESSAGES, _SESSION_ID, _USER)
    assert text == ""
    assert returned_usage is None


@respx.mock
def test_stream_query_usage_in_same_event_as_choices() -> None:
    """Usage embedded in the same event as choices should also be captured."""
    usage = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
    events = [
        {
            "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": usage,
        }
    ]
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=_sse_body(*events),
                                    headers={"Content-Type": "text/event-stream"})
    )
    with patch("kube_q.transport.Live"):
        text, hitl, action_id, returned_usage = stream_query(BASE, _MESSAGES, _SESSION_ID, _USER)
    assert returned_usage is not None
    assert returned_usage["total_tokens"] == 30


# ── Side-channel event routing (ki_event wire format) ─────────────────────────
#
# The server wraps side-channel payloads under a "ki_event" key:
#   {"ki_event": {"type": "status", "message": "..."}}
# This leaves the standard OpenAI "choices" / "usage" path unaffected.


@respx.mock
def test_stream_query_ki_status_events_do_not_add_to_text() -> None:
    """ki_event status events are display-only; they must not affect full_text."""
    events = [
        {"ki_event": {"type": "status", "phase": "analyzing", "message": "Analyzing…"}},
        {"ki_event": {"type": "status", "message": "Fetching pods…"}},
        {"choices": [{"delta": {"content": "result"}, "finish_reason": "stop"}]},
    ]
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=_sse_body(*events),
                                    headers={"Content-Type": "text/event-stream"})
    )
    with patch("kube_q.transport.Live"):
        text, hitl, action_id, _ = stream_query(BASE, _MESSAGES, _SESSION_ID, _USER)
    assert text == "result"


@respx.mock
def test_stream_query_ki_tool_call_events_do_not_add_to_text() -> None:
    """ki_event tool_call events are display-only; they must not affect full_text."""
    events = [
        {"ki_event": {"type": "tool_call", "tool": "k8s.get_pods", "message": "Fetching"}},
        {"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]},
    ]
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=_sse_body(*events),
                                    headers={"Content-Type": "text/event-stream"})
    )
    with patch("kube_q.transport.Live"):
        text, hitl, action_id, _ = stream_query(BASE, _MESSAGES, _SESSION_ID, _USER)
    assert text == "done"


@respx.mock
def test_stream_query_ki_error_events_do_not_add_to_text() -> None:
    """ki_event error events are display-only; they must not affect full_text."""
    events = [
        {"ki_event": {"type": "error", "message": "Failed to fetch logs"}},
        {"choices": [{"delta": {"content": "partial"}, "finish_reason": "stop"}]},
    ]
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=_sse_body(*events),
                                    headers={"Content-Type": "text/event-stream"})
    )
    with patch("kube_q.transport.Live"):
        text, hitl, action_id, _ = stream_query(BASE, _MESSAGES, _SESSION_ID, _USER)
    assert text == "partial"


@respx.mock
def test_stream_query_ki_usage_type_captured() -> None:
    """ki_event with type=usage must populate the returned usage dict."""
    events = [
        {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
        {"ki_event": {"type": "usage", "prompt_tokens": 80, "completion_tokens": 120,
                      "total_tokens": 200}},
    ]
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=_sse_body(*events),
                                    headers={"Content-Type": "text/event-stream"})
    )
    with patch("kube_q.transport.Live"):
        text, hitl, action_id, usage = stream_query(BASE, _MESSAGES, _SESSION_ID, _USER)
    assert text == "ok"
    assert usage is not None
    assert usage["prompt_tokens"] == 80
    assert usage["completion_tokens"] == 120


@respx.mock
def test_stream_query_ki_usage_nested_key_captured() -> None:
    """ki_event with a nested 'usage' key must also populate the returned usage dict."""
    usage_data = {"prompt_tokens": 50, "completion_tokens": 75, "total_tokens": 125}
    events = [
        {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]},
        {"ki_event": {"type": "info", "usage": usage_data}},
    ]
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=_sse_body(*events),
                                    headers={"Content-Type": "text/event-stream"})
    )
    with patch("kube_q.transport.Live"):
        text, hitl, action_id, usage = stream_query(BASE, _MESSAGES, _SESSION_ID, _USER)
    assert usage is not None
    assert usage["prompt_tokens"] == 50


@respx.mock
def test_stream_query_mixed_ki_and_openai_events() -> None:
    """Mixed stream: ki_event side-channels before tokens, then standard OpenAI delta."""
    events = [
        {"ki_event": {"type": "status", "message": "Scanning…"}},
        {"ki_event": {"type": "tool_call", "tool": "k8s.get_pods", "message": "Fetching"}},
        {"choices": [{"delta": {"content": "All "}}]},
        {"choices": [{"delta": {"content": "pods "}}]},
        {"choices": [{"delta": {"content": "healthy"}, "finish_reason": "stop"}]},
    ]
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=_sse_body(*events),
                                    headers={"Content-Type": "text/event-stream"})
    )
    with patch("kube_q.transport.Live"):
        text, hitl, action_id, _ = stream_query(BASE, _MESSAGES, _SESSION_ID, _USER)
    assert text == "All pods healthy"
    assert hitl is False


# ── _render_tool_call / _render_error_event unit tests ────────────────────────


def test_render_tool_call_tool_and_message() -> None:
    with patch("kube_q.cli.renderer.console") as mock_console:
        _render_tool_call({"tool": "k8s.get_pods", "message": "Fetching pods"})
    mock_console.print.assert_called_once()
    call_arg = mock_console.print.call_args[0][0]
    assert "k8s.get_pods" in call_arg
    assert "Fetching pods" in call_arg


def test_render_tool_call_tool_only() -> None:
    with patch("kube_q.cli.renderer.console") as mock_console:
        _render_tool_call({"tool": "k8s.describe_node"})
    mock_console.print.assert_called_once()
    assert "k8s.describe_node" in mock_console.print.call_args[0][0]


def test_render_tool_call_message_only() -> None:
    with patch("kube_q.cli.renderer.console") as mock_console:
        _render_tool_call({"message": "doing something"})
    mock_console.print.assert_called_once()
    assert "doing something" in mock_console.print.call_args[0][0]


def test_render_tool_call_empty_is_silent() -> None:
    with patch("kube_q.cli.renderer.console") as mock_console:
        _render_tool_call({})
    mock_console.print.assert_not_called()


def test_render_error_event_prints_message() -> None:
    with patch("kube_q.cli.renderer.console") as mock_console:
        _render_error_event({"message": "something broke"})
    mock_console.print.assert_called_once()
    assert "something broke" in mock_console.print.call_args[0][0]


def test_render_error_event_fallback_to_str() -> None:
    with patch("kube_q.cli.renderer.console") as mock_console:
        _render_error_event({"code": 500})  # no "message" key
    mock_console.print.assert_called_once()
    assert "500" in mock_console.print.call_args[0][0]
