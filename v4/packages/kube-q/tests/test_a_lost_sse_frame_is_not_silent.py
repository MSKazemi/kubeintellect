"""A frame the SSE parser cannot read must leave a trace the caller can find.

Both parsers used to answer an undecodable frame with `pass`. Nothing was raised, nothing
logged, nothing counted — so a truncated answer and a complete one produced byte-identical
observable behaviour, and every command built on SSE inherited that. `kq replay` is
fail-closed against it by hand; the next consumer would not have been.

The fix counts rather than raises, because the two constraints are in tension: a caller that
needs certainty (a verdict frame like `chain_valid`) must be able to detect loss, while the
interactive chat stream — the highest-traffic surface in the product — must still deliver the
part of the answer that did arrive rather than aborting the turn on one bad frame.

What this file pins down:
  * a dropped frame is counted, and the count reaches the caller;
  * so is a stream that ends mid-frame, which the old parser could not even see;
  * *what is yielded* is unchanged, so no existing caller shifts behaviour;
  * the async parser — a separate hand-written copy of the same loop — counts too;
  * the chat path prints a warning and still returns the partial answer.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from kube_q.core.client import _aiter_sse
from kube_q.core.transport import SseStats, iter_sse
from kube_q.transport import stream_query


class _FakeResponse:
    """Minimal httpx.Response stand-in that yields fixed text chunks (sync and async)."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    def iter_text(self) -> Iterator[str]:
        yield from self._chunks

    async def aiter_text(self) -> AsyncIterator[str]:
        for chunk in self._chunks:
            yield chunk


def _frame(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _drain(chunks: list[str], stats: SseStats | None) -> list[dict]:
    return list(iter_sse(_FakeResponse(chunks), stats))  # type: ignore[arg-type]


def _adrain(chunks: list[str], stats: SseStats | None) -> list[dict]:
    async def go() -> list[dict]:
        return [e async for e in _aiter_sse(_FakeResponse(chunks), stats)]  # type: ignore[arg-type]

    return asyncio.run(go())


PARSERS = pytest.mark.parametrize("drain", [_drain, _adrain], ids=["sync", "async"])


class TestTheParserCountsWhatItDrops:
    """The property the old code could not express: 'this stream lost something'."""

    @PARSERS
    def test_a_malformed_frame_is_counted_and_the_rest_still_arrives(self, drain) -> None:
        stats = SseStats()
        events = drain([_frame({"id": 0}), "data: not-json\n\n", _frame({"id": 1})], stats)

        # Unchanged: the good frames are still delivered, the bad one still isn't.
        assert [e["id"] for e in events] == [0, 1]
        # New: and the loss is now a fact the caller can read.
        assert stats.dropped_frames == 1
        assert stats.first_error == "not-json"
        assert stats.lossless is False

    @PARSERS
    def test_a_clean_stream_reports_itself_clean(self, drain) -> None:
        stats = SseStats()
        events = drain([_frame({"id": 0}), _frame({"id": 1}), "data: [DONE]\n\n"], stats)

        assert [e["id"] for e in events] == [0, 1]
        assert stats.dropped_frames == 0
        assert stats.first_error is None
        assert stats.truncated_tail is False
        assert stats.lossless is True

    @PARSERS
    def test_the_first_error_is_kept_not_the_last(self, drain) -> None:
        """A diagnostic that overwrites itself points at the symptom, not the cause."""
        stats = SseStats()
        drain(["data: first-bad\n\n", "data: second-bad\n\n", "data: third-bad\n\n"], stats)

        assert stats.dropped_frames == 3
        assert stats.first_error == "first-bad"

    @PARSERS
    def test_a_long_bad_payload_is_truncated_not_stored_whole(self, drain) -> None:
        """The diagnostic must not become the thing that floods the log."""
        stats = SseStats()
        drain([f"data: {'x' * 5000}\n\n"], stats)

        assert stats.dropped_frames == 1
        assert stats.first_error is not None
        assert len(stats.first_error) == 200

    @PARSERS
    def test_a_stream_that_ends_mid_frame_is_flagged(self, drain) -> None:
        """The old loop could not see this at all: no blank line, so no block, so no frame.

        It is the shape a dropped connection actually takes, and it was the one loss with
        *zero* observable trace — not even a skipped iteration.
        """
        stats = SseStats()
        events = drain([_frame({"id": 0}), 'data: {"id": 1, "content": "half a fra'], stats)

        assert [e["id"] for e in events] == [0]      # unchanged: the partial is not yielded
        assert stats.truncated_tail is True
        assert stats.dropped_frames == 0             # it was never decoded, so never dropped
        assert stats.lossless is False

    @PARSERS
    def test_a_trailing_blank_buffer_is_not_a_truncated_tail(self, drain) -> None:
        """A stream may legitimately end with whitespace; calling that loss is a false alarm."""
        stats = SseStats()
        drain([_frame({"id": 0}), "\n", "   "], stats)

        assert stats.truncated_tail is False
        assert stats.lossless is True

    @PARSERS
    def test_a_trailing_done_sentinel_is_not_a_truncated_tail(self, drain) -> None:
        stats = SseStats()
        drain([_frame({"id": 0}), "data: [DONE]"], stats)

        assert stats.truncated_tail is False
        assert stats.lossless is True

    @PARSERS
    def test_without_stats_the_parser_behaves_exactly_as_before(self, drain) -> None:
        """Every existing call site passes nothing. None of them may change behaviour."""
        chunks = [_frame({"id": 0}), "data: not-json\n\n", _frame({"id": 1}),
                  'data: {"id": 2, "trunc']
        assert [e["id"] for e in drain(chunks, None)] == [0, 1]


# ── the chat path, end to end over a real socket ──────────────────────────────

GOOD_A = {"choices": [{"delta": {"content": "the node is "}, "finish_reason": None}]}
GOOD_B = {"choices": [{"delta": {"content": "NotReady"}, "finish_reason": "stop"}]}


class _SseHandler(BaseHTTPRequestHandler):
    body = b""

    def do_POST(self) -> None:                       # noqa: N802 - BaseHTTPRequestHandler API
        self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a: object) -> None:       # keep pytest output readable
        return


@pytest.fixture
def sse_server():
    """Serve one fixed SSE body over loopback. A fake response object cannot prove the
    chat path survives a bad frame, because the bad frame has to travel the real socket,
    the real chunking and the real parser to get there."""

    def serve(body: str) -> str:
        handler = type("_H", (_SseHandler,), {"body": body.encode()})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    servers: list[ThreadingHTTPServer] = []
    yield serve
    for s in servers:
        s.shutdown()


def _ask(url: str) -> tuple[str, bool, str | None, dict | None]:
    return stream_query(
        url, [{"role": "user", "content": "why is node-1 down?"}],
        "sess-1", "tester", api_key="k", timeout=10.0,
    )


class TestTheChatPathDegradesInsteadOfAborting:
    """`done-when`: the operator can see the loss, *and* still gets the partial answer."""

    def test_a_bad_frame_mid_answer_still_yields_the_rest_and_warns(
        self, sse_server, capsys
    ) -> None:
        url = sse_server(
            _frame(GOOD_A) + "data: {broken\n\n" + _frame(GOOD_B) + "data: [DONE]\n\n"
        )
        text, hitl, action_id, _usage = _ask(url)

        # The turn is not aborted — this is why the fix counts instead of raising.
        assert "the node is " in text
        assert "NotReady" in text
        assert hitl is False and action_id is None
        # And the operator is told the answer may be missing a piece.
        out = capsys.readouterr().out
        assert "may be incomplete" in out
        assert "1 unreadable frame" in out

    def test_a_clean_answer_prints_no_warning(self, sse_server, capsys) -> None:
        """The warning has to be rare, or operators learn to ignore it."""
        url = sse_server(_frame(GOOD_A) + _frame(GOOD_B) + "data: [DONE]\n\n")
        text, _, _, _ = _ask(url)

        assert text == "the node is NotReady"
        assert "may be incomplete" not in capsys.readouterr().out

    def test_a_stream_cut_mid_frame_warns_and_keeps_what_arrived(
        self, sse_server, capsys
    ) -> None:
        url = sse_server(_frame(GOOD_A) + 'data: {"choices": [{"delta": {"cont')
        text, _, _, _ = _ask(url)

        assert text == "the node is "
        out = capsys.readouterr().out
        assert "may be incomplete" in out
        assert "ended mid-frame" in out

    def test_the_plural_reads_correctly_for_a_single_frame(self, sse_server, capsys) -> None:
        url = sse_server(_frame(GOOD_A) + "data: {a\n\n" + "data: {b\n\n" + "data: [DONE]\n\n")
        _ask(url)

        assert "2 unreadable frames" in capsys.readouterr().out
