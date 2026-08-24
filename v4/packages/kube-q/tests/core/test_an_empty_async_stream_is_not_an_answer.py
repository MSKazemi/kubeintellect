"""tests/core/test_an_empty_async_stream_is_not_an_answer.py

`AsyncKubeQClient.stream` exhausted its retries and then fell off the end of the loop. A
``return`` from an async generator is a **clean end of stream**, so the caller's ``async for``
completed with zero events and no exception — the exact shape of a server that answered and had
nothing to report. Measured 2026-08-24 against a refused connection:

    SYNC  : raised ConnectError
    ASYNC : returned normally, 0 event(s)

`docs/sdk.md` said of both clients that they retry "before giving up and raising", so the async
half had a published contract it did not keep, and no test and no caller had ever noticed.

These tests pin the *property* — the two clients end the same way — rather than the async fix
alone, so the sync sibling cannot quietly drift into the same silence later. They also keep the
distinction expressible: a healthy server that genuinely sends nothing must still finish quietly.

No pytest-asyncio in this suite; the async generator is driven with ``asyncio.run``.
"""

import asyncio
import json
import re
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from kube_q.core.client import AsyncKubeQClient, KubeQClient
from kube_q.core.transport import QUERY_RETRY_DELAYS

# Same *length* as the real schedule so the attempt count stays honest, but zero wait —
# a test that shortened the list would silently be testing a different retry policy.
NO_DELAYS = tuple(0 for _ in QUERY_RETRY_DELAYS)


def _sse(events: list[dict]) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


def _drain_sync(client, **kw):
    return list(client.stream("hi", **kw))


def _drain_async(client, **kw):
    async def go():
        return [e async for e in client.stream("hi", **kw)]

    return asyncio.run(go())


class _Transport(httpx.BaseTransport, httpx.AsyncBaseTransport):
    """One transport usable by both clients; `responses` is consumed one entry per attempt.

    An entry is either an exception to raise or an (status, body) pair to return.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.attempts = 0

    def _next(self, request):
        self.attempts += 1
        item = self.responses[min(self.attempts - 1, len(self.responses) - 1)]
        if isinstance(item, BaseException):
            raise item
        status, body = item
        return httpx.Response(status, content=body, request=request,
                              headers={"content-type": "text/event-stream"})

    def handle_request(self, request):
        return self._next(request)

    async def handle_async_request(self, request):
        return self._next(request)


def _patched(transport):
    """Point both client factories at `transport` and remove the retry sleeps."""
    return (
        patch.object(httpx.Client, "_transport_for_url", lambda self, url: transport),
        patch.object(httpx.AsyncClient, "_transport_for_url", lambda self, url: transport),
        patch("kube_q.core.client.QUERY_RETRY_DELAYS", NO_DELAYS),
    )


def _run(kind, transport):
    a, b, c = _patched(transport)
    with a, b, c:
        if kind == "sync":
            return _drain_sync(KubeQClient(url="http://server"))
        return _drain_async(AsyncKubeQClient(url="http://server"))


BOTH = pytest.mark.parametrize("kind", ["sync", "async"])


class TestAnUnreachableServerIsNeverAnEmptyAnswer:
    @BOTH
    def test_a_refused_connection_raises_rather_than_ending_the_stream(self, kind):
        t = _Transport([httpx.ConnectError("refused")])
        with pytest.raises(httpx.TransportError):
            _run(kind, t)

    @BOTH
    def test_a_timeout_raises_too(self, kind):
        t = _Transport([httpx.ConnectTimeout("slow")])
        with pytest.raises(httpx.TransportError):
            _run(kind, t)

    def test_the_async_half_was_the_one_that_was_silent(self):
        """The regression, stated as itself: this call used to return `[]`."""
        t = _Transport([httpx.ConnectError("refused")])
        with pytest.raises(httpx.ConnectError):
            _run("async", t)

    @BOTH
    def test_the_raised_error_is_the_last_transport_error_not_a_substitute(self, kind):
        t = _Transport([httpx.ConnectError("nothing is listening on 8000")])
        with pytest.raises(httpx.ConnectError, match="nothing is listening on 8000"):
            _run(kind, t)


class TestTheTwoClientsCannotDisagree:
    """The property, not the patch: whatever one does on failure, the other must do."""

    def test_both_raise_the_same_exception_type_for_the_same_failure(self):
        outcomes = {}
        for kind in ("sync", "async"):
            try:
                _run(kind, _Transport([httpx.ConnectError("refused")]))
                outcomes[kind] = "returned"
            except Exception as exc:  # noqa: BLE001 — the outcome is the assertion
                outcomes[kind] = type(exc).__name__
        assert outcomes["sync"] == outcomes["async"] == "ConnectError", outcomes

    def test_both_raise_on_an_http_error_status(self):
        outcomes = {}
        for kind in ("sync", "async"):
            try:
                _run(kind, _Transport([(500, b"boom")]))
                outcomes[kind] = "returned"
            except Exception as exc:  # noqa: BLE001
                outcomes[kind] = type(exc).__name__
        assert outcomes["sync"] == outcomes["async"] == "HTTPStatusError", outcomes

    def test_both_retry_the_same_number_of_times(self):
        counts = {}
        for kind in ("sync", "async"):
            t = _Transport([httpx.ConnectError("refused")])
            with pytest.raises(httpx.ConnectError):
                _run(kind, t)
            counts[kind] = t.attempts
        assert counts["sync"] == counts["async"] == len(QUERY_RETRY_DELAYS) + 1, counts


class TestSilenceFromAHealthyServerStaysSilent:
    """The fix must not make every quiet stream an error — that would be the same bug mirrored."""

    @BOTH
    def test_a_server_that_sends_no_events_ends_without_raising(self, kind):
        assert _run(kind, _Transport([(200, b"")])) == []

    @BOTH
    def test_a_server_that_sends_events_still_delivers_them(self, kind):
        body = _sse([{"choices": [{"delta": {"content": "ok"}}]}])
        assert len(_run(kind, _Transport([(200, body)]))) >= 1

    @BOTH
    def test_a_recovered_connection_yields_rather_than_raising(self, kind):
        body = _sse([{"choices": [{"delta": {"content": "ok"}}]}])
        t = _Transport([httpx.ConnectError("refused"), (200, body)])
        assert len(_run(kind, t)) >= 1
        assert t.attempts == 2


class TestTheDocumentedContractMatchesTheCode:
    SDK = Path(__file__).resolve().parents[2] / "docs" / "sdk.md"

    def test_the_retry_schedule_in_the_docs_is_the_one_in_the_code(self):
        """The published schedule said `[1s, 3s, 5s]`; the code has always said (2, 5, 10)."""
        text = self.SDK.read_text()
        assert re.search(r"[`,\s]*".join(f"{d}s" for d in QUERY_RETRY_DELAYS), text), (
            f"docs/sdk.md must name the real schedule {QUERY_RETRY_DELAYS}")

    def test_the_docs_do_not_still_claim_the_old_schedule(self):
        assert "[1s, 3s, 5s]" not in self.SDK.read_text()

    def test_the_docs_say_stream_raises_and_query_does_not(self):
        text = self.SDK.read_text()
        assert "raises the last `httpx.TransportError`" in text
        assert '`query()` | returns `{"text": "", …}`' in text
