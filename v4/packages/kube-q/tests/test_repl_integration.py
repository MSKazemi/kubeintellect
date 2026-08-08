"""
Integration tests for the REPL loop — end-to-end flow that drives
``run_repl()`` against a mocked HTTP backend.

Covers:
  • Streaming roundtrip (user → server SSE → rendered → persisted)
  • Non-streaming roundtrip
  • HITL approve cycle
  • HITL deny cycle
  • `/forget` deletes the current session from the local store
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

import kube_q.cli.repl as repl_mod
import kube_q.cli.store as store_mod
from kube_q.cli.repl import ReplConfig, run_repl

BASE = "http://localhost:8000"
SESSION_ID = "sess-repl-1"
USER_ID = "user-repl-1"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect DB + prompt-toolkit history file into a temp dir."""
    monkeypatch.setattr(store_mod, "DB_PATH", tmp_path / "history.db")
    monkeypatch.setattr(repl_mod, "_HISTORY_FILE", str(tmp_path / "history"))


class _FakePromptSession:
    """Stand-in for prompt_toolkit.PromptSession used in tests.

    ``prompt()`` returns the next queued input each call. When the queue is
    empty it raises ``EOFError`` — the REPL treats that as Ctrl-D and exits.
    """

    def __init__(self, inputs: Iterable[str]) -> None:
        self._inputs = list(inputs)

    def prompt(self, *_args, **kwargs) -> str:
        if not self._inputs:
            raise EOFError
        # Honour ``default`` so retry pre-fill works (tests don't exercise it,
        # but matching the real API keeps surprises out).
        return self._inputs.pop(0)


def _run_repl_with_inputs(cfg: ReplConfig, inputs: list[str]) -> None:
    """Run the REPL feeding it ``inputs``, with rendering stubbed out."""
    fake_session = _FakePromptSession(inputs)
    with (
        patch.object(repl_mod, "_make_prompt_session", return_value=fake_session),
        patch("kube_q.transport.Live"),
        patch("kube_q.cli.repl._print_logo"),
    ):
        run_repl(cfg)


def _sse_body(*events: dict, done: bool = True) -> bytes:
    parts = [f"data: {json.dumps(e)}\n\n" for e in events]
    if done:
        parts.append("data: [DONE]\n\n")
    return "".join(parts).encode()


def _base_cfg(stream: bool = True) -> ReplConfig:
    return ReplConfig(
        url=BASE,
        stream=stream,
        user_id=USER_ID,
        initial_conversation_id=SESSION_ID,
        skip_health_check=True,
        show_header=False,
        quiet=True,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


@respx.mock
def test_repl_streaming_roundtrip() -> None:
    """User sends one query; server streams a response; conversation is persisted."""
    events = [
        {"choices": [{"delta": {"content": "hello "}}]},
        {"choices": [{"delta": {"content": "world"}, "finish_reason": "stop"}]},
        {"usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}},
    ]
    route = respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, content=_sse_body(*events),
            headers={"Content-Type": "text/event-stream"},
        )
    )

    _run_repl_with_inputs(_base_cfg(stream=True), ["list pods", "/quit"])

    assert route.called
    # Conversation persisted: user message + assistant reply
    messages = store_mod.load_messages(SESSION_ID)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "list pods"
    assert messages[1]["content"] == "hello world"
    # Tokens logged
    tok = store_mod.get_session_tokens(SESSION_ID)
    assert tok["total_tokens"] == 7


@respx.mock
def test_repl_non_streaming_roundtrip() -> None:
    """Same as above, but exercising the non-streaming code path."""
    body = {
        "choices": [
            {"message": {"content": "non-stream reply"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    }
    route = respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=body)
    )

    _run_repl_with_inputs(_base_cfg(stream=False), ["hello?", "/quit"])

    assert route.called
    messages = store_mod.load_messages(SESSION_ID)
    assert [m["content"] for m in messages] == ["hello?", "non-stream reply"]


@respx.mock
def test_repl_hitl_approve_cycle() -> None:
    """HITL: server asks for approval, user types /approve, server confirms."""
    proposal = _sse_body(
        {"choices": [{"delta": {"content": "Proposed: scale to 5"}}]},
        {"choices": [
            {"delta": {"content": ""},
             "finish_reason": "stop",
             "hitl_required": True,
             "action_id": "act-42"}
        ]},
    )
    confirmation = _sse_body(
        {"choices": [{"delta": {"content": "Scaled to 5."}, "finish_reason": "stop"}]},
    )
    route = respx.post(f"{BASE}/v1/chat/completions").mock(
        side_effect=[
            httpx.Response(200, content=proposal,
                           headers={"Content-Type": "text/event-stream"}),
            httpx.Response(200, content=confirmation,
                           headers={"Content-Type": "text/event-stream"}),
        ]
    )

    _run_repl_with_inputs(
        _base_cfg(stream=True),
        ["scale the deployment to 5", "/approve", "/quit"],
    )

    assert route.call_count == 2
    # Second request body should carry "approve" as the user message
    second_body = json.loads(route.calls[1].request.content)
    assert second_body["messages"][0]["content"] == "approve"

    # All three assistant/user turns persisted
    messages = store_mod.load_messages(SESSION_ID)
    assert [m["role"] for m in messages] == [
        "user", "assistant", "user", "assistant"
    ]
    assert messages[0]["content"] == "scale the deployment to 5"
    assert messages[1]["content"] == "Proposed: scale to 5"
    assert messages[2]["content"] == "approve"
    assert messages[3]["content"] == "Scaled to 5."


@respx.mock
def test_repl_hitl_deny_cycle() -> None:
    """HITL: user types /deny and the proposed action is cancelled server-side."""
    proposal = _sse_body(
        {"choices": [{"delta": {"content": "Proposed: delete pod"}}]},
        {"choices": [
            {"delta": {"content": ""},
             "finish_reason": "stop",
             "hitl_required": True,
             "action_id": "act-99"}
        ]},
    )
    cancelled = _sse_body(
        {"choices": [{"delta": {"content": "Cancelled."}, "finish_reason": "stop"}]},
    )
    route = respx.post(f"{BASE}/v1/chat/completions").mock(
        side_effect=[
            httpx.Response(200, content=proposal,
                           headers={"Content-Type": "text/event-stream"}),
            httpx.Response(200, content=cancelled,
                           headers={"Content-Type": "text/event-stream"}),
        ]
    )

    _run_repl_with_inputs(
        _base_cfg(stream=True),
        ["delete the broken pod", "/deny", "/quit"],
    )

    assert route.call_count == 2
    second_body = json.loads(route.calls[1].request.content)
    assert second_body["messages"][0]["content"] == "deny"

    messages = store_mod.load_messages(SESSION_ID)
    assert messages[-2]["content"] == "deny"
    assert messages[-1]["content"] == "Cancelled."


@respx.mock
def test_repl_forget_deletes_session() -> None:
    """`/forget y` removes the current session from local history."""
    events = [
        {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
    ]
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, content=_sse_body(*events),
            headers={"Content-Type": "text/event-stream"},
        )
    )

    # The user sends one message, runs /forget, confirms with "y", then /quit.
    # After /forget the REPL switches to a brand-new conversation ID, so the
    # original SESSION_ID should have no stored messages.
    _run_repl_with_inputs(
        _base_cfg(stream=True),
        ["hello", "/forget", "y", "/quit"],
    )

    assert store_mod.load_messages(SESSION_ID) == []


@respx.mock
def test_repl_unknown_slash_command_does_not_send_request() -> None:
    """Typos in slash commands must not reach the server."""
    route = respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, content=_sse_body(
                {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}
            ),
            headers={"Content-Type": "text/event-stream"},
        )
    )

    _run_repl_with_inputs(_base_cfg(stream=True), ["/hlep", "/quit"])

    # /hlep was a typo, no request was made.
    assert route.call_count == 0


@respx.mock
def test_repl_plan_event_rendered(capsys: pytest.CaptureFixture) -> None:
    """Server emits a plan ki_event — CLI must render it, not silently drop it."""
    plan_event = {
        "ki_event": {
            "type": "plan",
            "steps": [
                {"description": "Check pod status"},
                {"description": "Inspect logs"},
                {"description": "Apply fix"},
            ],
        },
        "choices": [],
    }
    events = [
        plan_event,
        {"choices": [{"delta": {"content": "Done."}, "finish_reason": "stop"}]},
    ]
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, content=_sse_body(*events),
            headers={"Content-Type": "text/event-stream"},
        )
    )

    _run_repl_with_inputs(_base_cfg(stream=True), ["why is my pod crashing?", "/quit"])

    out = capsys.readouterr().out
    assert "Investigation Plan" in out
    assert "Check pod status" in out
    assert "Inspect logs" in out
    assert "Apply fix" in out


@respx.mock
def test_repl_preserves_pending_message_on_failure() -> None:
    """When all retries fail, the original input is pre-filled for easy resend."""
    respx.post(f"{BASE}/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("refused")
    )

    captured_defaults: list[str] = []

    class _CapturingSession(_FakePromptSession):
        def prompt(self, *_args, **kwargs):  # type: ignore[override]
            captured_defaults.append(kwargs.get("default", ""))
            return super().prompt()

    fake = _CapturingSession(["list pods", "/quit"])
    cfg = replace(_base_cfg(stream=True), startup_retry_timeout=0)
    with (
        patch.object(repl_mod, "_make_prompt_session", return_value=fake),
        patch("kube_q.transport.Live"),
        patch("kube_q.transport.time.sleep"),
        patch("kube_q.cli.repl._print_logo"),
    ):
        run_repl(cfg)

    # Second call to prompt() should have had the failed message pre-filled.
    assert captured_defaults[1] == "list pods"
