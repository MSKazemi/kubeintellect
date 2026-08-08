"""
tests/core/test_events.py — Round-trip tests for every event type in the typed protocol.

Covers: parse_event for all 8 types, legacy ki_event format (no "data" wrapper),
        unknown types return None, and field defaults.
"""


from kube_q.core.events import (
    ErrorEvent,
    FinalEvent,
    HitlRequestEvent,
    StatusEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
    UsageEvent,
    parse_event,
)

# ── StatusEvent ───────────────────────────────────────────────────────────────

def test_status_event_full() -> None:
    raw = {
        "type": "status",
        "event_id": "e1",
        "session_id": "s1",
        "data": {"phase": "routing", "message": "Routing to agent"},
    }
    event = parse_event(raw)
    assert isinstance(event, StatusEvent)
    assert event.data.phase == "routing"
    assert event.data.message == "Routing to agent"
    assert event.event_id == "e1"


def test_status_event_defaults() -> None:
    event = parse_event({"type": "status"})
    assert isinstance(event, StatusEvent)
    assert event.data.phase == ""
    assert event.data.message == ""


# ── TokenEvent ────────────────────────────────────────────────────────────────

def test_token_event() -> None:
    event = parse_event({"type": "token", "data": {"content": "hello world"}})
    assert isinstance(event, TokenEvent)
    assert event.data.content == "hello world"
    assert event.data.role == "assistant"  # default


def test_token_event_with_role() -> None:
    event = parse_event({"type": "token", "data": {"content": "x", "role": "tool"}})
    assert isinstance(event, TokenEvent)
    assert event.data.role == "tool"


# ── ToolCallEvent ─────────────────────────────────────────────────────────────

def test_tool_call_event() -> None:
    raw = {
        "type": "tool_call",
        "data": {
            "tool_name": "k8s.get_pods",
            "args": {"namespace": "default"},
            "call_id": "c1",
            "dry_run": True,
        },
    }
    event = parse_event(raw)
    assert isinstance(event, ToolCallEvent)
    assert event.data.tool_name == "k8s.get_pods"
    assert event.data.args == {"namespace": "default"}
    assert event.data.dry_run is True


def test_tool_call_event_defaults() -> None:
    event = parse_event({"type": "tool_call", "data": {"tool_name": "k8s.list"}})
    assert isinstance(event, ToolCallEvent)
    assert event.data.args == {}
    assert event.data.dry_run is False


# ── ToolResultEvent ───────────────────────────────────────────────────────────

def test_tool_result_event() -> None:
    raw = {
        "type": "tool_result",
        "data": {"call_id": "c1", "ok": False, "summary": "Error: not found", "truncated": True},
    }
    event = parse_event(raw)
    assert isinstance(event, ToolResultEvent)
    assert event.data.ok is False
    assert event.data.truncated is True


# ── HitlRequestEvent ──────────────────────────────────────────────────────────

def test_hitl_request_event() -> None:
    raw = {
        "type": "hitl_request",
        "data": {
            "action": "delete pod",
            "risk": "high",
            "diff": "- pod/api-7f...",
            "approval_id": "appr-123",
        },
    }
    event = parse_event(raw)
    assert isinstance(event, HitlRequestEvent)
    assert event.data.risk == "high"
    assert event.data.approval_id == "appr-123"


# ── UsageEvent ────────────────────────────────────────────────────────────────

def test_usage_event() -> None:
    raw = {
        "type": "usage",
        "data": {"prompt_tokens": 120, "completion_tokens": 340, "total_tokens": 460},
    }
    event = parse_event(raw)
    assert isinstance(event, UsageEvent)
    assert event.data.total_tokens == 460


# ── FinalEvent ────────────────────────────────────────────────────────────────

def test_final_event() -> None:
    raw = {
        "type": "final",
        "data": {"content": "Root cause: OOM", "elapsed_ms": 3200},
    }
    event = parse_event(raw)
    assert isinstance(event, FinalEvent)
    assert event.data.content == "Root cause: OOM"
    assert event.data.elapsed_ms == 3200


def test_final_event_with_usage() -> None:
    raw = {
        "type": "final",
        "data": {
            "content": "done",
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        },
    }
    event = parse_event(raw)
    assert isinstance(event, FinalEvent)
    assert event.data.usage is not None
    assert event.data.usage.total_tokens == 30


# ── ErrorEvent ────────────────────────────────────────────────────────────────

def test_error_event() -> None:
    raw = {
        "type": "error",
        "data": {"code": "TIMEOUT", "message": "upstream timed out", "retryable": True},
    }
    event = parse_event(raw)
    assert isinstance(event, ErrorEvent)
    assert event.data.retryable is True
    assert "timed out" in event.data.message


# ── Legacy ki_event format (no "data" wrapper) ────────────────────────────────

def test_legacy_status_no_data_wrapper() -> None:
    """Backends that emit ki_event with flat fields (no 'data' key) are accepted."""
    raw = {"type": "status", "phase": "thinking", "message": "Analyzing..."}
    event = parse_event(raw)
    assert isinstance(event, StatusEvent)
    assert event.data.message == "Analyzing..."


def test_legacy_tool_call_no_data_wrapper() -> None:
    raw = {"type": "tool_call", "tool_name": "k8s.describe", "call_id": "x"}
    event = parse_event(raw)
    assert isinstance(event, ToolCallEvent)
    assert event.data.tool_name == "k8s.describe"


# ── Unknown / malformed ───────────────────────────────────────────────────────

def test_unknown_type_returns_none() -> None:
    assert parse_event({"type": "totally_unknown"}) is None


def test_empty_dict_returns_none() -> None:
    assert parse_event({}) is None


def test_missing_required_field_token_returns_none() -> None:
    # TokenEvent requires data.content — missing → None
    assert parse_event({"type": "token", "data": {}}) is None


# ── Envelope fields pass through ──────────────────────────────────────────────

def test_envelope_fields() -> None:
    raw = {
        "type": "status",
        "event_id": "ev-abc",
        "session_id": "sess-xyz",
        "conversation_id": "conv-123",
        "timestamp": "2026-04-12T12:00:00Z",
        "data": {},
    }
    event = parse_event(raw)
    assert isinstance(event, StatusEvent)
    assert event.session_id == "sess-xyz"
    assert event.conversation_id == "conv-123"
    assert event.timestamp == "2026-04-12T12:00:00Z"
