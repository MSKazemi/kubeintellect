"""
tests/core/test_client_replay.py — Feed canned SSE fixtures into KubeQClient.stream()
and assert the correct typed event sequence is produced.

Uses respx to mock the HTTP layer without hitting a real server.
"""

import json
from unittest.mock import patch

import httpx
import pytest
import respx

from kube_q.core.client import KubeQClient
from kube_q.core.events import (
    FinalEvent,
    StatusEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
    UsageEvent,
)


def _sse(events: list[dict]) -> str:
    """Encode a list of event dicts as an SSE byte stream."""
    lines = []
    for e in events:
        lines.append(f"data: {json.dumps(e)}\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines)


BASE_URL = "http://test-server"


@pytest.fixture
def client() -> KubeQClient:
    return KubeQClient(url=BASE_URL, model="test-model")


# ── Token stream → TokenEvent sequence ───────────────────────────────────────

@respx.mock
def test_stream_yields_token_events(client: KubeQClient) -> None:
    chunks = [
        {"choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": " world"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(200, text=_sse(chunks))
    )

    events = list(client.stream("test query"))
    token_events = [e for e in events if isinstance(e, TokenEvent)]
    assert len(token_events) == 2
    assert token_events[0].data.content == "Hello"
    assert token_events[1].data.content == " world"


# ── ki_event side-channel: status → tool_call → token → final ────────────────

@respx.mock
def test_stream_ki_event_sequence(client: KubeQClient) -> None:
    chunks = [
        {"ki_event": {"type": "status", "data": {"message": "Routing..."}}},
        {"ki_event": {"type": "tool_call", "data": {"tool_name": "k8s.get_pods", "call_id": "c1"}}},
        {"ki_event": {"type": "tool_result", "data": {
            "call_id": "c1", "ok": True, "summary": "3 pods",
        }}},
        {"choices": [{"delta": {"content": "Found 3 pods."}, "finish_reason": None}]},
        {"ki_event": {"type": "final", "data": {"content": "Done", "elapsed_ms": 1200}}},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(200, text=_sse(chunks))
    )

    events = list(client.stream("why are my pods failing?"))
    types = [type(e) for e in events]

    assert StatusEvent in types
    assert ToolCallEvent in types
    assert ToolResultEvent in types
    assert TokenEvent in types
    assert FinalEvent in types


# ── Usage event at end of stream ──────────────────────────────────────────────

@respx.mock
def test_stream_usage_event(client: KubeQClient) -> None:
    chunks = [
        {"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
    ]
    respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(200, text=_sse(chunks))
    )

    events = list(client.stream("query"))
    usage_events = [e for e in events if isinstance(e, UsageEvent)]
    assert len(usage_events) == 1
    assert usage_events[0].data.total_tokens == 15


# ── Empty stream (only [DONE]) produces no events ────────────────────────────

@respx.mock
def test_stream_empty(client: KubeQClient) -> None:
    respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(200, text="data: [DONE]\n\n")
    )
    events = list(client.stream("empty query"))
    assert events == []


# ── HTTP error raises ─────────────────────────────────────────────────────────

@respx.mock
def test_stream_http_error_raises(client: KubeQClient) -> None:
    respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    with pytest.raises(httpx.HTTPStatusError):
        list(client.stream("bad query"))


# ── Namespace prepended when provided ────────────────────────────────────────

@respx.mock
def test_stream_namespace_prepended(client: KubeQClient) -> None:
    captured: list[dict] = []

    def capture(request: httpx.Request, route: respx.Route) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, text="data: [DONE]\n\n")

    respx.post(f"{BASE_URL}/v1/chat/completions").mock(side_effect=capture)
    list(client.stream("list pods", namespace="production"))

    assert len(captured) == 1
    msg_content = captured[0]["messages"][0]["content"]
    assert "namespace=production" in msg_content
    assert "list pods" in msg_content


# ── KubeQClient.health() delegates to core check_health ──────────────────────

@respx.mock
def test_health_ok(client: KubeQClient) -> None:
    respx.get(f"{BASE_URL}/healthz").mock(return_value=httpx.Response(200))
    ok, reason = client.health()
    assert ok is True
    assert reason == ""


@respx.mock
def test_health_401(client: KubeQClient) -> None:
    respx.get(f"{BASE_URL}/healthz").mock(return_value=httpx.Response(401))
    ok, reason = client.health()
    assert ok is False
    assert "Authentication" in reason


# ── stream() retry behaviour ──────────────────────────────────────────────────

@respx.mock
def test_stream_retries_on_transport_error_then_succeeds(client: KubeQClient) -> None:
    """First attempt raises TransportError before any data; second attempt succeeds."""
    chunks = [{"choices": [{"delta": {"content": "hello"}, "finish_reason": "stop"}]}]
    respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        side_effect=[
            httpx.ConnectError("refused"),
            httpx.Response(200, text=_sse(chunks)),
        ]
    )
    with patch("kube_q.core.client.time") as mock_time:
        mock_time.sleep = lambda _: None
        events = list(client.stream("retry query"))

    token_events = [e for e in events if isinstance(e, TokenEvent)]
    assert len(token_events) == 1
    assert token_events[0].data.content == "hello"


@respx.mock
def test_stream_raises_after_all_retries_fail(client: KubeQClient) -> None:
    """All attempts raise TransportError → final attempt re-raises."""
    respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("refused")
    )
    with patch("kube_q.core.client.time") as mock_time:
        mock_time.sleep = lambda _: None
        with pytest.raises(httpx.TransportError):
            list(client.stream("failing query"))


@respx.mock
def test_stream_does_not_retry_after_partial_delivery(client: KubeQClient) -> None:
    """If events were already yielded and then a transport error occurs, re-raise immediately."""
    call_count = 0

    def broken_iter_sse(resp: httpx.Response):  # type: ignore[return]
        nonlocal call_count
        call_count += 1
        yield {"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]}
        raise httpx.RemoteProtocolError("connection dropped")

    respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(200, text="")
    )

    collected: list = []
    with patch("kube_q.core.client.iter_sse", side_effect=broken_iter_sse):
        with pytest.raises(httpx.TransportError):
            for event in client.stream("partial query"):
                collected.append(event)

    # At least the first token was received before the error
    assert any(isinstance(e, TokenEvent) for e in collected)
    # Should not retry — only one request was made
    assert call_count == 1


# ── KubeQClient.query() retry ─────────────────────────────────────────────────

@respx.mock
def test_query_retries_on_transport_error(client: KubeQClient) -> None:
    """Non-streaming query retries on TransportError and returns result on success."""
    respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        side_effect=[
            httpx.ConnectError("refused"),
            httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}),
        ]
    )
    with patch("kube_q.core.client.time") as mock_time:
        mock_time.sleep = lambda _: None
        result = client.query("test")
    assert result["text"] == "ok"


@respx.mock
def test_query_returns_empty_after_all_retries(client: KubeQClient) -> None:
    respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("refused")
    )
    with patch("kube_q.core.client.time") as mock_time:
        mock_time.sleep = lambda _: None
        result = client.query("test")
    assert result["text"] == ""
