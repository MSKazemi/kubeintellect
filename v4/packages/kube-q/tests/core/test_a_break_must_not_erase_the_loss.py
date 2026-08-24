"""tests/core/test_a_break_must_not_erase_the_loss.py

`stream()` counted every SSE frame it could not parse into an `SseStats`, then called
`_warn_if_lossy(stats)` on the line after the loop. A `break` in the caller closes the generator
at the `yield`, so that trailing line ran on a full drain and on nothing else — and the pattern
the documentation tells people to write is exactly a break:

    async for event in client.stream("why are my pods failing?"):
        match event:
            case TokenEvent(data=d): print(d.content, end="", flush=True)
            case FinalEvent():       break        # <- docs/sdk.md and the class docstring

Measured 2026-08-24 with one unparseable frame ahead of the final event:

    SYNC  drain to end        -> 3 events, 1 warning
    SYNC  break at FinalEvent -> 2 events, 0 warnings
    ASYNC drain to end        -> 3 events, 1 warning
    ASYNC break at FinalEvent -> 2 events, 0 warnings

So the frame the caller never saw left no record anywhere, for the documented usage only.

Second half: `SseStats` says "a caller that must be fail-closed inspects this and refuses" — true
of the CLI, which owns its own `SseStats`, and never true of the SDK, where `stats` was a local
nobody could reach. It is now `client.last_stream_stats`, assigned before the first yield so a
caller that breaks early can still read it.
"""

import asyncio
import logging

import httpx
import pytest

from kube_q.core.client import AsyncKubeQClient, KubeQClient
from kube_q.core.events import FinalEvent

GOOD = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
BAD = b'data: {THIS IS NOT JSON\n\n'
FINAL = b'data: {"ki_event":{"type":"final","data":{}}}\n\n'
MORE = b'data: {"choices":[{"delta":{"content":" more"}}]}\n\n'
DONE = b'data: [DONE]\n\n'

LOSSY = GOOD + BAD + FINAL + MORE + DONE
CLEAN = GOOD + FINAL + MORE + DONE
TRUNCATED = GOOD + FINAL + b'data: {"choices":[{"delta":'  # ends mid-frame, no [DONE]


@pytest.fixture
def warnings(monkeypatch):
    seen: list[str] = []
    handler = type("H", (logging.Handler,), {"emit": lambda s, r: seen.append(r.getMessage())})()
    log = logging.getLogger("kube_q.core.client")
    log.addHandler(handler)
    old = log.level
    log.setLevel(logging.WARNING)
    yield seen
    log.removeHandler(handler)
    log.setLevel(old)


def _serve(body):
    def send(self, request, **kw):
        return httpx.Response(200, content=body, request=request,
                              headers={"content-type": "text/event-stream"})

    async def asend(self, request, **kw):
        return send(self, request, **kw)

    return send, asend


def drain(kind, body, *, stop_at_final, monkeypatch):
    send, asend = _serve(body)
    monkeypatch.setattr(httpx.Client, "send", send)
    monkeypatch.setattr(httpx.AsyncClient, "send", asend)
    client = KubeQClient(url="http://x") if kind == "sync" else AsyncKubeQClient(url="http://x")
    seen = []

    if kind == "sync":
        for ev in client.stream("hi"):
            seen.append(ev)
            if stop_at_final and isinstance(ev, FinalEvent):
                break
    else:
        async def go():
            async for ev in client.stream("hi"):
                seen.append(ev)
                if stop_at_final and isinstance(ev, FinalEvent):
                    break
        asyncio.run(go())
    return client, seen


BOTH = pytest.mark.parametrize("kind", ["sync", "async"])


class TestTheLossIsRecordedEvenWhenTheCallerStopsEarly:
    @BOTH
    def test_breaking_at_the_final_event_still_logs_the_dropped_frame(
        self, kind, warnings, monkeypatch
    ):
        drain(kind, LOSSY, stop_at_final=True, monkeypatch=monkeypatch)
        assert [w for w in warnings if "lossy" in w], (
            "the documented break pattern erased the only record of the dropped frame")

    @BOTH
    def test_the_warning_names_how_many_frames_were_lost(self, kind, warnings, monkeypatch):
        drain(kind, LOSSY, stop_at_final=True, monkeypatch=monkeypatch)
        assert any("1 unparseable frame(s)" in w for w in warnings), warnings

    @BOTH
    def test_a_full_drain_logs_it_too_as_it_always_did(self, kind, warnings, monkeypatch):
        drain(kind, LOSSY, stop_at_final=False, monkeypatch=monkeypatch)
        assert [w for w in warnings if "lossy" in w]

    @BOTH
    def test_a_clean_stream_logs_nothing_either_way(self, kind, warnings, monkeypatch):
        for stop in (True, False):
            drain(kind, CLEAN, stop_at_final=stop, monkeypatch=monkeypatch)
        assert warnings == [], "a lossless stream must stay quiet"

    @BOTH
    def test_the_caller_really_did_stop_early(self, kind, monkeypatch):
        """Guards the fixture: if the break never happened, none of the above tests anything."""
        _c, broke = drain(kind, LOSSY, stop_at_final=True, monkeypatch=monkeypatch)
        _c, full = drain(kind, LOSSY, stop_at_final=False, monkeypatch=monkeypatch)
        assert len(broke) < len(full), (len(broke), len(full))


class TestAFailClosedCallerCanInspectTheStats:
    @BOTH
    def test_the_stats_are_reachable_after_a_break(self, kind, monkeypatch):
        client, _ = drain(kind, LOSSY, stop_at_final=True, monkeypatch=monkeypatch)
        stats = client.last_stream_stats
        assert stats is not None and not stats.lossless
        assert stats.dropped_frames == 1
        assert "THIS IS NOT JSON" in (stats.first_error or "")

    @BOTH
    def test_a_clean_stream_reports_itself_lossless(self, kind, monkeypatch):
        client, _ = drain(kind, CLEAN, stop_at_final=True, monkeypatch=monkeypatch)
        assert client.last_stream_stats is not None
        assert client.last_stream_stats.lossless

    @BOTH
    def test_a_stream_that_ends_mid_frame_says_so(self, kind, monkeypatch):
        client, _ = drain(kind, TRUNCATED, stop_at_final=False, monkeypatch=monkeypatch)
        assert client.last_stream_stats is not None
        assert client.last_stream_stats.truncated_tail

    @BOTH
    def test_a_client_that_never_streamed_reports_none_not_lossless(self, kind):
        client = KubeQClient(url="http://x") if kind == "sync" else AsyncKubeQClient(url="http://x")
        assert client.last_stream_stats is None, (
            "'not yet run' must not be reported as 'ran and lost nothing'")

    @BOTH
    def test_a_second_stream_does_not_inherit_the_first_ones_loss(self, kind, monkeypatch):
        client, _ = drain(kind, LOSSY, stop_at_final=True, monkeypatch=monkeypatch)
        assert not client.last_stream_stats.lossless
        client2, _ = drain(kind, CLEAN, stop_at_final=True, monkeypatch=monkeypatch)
        assert client2.last_stream_stats.lossless


class TestTheDocumentedPatternIsTheOneThatIsTested:
    def test_the_class_docstring_still_shows_a_break(self):
        """If the docs stop breaking, these tests are guarding a pattern nobody uses."""
        import inspect
        assert "break" in (inspect.getdoc(AsyncKubeQClient) or "")

    def test_the_sdk_doc_tells_callers_how_to_be_fail_closed(self):
        from pathlib import Path
        text = (Path(__file__).resolve().parents[2] / "docs" / "sdk.md").read_text()
        # The attribute as a caller would write it — a bare substring check passes for
        # `_last_stream_stats` too, which is a different (private) name.
        assert "client.last_stream_stats" in text
        assert "stats.lossless" in text
        assert "stats.dropped_frames" in text
