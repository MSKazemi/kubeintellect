# tests/test_hitl_sse_schema.py
"""
P0 regression: breakpoint SSE stream must set hitl_required=True on the final
chunk without requiring a 🛑 emoji in the response text.

Tests cli.stream_query HITL detection using the new hitl_required field and
verifies the emoji fallback still works for old server versions.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

try:
    import cli.transport  # noqa: F401
    _CLI_AVAILABLE = True
except ModuleNotFoundError:
    _CLI_AVAILABLE = False



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sse_chunk(content: str | None = None, finish_reason: str | None = None,
                    hitl_required: bool | None = None,
                    action_id: str | None = None) -> str:
    choice: dict = {"index": 0, "delta": {}, "finish_reason": finish_reason}
    if content is not None:
        choice["delta"]["content"] = content
    if hitl_required is not None:
        choice["hitl_required"] = hitl_required
    if action_id is not None:
        choice["action_id"] = action_id
    chunk = {"choices": [choice]}
    return f"data: {json.dumps(chunk)}\n\n"


def _build_sse_stream(*chunks: str) -> bytes:
    return ("".join(chunks) + "data: [DONE]\n\n").encode()


# ---------------------------------------------------------------------------
# Tests for stream_query HITL detection
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _CLI_AVAILABLE, reason="kube-q CLI not installed (pip install kube-q)")
class TestStreamQueryHitlDetection:
    """stream_query must return hitl_pending=True for breakpoint SSE streams."""

    def _run_stream_query(self, raw_sse: bytes):
        """Invoke cli_transport.stream_query with a mocked httpx response."""
        import cli.transport as cli_transport  # imported here so the test file has no top-level side effects

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_text.return_value = [raw_sse.decode()]

        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__enter__ = MagicMock(return_value=mock_resp)
        mock_stream_ctx.__exit__ = MagicMock(return_value=False)

        mock_client = MagicMock()
        mock_client.stream.return_value = mock_stream_ctx

        mock_client_ctx = MagicMock()
        mock_client_ctx.__enter__ = MagicMock(return_value=mock_client)
        mock_client_ctx.__exit__ = MagicMock(return_value=False)

        with patch("cli.transport.httpx.Client", return_value=mock_client_ctx), \
             patch("cli.transport.Live") as mock_live:
            mock_live_ctx = MagicMock()
            mock_live_ctx.__enter__ = MagicMock(return_value=MagicMock())
            mock_live_ctx.__exit__ = MagicMock(return_value=False)
            mock_live.return_value = mock_live_ctx

            return cli_transport.stream_query(
                url="http://localhost:8000",
                messages=[{"role": "user", "content": "test"}],
                conversation_id="conv-123",
                user_id="test-user",
            )

    def test_hitl_required_field_sets_hitl_pending(self):
        """hitl_required=True on the final chunk triggers hitl_pending — no emoji needed."""
        sse = _build_sse_stream(
            _make_sse_chunk(content="I need to generate code for this task."),
            _make_sse_chunk(finish_reason="stop", hitl_required=True),
        )
        _, hitl_pending, _ = self._run_stream_query(sse)
        assert hitl_pending is True

    def test_hitl_required_false_does_not_set_hitl_pending(self):
        """hitl_required=False on the final chunk leaves hitl_pending=False."""
        sse = _build_sse_stream(
            _make_sse_chunk(content="Here are your pods."),
            _make_sse_chunk(finish_reason="stop", hitl_required=False),
        )
        _, hitl_pending, _ = self._run_stream_query(sse)
        assert hitl_pending is False

    def test_emoji_fallback_when_field_absent(self):
        """Emoji scan fallback fires when hitl_required field is absent (old server)."""
        sse = _build_sse_stream(
            _make_sse_chunk(content="🛑 Approval required before executing this."),
            _make_sse_chunk(finish_reason="stop"),  # no hitl_required field
        )
        _, hitl_pending, _ = self._run_stream_query(sse)
        assert hitl_pending is True

    def test_no_hitl_no_emoji_no_pending(self):
        """Normal response with no hitl_required field and no emoji → hitl_pending=False."""
        sse = _build_sse_stream(
            _make_sse_chunk(content="Here are your running pods."),
            _make_sse_chunk(finish_reason="stop"),
        )
        _, hitl_pending, _ = self._run_stream_query(sse)
        assert hitl_pending is False

    def test_action_id_returned_when_hitl_required(self):
        """action_id from the final chunk is returned as third element of the tuple."""
        sse = _build_sse_stream(
            _make_sse_chunk(content="Need to generate code."),
            _make_sse_chunk(finish_reason="stop", hitl_required=True,
                            action_id="test-action-id-123"),
        )
        _, hitl_pending, action_id = self._run_stream_query(sse)
        assert hitl_pending is True
        assert action_id == "test-action-id-123"


# ---------------------------------------------------------------------------
# Tests for ChatCompletionStreamChoice schema
# ---------------------------------------------------------------------------

class TestChatCompletionStreamChoiceSchema:
    """ChatCompletionStreamChoice must include hitl_required field."""

    def test_hitl_required_defaults_to_false(self):
        from app.api.v1.endpoints.chat_completions import (
            ChatCompletionStreamChoice,
            ChatCompletionStreamDelta,
        )
        choice = ChatCompletionStreamChoice(
            index=0,
            delta=ChatCompletionStreamDelta(),
            finish_reason="stop",
        )
        assert choice.hitl_required is False

    def test_hitl_required_can_be_set_true(self):
        from app.api.v1.endpoints.chat_completions import (
            ChatCompletionStreamChoice,
            ChatCompletionStreamDelta,
        )
        choice = ChatCompletionStreamChoice(
            index=0,
            delta=ChatCompletionStreamDelta(),
            finish_reason="stop",
            hitl_required=True,
        )
        assert choice.hitl_required is True

    def test_hitl_required_serialised_in_json(self):
        from app.api.v1.endpoints.chat_completions import (
            ChatCompletionStreamChoice,
            ChatCompletionStreamDelta,
        )
        choice = ChatCompletionStreamChoice(
            index=0,
            delta=ChatCompletionStreamDelta(),
            finish_reason="stop",
            hitl_required=True,
        )
        data = json.loads(choice.model_dump_json())
        assert data["hitl_required"] is True
