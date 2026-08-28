"""A16 — the rate limiter's IP fallback was inert behind the Ingress the chart ships.

`rate_limit.py` argues its central design decision this way:

    Keyed by identity, not by IP. Behind an Ingress every request arrives from one address, so
    an IP-keyed limiter would throttle the whole tenancy the moment one client misbehaved.

That reasoning is right, and it only covers requests that carry a bearer token. When one does
not, `caller_key` falls back to `request.client.host` — which behind an Ingress *is* the one
address the paragraph warns about. Measured 2026-08-28 against the real ASGI app: three clients
sent as `X-Forwarded-For: 203.0.113.9 / 198.51.100.4 / 192.0.2.77` all landed in the single
bucket `ip:10.42.0.7`, and 20 requests from the second were answered `404×10, 429×10` purely
because the first had spent the burst. The chart ships `ingress.yaml` at path `/`, and `T212`
records that the production API answers with no credentials at all — so on the deployment as it
actually runs, every anonymous caller on the internet shared one 120/min bucket, and any one of
them could shut out all the others.

The docstring also stated the fallback happens "only when auth is disabled (local development)".
It does not: `caller_key` keys on the presence of a bearer header and never consults the auth
settings. The measurement above ran with `settings.auth_enabled` True.

The fix keeps the safe default. `X-Forwarded-For` is attacker-controlled — honouring it blindly
lets a client mint a fresh bucket per request and, worse, fill the bucket table. So it is trusted
only as far as an operator says it should be: `RATE_LIMIT_TRUSTED_PROXY_HOPS`, default `0`, which
is exactly today's behaviour. Set to the number of proxies in front of the server, the client
address is read that many entries from the right — the end a proxy appends to, and the end an
untrusted client cannot forge.
"""
from __future__ import annotations

import collections

import pytest
from starlette.requests import Request

from app.api.rate_limit import caller_key
from app.core.config import settings


def _request(xff: str | None = None, peer: str = "10.42.0.7", token: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    return Request({
        "type": "http", "method": "GET", "path": "/v1/x",
        "headers": headers, "client": (peer, 1234), "query_string": b"",
    })


class TestTheDefaultIsUnchanged:
    """Hops `0` must behave exactly as the shipped build did — no new trust, no new surface."""

    def test_the_setting_exists_and_defaults_to_trusting_nothing(self):
        assert settings.RATE_LIMIT_TRUSTED_PROXY_HOPS == 0

    def test_a_forwarded_header_is_ignored_by_default(self, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_TRUSTED_PROXY_HOPS", 0)
        assert caller_key(_request(xff="203.0.113.9")) == "ip:10.42.0.7"

    def test_a_bearer_token_still_wins_over_any_address(self, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_TRUSTED_PROXY_HOPS", 2)
        key = caller_key(_request(xff="203.0.113.9, 10.0.0.5", token="alpha"))
        assert key.startswith("k:")

    def test_the_token_is_never_stored_in_the_clear(self, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_TRUSTED_PROXY_HOPS", 0)
        assert "alpha-secret" not in caller_key(_request(token="alpha-secret"))


class TestBehindOneProxy:

    def test_distinct_clients_get_distinct_buckets(self, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_TRUSTED_PROXY_HOPS", 1)
        a = caller_key(_request(xff="203.0.113.9"))
        b = caller_key(_request(xff="198.51.100.4"))
        assert a != b, "one client behind the ingress must not spend another's burst"
        assert a == "ip:203.0.113.9"

    def test_a_client_cannot_forge_a_bucket_by_prepending_entries(self, monkeypatch):
        # The client controls the LEFT of the list; the proxy appends on the RIGHT. Reading from
        # the right is what makes the value trustworthy, so an injected entry changes nothing.
        monkeypatch.setattr(settings, "RATE_LIMIT_TRUSTED_PROXY_HOPS", 1)
        honest = caller_key(_request(xff="203.0.113.9"))
        forged = caller_key(_request(xff="1.2.3.4, 203.0.113.9"))
        assert forged == honest == "ip:203.0.113.9"

    def test_whitespace_around_entries_is_not_a_different_client(self, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_TRUSTED_PROXY_HOPS", 1)
        assert caller_key(_request(xff="  203.0.113.9  ")) == "ip:203.0.113.9"


class TestBehindTwoProxies:

    def test_the_client_is_read_past_every_trusted_hop(self, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_TRUSTED_PROXY_HOPS", 2)
        assert caller_key(_request(xff="203.0.113.9, 10.0.0.5")) == "ip:203.0.113.9"

    def test_a_list_shorter_than_the_trusted_depth_is_refused(self, monkeypatch):
        # Fewer entries than declared hops means the header did not come from the proxy chain the
        # operator described. Trusting it anyway would honour a value a client wrote unaided.
        monkeypatch.setattr(settings, "RATE_LIMIT_TRUSTED_PROXY_HOPS", 2)
        assert caller_key(_request(xff="203.0.113.9")) == "ip:10.42.0.7"

    def test_an_empty_or_malformed_header_falls_back_to_the_peer(self, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_TRUSTED_PROXY_HOPS", 1)
        for bad in ("", "   ", ",", " , "):
            assert caller_key(_request(xff=bad)) == "ip:10.42.0.7", bad


class TestTheDocumentedDesignIsTrue:

    def test_it_does_not_claim_the_fallback_needs_auth_disabled(self):
        import app.api.rate_limit as rl

        doc = rl.__doc__ or ""
        assert "only when auth is disabled" not in doc, (
            "measured false: caller_key never consults the auth settings — it keys on whether a "
            "bearer header is present, and the fallback fired with settings.auth_enabled True"
        )

    def test_the_module_says_what_makes_the_fallback_usable(self):
        import app.api.rate_limit as rl

        assert "RATE_LIMIT_TRUSTED_PROXY_HOPS" in (rl.__doc__ or "")


class TestEndToEndThroughTheRealApp:
    """The measurement that found this, kept as a test."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from app.main import app

        return TestClient(app)

    def test_two_clients_behind_the_ingress_no_longer_shut_each_other_out(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(settings, "RATE_LIMIT_TRUSTED_PROXY_HOPS", 1)
        path = "/v1/no-such-route-exists-here"
        first = collections.Counter()
        second = collections.Counter()
        for _ in range(settings.RATE_LIMIT_BURST + 5):
            first[client.get(path, headers={"X-Forwarded-For": "203.0.113.9"}).status_code] += 1
        for _ in range(10):
            second[client.get(path, headers={"X-Forwarded-For": "198.51.100.4"}).status_code] += 1

        assert first[429] > 0, "the noisy client must still be throttled"
        assert second[429] == 0, "the quiet client must not pay for the noisy one"
