"""
Unit tests for HITL session continuity and approval detection.

Tests the pure-Python logic in workflow.py without a real LangGraph graph
or Postgres connection.
"""
import pytest


# ── Approval / denial detection ───────────────────────────────────────────────

class TestApprovalDetection:
    """_is_approval and _is_denial must correctly parse common user responses."""

    def setup_method(self):
        from app.agent.hitl import is_approval, is_denial
        self._is_approval = is_approval
        self._is_denial = is_denial

    @pytest.mark.parametrize("msg", [
        "yes", "Yes", "YES",
        "approve", "Approve",
        "approved",
        "do it", "yes do it",
        "go ahead",
        "confirm",
        "ok", "okay",
        "sure",
        "proceed",
        "run it",
    ])
    def test_approval_phrases(self, msg):
        assert self._is_approval(msg) is True

    @pytest.mark.parametrize("msg", [
        "no", "No", "NO",
        "deny", "denied",
        "cancel", "abort",
        "stop", "nope",
        "don't", "dont",
    ])
    def test_denial_phrases(self, msg):
        assert self._is_denial(msg) is True

    def test_approval_not_denial(self):
        assert self._is_denial("yes") is False

    def test_denial_not_approval(self):
        assert self._is_approval("no") is False

    @pytest.mark.parametrize("msg", [
        "get all pods",
        "create ns mohsen",
        "what is the status of my cluster",
        "",
        "   ",
    ])
    def test_regular_queries_are_neither(self, msg):
        assert self._is_approval(msg) is False
        assert self._is_denial(msg) is False


# ── Chat completions — session ID extraction ──────────────────────────────────

class TestSessionIDHandling:
    """
    The endpoint must use X-Session-ID from the request header as the
    LangGraph thread_id.  A missing header generates a fresh UUID.
    """

    def _make_request(self, headers=None):
        from fastapi.testclient import TestClient
        from unittest.mock import patch, AsyncMock

        # Patch stream_events so no real graph runs
        async def fake_stream(*args, **kwargs):
            return
            yield  # make it an async generator

        with patch("app.api.v1.endpoints.chat_completions.stream_events", fake_stream):
            from app.main import app
            client = TestClient(app)
            resp = client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "get pods"}], "stream": True},
                headers=headers or {},
            )
        return resp

    def test_session_id_from_header_is_used(self):
        """When X-Session-ID is present, stream_events must receive that exact value."""
        from unittest.mock import patch, AsyncMock
        captured = {}

        async def fake_stream(msg, session_id, user_id):
            captured["session_id"] = session_id
            return
            yield

        with patch("app.api.v1.endpoints.chat_completions.stream_events", fake_stream):
            from fastapi.testclient import TestClient
            from app.main import app
            client = TestClient(app)
            client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "test"}], "stream": True},
                headers={"X-Session-ID": "my-fixed-session-123"},
            )
        assert captured.get("session_id") == "my-fixed-session-123"

    def test_missing_session_id_generates_uuid(self):
        """Without X-Session-ID, a fresh UUID must be generated per request."""
        import re
        from unittest.mock import patch
        captured_ids = []

        async def fake_stream(msg, session_id, user_id):
            captured_ids.append(session_id)
            return
            yield

        with patch("app.api.v1.endpoints.chat_completions.stream_events", fake_stream):
            from fastapi.testclient import TestClient
            from app.main import app
            client = TestClient(app)
            for _ in range(2):
                client.post(
                    "/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "test"}], "stream": True},
                )
        assert len(captured_ids) == 2
        # Both must be valid UUIDs
        uuid_re = re.compile(r"^[0-9a-f-]{36}$")
        for sid in captured_ids:
            assert uuid_re.match(sid), f"Not a UUID: {sid!r}"
        # And they must be different (no shared state between requests)
        assert captured_ids[0] != captured_ids[1]
