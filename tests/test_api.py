"""
API endpoint tests — /healthz, auth (401/403), SSE format basics.

Uses FastAPI TestClient against a minimal test app (no lifespan, no DB,
no LLM).  The streaming workflow is mocked so tests run without a cluster.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def app():
    """Minimal FastAPI app with health + API routers, no lifespan."""
    @asynccontextmanager
    async def noop_lifespan(a):
        yield

    from app.api.v1.router import api_router
    from app.api.v1.endpoints.health import router as health_router

    test_app = FastAPI(lifespan=noop_lifespan)
    test_app.include_router(health_router)
    test_app.include_router(api_router, prefix="/v1")
    return test_app


@pytest.fixture(scope="module")
def client(app):
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def auth_settings(monkeypatch):
    """Enable auth by monkeypatching settings properties."""
    from app.core import config
    monkeypatch.setattr(config.settings, "KUBEINTELLECT_ADMIN_KEYS",    "ki-test-admin")
    monkeypatch.setattr(config.settings, "KUBEINTELLECT_OPERATOR_KEYS", "ki-test-operator")
    monkeypatch.setattr(config.settings, "KUBEINTELLECT_READONLY_KEYS", "ki-test-readonly")
    # Force property re-evaluation by patching the raw strings
    monkeypatch.setattr(
        type(config.settings), "admin_keys",
        property(lambda s: {"ki-test-admin"}),
    )
    monkeypatch.setattr(
        type(config.settings), "operator_keys",
        property(lambda s: {"ki-test-operator"}),
    )
    monkeypatch.setattr(
        type(config.settings), "readonly_keys",
        property(lambda s: {"ki-test-readonly"}),
    )
    monkeypatch.setattr(
        type(config.settings), "auth_enabled",
        property(lambda s: True),
    )


# ── /healthz ──────────────────────────────────────────────────────────────────


class TestHealthz:
    def test_returns_200(self, client):
        r = client.get("/healthz")
        assert r.status_code == 200

    def test_body_has_status_ok(self, client):
        r = client.get("/healthz")
        assert r.json()["status"] == "ok"

    def test_body_has_version(self, client):
        r = client.get("/healthz")
        assert "version" in r.json()

    def test_method_not_allowed(self, client):
        r = client.post("/healthz")
        assert r.status_code == 405


# ── Authentication ─────────────────────────────────────────────────────────────


class TestAuth:
    _body = {
        "model": "kubeintellect-v2",
        "messages": [{"role": "user", "content": "get all pods"}],
        "stream": True,
    }

    def test_no_key_returns_401_when_auth_enabled(self, client, auth_settings):
        r = client.post("/v1/chat/completions", json=self._body)
        assert r.status_code == 401

    def test_wrong_key_returns_401(self, client, auth_settings):
        r = client.post(
            "/v1/chat/completions",
            json=self._body,
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert r.status_code == 401

    def test_missing_bearer_prefix_returns_401(self, client, auth_settings):
        r = client.post(
            "/v1/chat/completions",
            json=self._body,
            headers={"Authorization": "ki-test-admin"},  # no "Bearer " prefix
        )
        assert r.status_code == 401

    def test_admin_key_accepted(self, client, auth_settings):
        """Admin key should not return 401 (may return other codes due to mocking)."""
        with patch("app.api.v1.endpoints.chat_completions.run_session", new_callable=AsyncMock), \
             patch("app.api.v1.endpoints.chat_completions.prepare_session"), \
             patch("app.api.v1.endpoints.chat_completions.emitter_stream") as mock_stream, \
             patch("app.api.v1.endpoints.chat_completions._audit_log", new_callable=AsyncMock):
            async def _empty():
                return
                yield  # make it an async generator
            mock_stream.return_value = _empty()
            r = client.post(
                "/v1/chat/completions",
                json=self._body,
                headers={"Authorization": "Bearer ki-test-admin"},
            )
        assert r.status_code != 401

    def test_no_user_message_returns_422(self, client, auth_settings):
        r = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "assistant", "content": "hi"}], "stream": True},
            headers={"Authorization": "Bearer ki-test-admin"},
        )
        assert r.status_code == 422

    def test_stream_false_returns_422(self, client, auth_settings):
        r = client.post(
            "/v1/chat/completions",
            json={**self._body, "stream": False},
            headers={"Authorization": "Bearer ki-test-admin"},
        )
        assert r.status_code == 422


# ── SSE format ────────────────────────────────────────────────────────────────


class TestSSEFormat:
    """Verify the stream.start handshake and [DONE] terminator shape."""

    _body = {
        "messages": [{"role": "user", "content": "list pods"}],
        "stream": True,
    }

    def _stream_frames(self, client, extra_headers=None):
        """Collect all data: frames from the SSE stream."""
        headers = extra_headers or {}
        with patch("app.api.v1.endpoints.chat_completions.run_session", new_callable=AsyncMock), \
             patch("app.api.v1.endpoints.chat_completions.prepare_session"), \
             patch("app.api.v1.endpoints.chat_completions._audit_log", new_callable=AsyncMock), \
             patch("app.api.v1.endpoints.chat_completions.emitter_stream") as mock_stream:
            async def _gen():
                # Yield one token then close
                yield {"type": "token", "content": "Hello", "session_id": "x", "ts": 0}
            mock_stream.return_value = _gen()
            r = client.post("/v1/chat/completions", json=self._body, headers=headers)

        frames = []
        for line in r.text.splitlines():
            if line.startswith("data: "):
                frames.append(line[6:])
        return frames

    def test_first_frame_is_stream_start(self, client):
        frames = self._stream_frames(client)
        first = json.loads(frames[0])
        assert first.get("object") == "stream.start"

    def test_first_frame_has_protocol_version(self, client):
        frames = self._stream_frames(client)
        first = json.loads(frames[0])
        assert "protocol_version" in first

    def test_last_frame_is_done(self, client):
        frames = self._stream_frames(client)
        assert frames[-1] == "[DONE]"

    def test_content_type_is_event_stream(self, client):
        with patch("app.api.v1.endpoints.chat_completions.run_session", new_callable=AsyncMock), \
             patch("app.api.v1.endpoints.chat_completions.prepare_session"), \
             patch("app.api.v1.endpoints.chat_completions._audit_log", new_callable=AsyncMock), \
             patch("app.api.v1.endpoints.chat_completions.emitter_stream") as mock_stream:
            async def _gen():
                return
                yield
            mock_stream.return_value = _gen()
            r = client.post("/v1/chat/completions", json=self._body)
        assert "text/event-stream" in r.headers.get("content-type", "")

    def test_token_event_becomes_content_chunk(self, client):
        frames = self._stream_frames(client)
        # Find the frame with "Hello"
        content_frames = [
            json.loads(f) for f in frames
            if f != "[DONE]" and "choices" in json.loads(f)
            and json.loads(f).get("choices")
            and json.loads(f)["choices"][0].get("delta", {}).get("content") == "Hello"
        ]
        assert len(content_frames) == 1

    def test_chunk_has_openai_shape(self, client):
        frames = self._stream_frames(client)
        for raw in frames:
            if raw == "[DONE]":
                continue
            obj = json.loads(raw)
            # stream.start has no "choices" — that's fine
            if "choices" in obj:
                assert "id" in obj
                assert obj["object"] == "chat.completion.chunk"


# ── Serialisation helpers ─────────────────────────────────────────────────────


class TestSerialiseEvent:
    """Unit tests for _serialise_event — no HTTP needed."""

    def _s(self, event: dict) -> str | None:
        from app.api.v1.endpoints.chat_completions import _serialise_event
        return _serialise_event("chatcmpl-test", event)

    def test_token_produces_data_frame(self):
        result = self._s({"type": "token", "content": "hi", "session_id": "x", "ts": 0})
        assert result is not None
        assert result.startswith("data: ")
        payload = json.loads(result[6:])
        assert payload["choices"][0]["delta"]["content"] == "hi"

    def test_status_event_produces_ki_event_frame(self):
        result = self._s({"type": "status", "phase": "analyzing", "message": "Thinking…", "session_id": "x", "ts": 0})
        assert result is not None
        payload = json.loads(result[6:])
        assert payload["ki_event"]["type"] == "status"
        assert payload["choices"] == []

    def test_tool_call_produces_ki_event_frame(self):
        result = self._s({"type": "tool_call", "tool": "run_kubectl", "command": "kubectl get pods", "session_id": "x", "ts": 0})
        payload = json.loads(result[6:])
        assert payload["ki_event"]["type"] == "tool_call"

    def test_tool_result_produces_ki_event_frame(self):
        result = self._s({"type": "tool_result", "tool": "run_kubectl", "output": "pod1 Running", "session_id": "x", "ts": 0})
        payload = json.loads(result[6:])
        assert payload["ki_event"]["type"] == "tool_result"

    def test_final_event_returns_none(self):
        result = self._s({"type": "final", "session_id": "x", "ts": 0})
        assert result is None

    def test_hitl_request_has_hitl_required_field(self):
        result = self._s({
            "type": "hitl_request",
            "risk_level": "high",
            "command": "kubectl delete pod foo",
            "stdin_yaml": None,
            "action_id": "act-123",
            "session_id": "x",
            "ts": 0,
        })
        payload = json.loads(result[6:])
        choice = payload["choices"][0]
        assert choice["hitl_required"] is True
        assert choice["risk_level"] == "high"
