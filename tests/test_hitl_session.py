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

    In v3 the endpoint launches the workflow via run_session(user_message,
    session_id, user_id, user_role, ...) as a background task and streams the
    queue via emitter_stream(session_id). We capture the session_id from the
    run_session call (recorded synchronously when the coroutine is created).
    """

    @staticmethod
    def _post(headers=None, count=1):
        from unittest.mock import patch, AsyncMock
        from fastapi.testclient import TestClient

        # AsyncMock records call args synchronously when the coroutine is created,
        # so we don't depend on the background task actually running before it is
        # cancelled. session_id is the 2nd positional arg of run_session(...).
        mock_run = AsyncMock()

        async def empty_stream(*args, **kwargs):
            return
            yield  # make it an async generator

        with patch("app.api.v1.endpoints.chat_completions.run_session", mock_run), \
             patch("app.api.v1.endpoints.chat_completions.emitter_stream", side_effect=empty_stream), \
             patch("app.api.v1.endpoints.chat_completions.prepare_session"), \
             patch("app.api.v1.endpoints.chat_completions._audit_log", new_callable=AsyncMock):
            from app.main import app
            client = TestClient(app)
            for _ in range(count):
                client.post(
                    "/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "test"}], "stream": True},
                    headers=headers or {},
                )
        return [call.args[1] for call in mock_run.call_args_list]

    def test_session_id_from_header_is_used(self):
        """When X-Session-ID is present, run_session must receive that exact value."""
        captured_ids = self._post(headers={"X-Session-ID": "my-fixed-session-123"})
        assert captured_ids == ["my-fixed-session-123"]

    def test_missing_session_id_generates_uuid(self):
        """Without X-Session-ID, a fresh UUID must be generated per request."""
        import re

        captured_ids = self._post(count=2)
        assert len(captured_ids) == 2
        # Both must be valid UUIDs
        uuid_re = re.compile(r"^[0-9a-f-]{36}$")
        for sid in captured_ids:
            assert uuid_re.match(sid), f"Not a UUID: {sid!r}"
        # And they must be different (no shared state between requests)
        assert captured_ids[0] != captured_ids[1]
