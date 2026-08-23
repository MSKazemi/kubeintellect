"""A stream that dies mid-turn must say so in a form a script can read.

`_stream()`'s failure path ends the SSE stream with exactly the frames a successful answer
ends with — a `finish_reason: "stop"` chunk followed by `[DONE]` — and puts the reason in
`content` as `[Error: …]`. Prose is not a signal. Measured on 2026-08-19 (pass 49) against the
real client: `kq -q` scored that text as an answer and exited **0**, so a CI job could not tell
a server crash from a real result.

The fix is a `ki_event` error frame carrying `fatal: true`. `fatal` matters as much as the
frame: error events are also emitted mid-turn when one tool fails and the agent recovers and
answers anyway, and those must keep counting as answers. The client half of this contract is
pinned in `packages/kube-q/tests/test_transport.py`.
"""

from __future__ import annotations

import json

import pytest

from app.api.v1.endpoints import chat_completions


def _frames(raw: list[str]) -> list[dict]:
    """Parse the `data:` payloads out of an SSE stream, ignoring `[DONE]` and comments."""
    out = []
    for chunk in raw:
        for line in chunk.splitlines():
            if line.startswith("data: ") and line[6:].strip() != "[DONE]":
                out.append(json.loads(line[6:]))
    return out


async def _collect(monkeypatch) -> list[dict]:
    def _boom(*_a, **_k):
        raise RuntimeError("connection to Postgres lost")

    monkeypatch.setattr(chat_completions, "emitter_stream", _boom)
    monkeypatch.setattr(chat_completions, "prepare_session", lambda *_a, **_k: None)

    async def _noop_session(*_a, **_k):
        return None

    monkeypatch.setattr(chat_completions, "run_session", _noop_session)
    return _frames([c async for c in chat_completions._stream("q", "sess", "u", "admin")])


@pytest.mark.asyncio
async def test_a_crashed_stream_emits_a_fatal_error_event(monkeypatch):
    frames = await _collect(monkeypatch)
    fatal = [f["ki_event"] for f in frames
             if f.get("ki_event", {}).get("type") == "error" and f["ki_event"].get("fatal")]
    assert fatal, (
        "The stream crashed but emitted no fatal error event. The terminal frames are "
        "byte-identical to a successful answer's, so the only remaining evidence of failure is "
        "prose inside `content` — which `kq -q` scores as an answer and exits 0 on."
    )
    assert "connection to Postgres lost" in fatal[0]["message"]


@pytest.mark.asyncio
async def test_the_reason_still_reaches_openai_compatible_clients(monkeypatch):
    """Third-party clients ignore the side channel; they must not get a silent empty answer."""
    frames = await _collect(monkeypatch)
    content = "".join(
        c.get("delta", {}).get("content", "")
        for f in frames for c in f.get("choices", []) or []
    )
    assert "connection to Postgres lost" in content


@pytest.mark.asyncio
async def test_the_stream_still_terminates_cleanly(monkeypatch):
    """A crash must not also produce a malformed stream — clients hang on a missing [DONE]."""
    raw = []
    def _boom(*_a, **_k):
        raise RuntimeError("connection to Postgres lost")
    monkeypatch.setattr(chat_completions, "emitter_stream", _boom)
    monkeypatch.setattr(chat_completions, "prepare_session", lambda *_a, **_k: None)
    async def _noop_session(*_a, **_k):
        return None
    monkeypatch.setattr(chat_completions, "run_session", _noop_session)
    raw = [c async for c in chat_completions._stream("q", "sess", "u", "admin")]
    assert raw[-1].strip().endswith("[DONE]")
