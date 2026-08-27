"""tests/core/test_both_health_checks_name_the_same_cause.py

There are two health checks — `KubeQClient.health` (via `check_health`) and
`AsyncKubeQClient.health` — and until 2026-08-24 each carried its own copy of the
"what went wrong" classification. They had drifted on every point that matters. Measured:

    DNS      SYNC  -> DNS resolution failed for 'bad-host:8000' — check the hostname or /etc/hosts
    DNS      ASYNC -> Connection refused — nothing is listening at http://bad-host:8000
    timeout  SYNC  -> Connection timed out — … did not respond within 5 s
    timeout  ASYNC -> Connection timed out — … did not respond

The DNS one is the defect that matters: nothing was ever contacted, so "nothing is listening"
names the wrong cause and sends the reader to check the port and the service when the hostname
is what does not resolve. `transport.py` had the right answer, and a test pinning it, the whole
time — for the sync half only. The timeout message had been explicitly corrected on the sync
side to name the duration in force ("this message used to say '5 s' whatever the caller passed")
and the async twin still carried the pre-fix shape.

Third divergence, silent rather than wrong: both docstrings say "Fast connectivity check", but
the async one ran on `self.timeout` — the *query* timeout, 120 s by default, 24× the sync 5 s.

The classification now lives once, in `health_status_reason` / `health_failure_reason`. These
tests assert the *equality* of the two surfaces rather than either message, so a future edit to
one of them cannot re-open the gap.
"""

import asyncio
from unittest.mock import patch

import httpx
import pytest

from kube_q.core.client import AsyncKubeQClient, KubeQClient
from kube_q.core.transport import (
    HEALTH_PATH,
    HEALTH_TIMEOUT,
    health_failure_reason,
    health_status_reason,
)

URL = "http://bad-host:8000"
DNS_ERROR = httpx.ConnectError(
    "[Errno -2] Name or service not known: getaddrinfo failed for 'bad-host'")
REFUSED = httpx.ConnectError("All connection attempts failed")
TIMED_OUT = httpx.ConnectTimeout("timed out")


def _sync_health(outcome):
    def send(self, request, **kw):
        if isinstance(outcome, BaseException):
            raise outcome
        return httpx.Response(outcome, request=request)

    with patch.object(httpx.Client, "send", send):
        return KubeQClient(url=URL).health()


def _async_health(outcome, **client_kw):
    async def send(self, request, **kw):
        if isinstance(outcome, BaseException):
            raise outcome
        return httpx.Response(outcome, request=request)

    async def go():
        with patch.object(httpx.AsyncClient, "send", send):
            return await AsyncKubeQClient(url=URL, **client_kw).health()

    return asyncio.run(go())


OUTCOMES = [
    pytest.param(DNS_ERROR, id="dns"),
    pytest.param(REFUSED, id="refused"),
    pytest.param(TIMED_OUT, id="timeout"),
    pytest.param(200, id="200"),
    pytest.param(401, id="401"),
    pytest.param(503, id="503"),
    pytest.param(500, id="500"),
]


class TestTheTwoHealthChecksCannotDisagree:
    """The property. Every outcome, both surfaces, byte-identical verdict."""

    @pytest.mark.parametrize("outcome", OUTCOMES)
    def test_same_verdict_and_same_words(self, outcome):
        assert _sync_health(outcome) == _async_health(outcome)


class TestTheWrongCauseIsNoLongerNamed:
    def test_a_name_that_does_not_resolve_is_reported_as_dns_on_both(self):
        for ok, reason in (_sync_health(DNS_ERROR), _async_health(DNS_ERROR)):
            assert ok is False
            assert "DNS resolution failed" in reason
            assert "nothing is listening" not in reason, (
                "the host never resolved — nothing was contacted, so no port can be blamed")

    def test_a_real_refusal_still_says_nothing_is_listening(self):
        """The fix must keep the two causes apart, not merge them into one message."""
        for ok, reason in (_sync_health(REFUSED), _async_health(REFUSED)):
            assert ok is False
            assert "nothing is listening" in reason
            assert "DNS" not in reason

    def test_the_timeout_message_names_the_timeout_in_force_on_both(self):
        for _ok, reason in (_sync_health(TIMED_OUT), _async_health(TIMED_OUT)):
            assert f"within {HEALTH_TIMEOUT:g} s" in reason


class TestFastMeansFastOnBothClients:
    """Both docstrings promise a *fast* check; the async one ran on the 120 s query timeout."""

    def _timeout_used(self, make):
        seen = []
        real_init = httpx.AsyncClient.__init__

        def spy(self, *a, **kw):
            seen.append(kw.get("timeout"))
            return real_init(self, *a, **kw)

        async def send(self, request, **kw):
            return httpx.Response(200, request=request)

        async def go():
            with patch.object(httpx.AsyncClient, "__init__", spy), \
                 patch.object(httpx.AsyncClient, "send", send):
                await make().health()

        asyncio.run(go())
        return seen

    def test_the_async_health_check_does_not_use_the_query_timeout(self):
        seen = self._timeout_used(lambda: AsyncKubeQClient(url=URL, timeout=120.0))
        assert seen == [HEALTH_TIMEOUT], seen

    def test_a_custom_query_timeout_does_not_slow_the_health_check(self):
        seen = self._timeout_used(lambda: AsyncKubeQClient(url=URL, timeout=900.0))
        assert 900.0 not in seen, "health() must not inherit the query timeout"

    def test_the_health_timeout_is_actually_fast(self):
        assert HEALTH_TIMEOUT <= 10.0


class TestTheClassifiersAreTheOnlyPlaceThisIsDecided:
    def test_status_classification_is_a_pure_function(self):
        assert health_status_reason(URL, 200, HEALTH_PATH) == (True, "")
        ok, reason = health_status_reason(URL, 401, HEALTH_PATH)
        assert ok is False and "KUBE_Q_API_KEY" in reason
        ok, reason = health_status_reason(URL, 502, HEALTH_PATH)
        assert ok is False and f"HTTP 502 from {URL}{HEALTH_PATH}" == reason

    def test_failure_classification_is_a_pure_function(self):
        assert "DNS resolution failed" in health_failure_reason(URL, DNS_ERROR, 5.0)
        assert "nothing is listening" in health_failure_reason(URL, REFUSED, 5.0)
        assert "within 5 s" in health_failure_reason(URL, TIMED_OUT, 5.0)
        assert "Unexpected error" in health_failure_reason(URL, ValueError("odd"), 5.0)

    def test_an_unknown_failure_is_still_a_verdict_and_never_a_raise(self):
        """`health()` returns (ok, reason); it must not propagate anything."""
        for got in (_sync_health(ValueError("odd")), _async_health(ValueError("odd"))):
            assert got[0] is False
            assert "odd" in got[1]

    def test_neither_health_method_classifies_a_status_code_itself(self):
        """Structural: the copies are gone, not merely agreeing today.

        Parsed with `ast` and read with the docstring dropped — a text scan matched the
        *explanation* of the old wording in the new docstring, which is the failure mode this
        note's own method warns about ("do not trust a text scan").
        """
        import ast
        from pathlib import Path
        tree = ast.parse(
            (Path(__file__).resolve().parents[2] / "kube_q" / "core" / "client.py")
            .read_text(encoding="utf-8"))
        found = [n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == "health"]
        assert len(found) == 2, "expected exactly two health() definitions"
        for fn in found:
            body = list(fn.body)
            if ast.get_docstring(fn) is not None:
                body = body[1:]
            code = "\n".join(ast.unparse(n) for n in body)
            assert "status_code == 200" not in code
            assert "Authentication required" not in code
            assert "nothing is listening" not in code
            assert "DNS" not in code
