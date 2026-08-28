"""A16 — there is a rate limit, and the two ways a limiter makes things worse are tested.

Before 2026-08-28 no route had one: a retry loop with a valid readonly key could issue unbounded
`POST /v1/chat/completions`, and every one of those is an LLM call against the operator's spend
and a kubectl fan-out against their API server.

The claims that matter are not "it returns 429". They are:

* **It never limits a probe.** A 429 to the kubelet's liveness probe restarts the pod under
  exactly the load the limiter exists to survive — strictly worse than no limiter.
* **It is keyed by identity, not by address**, because behind an Ingress every request shares one
  peer address and an IP-keyed limiter throttles the whole tenancy when one client misbehaves.
  And the credential itself is never retained.
* **A 429 is still a CORS response and still appears in the access log.** Mounted outside CORS,
  the browser client sees an opaque network error; mounted outside logging, the operator cannot
  see the limiter firing at all.
* **The bucket table is bounded**, because the middleware runs before route authentication, so
  arbitrary bearer tokens can create buckets.
"""
from __future__ import annotations

import inspect

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request

from app.api import rate_limit
from app.api.rate_limit import EXEMPT_PATHS, RateLimitMiddleware, caller_key
from app.core.config import settings


def _request(headers: dict[str, str] | None = None, host: str = "10.0.0.1") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/v1/chat/completions",
                    "headers": raw, "client": (host, 5555), "scheme": "http",
                    "server": ("test", 80), "query_string": b""})


@pytest.fixture
def limiter():
    return RateLimitMiddleware(app=None)


@pytest.fixture
def tight(monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "RATE_LIMIT_PER_MIN", 60)   # 1 token/second
    monkeypatch.setattr(settings, "RATE_LIMIT_BURST", 3)


class TestItActuallySaysNo:
    def test_the_burst_is_admitted_then_the_next_is_refused(self, limiter, tight):
        assert [limiter.allow("k:a", 0.0)[0] for _ in range(4)] == [True, True, True, False]

    def test_a_refusal_carries_a_retry_after_that_would_actually_work(self, limiter, tight):
        for _ in range(3):
            limiter.allow("k:a", 0.0)
        allowed, retry_after = limiter.allow("k:a", 0.0)
        assert not allowed
        assert retry_after >= 1.0
        # Waiting exactly that long must succeed, or the header is advice that hot-loops.
        assert limiter.allow("k:a", retry_after)[0] is True

    def test_the_bucket_refills_over_time(self, limiter, tight):
        for _ in range(3):
            limiter.allow("k:a", 0.0)
        assert limiter.allow("k:a", 0.5)[0] is False   # 0.5 tokens — not a whole one
        assert limiter.allow("k:a", 1.0)[0] is True

    def test_it_never_refills_past_the_burst(self, limiter, tight):
        limiter.allow("k:a", 0.0)
        assert [limiter.allow("k:a", 10_000.0)[0] for _ in range(4)] == [True, True, True, False]

    def test_one_caller_cannot_spend_anothers_allowance(self, limiter, tight):
        for _ in range(4):
            limiter.allow("k:a", 0.0)
        assert limiter.allow("k:b", 0.0)[0] is True

    def test_off_means_off(self, limiter, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
        app = FastAPI()
        app.add_middleware(RateLimitMiddleware)

        @app.get("/v1/thing")
        def thing():
            return {"ok": True}

        with TestClient(app) as c:
            assert all(c.get("/v1/thing").status_code == 200 for _ in range(50))

    def test_it_is_on_by_default(self):
        """A limiter that ships off is not a limiter."""
        assert settings.RATE_LIMIT_ENABLED is True
        assert settings.RATE_LIMIT_PER_MIN > 0


class TestItNeverLimitsAProbe:
    def test_the_probe_and_scrape_paths_are_all_exempt(self):
        for path in ("/healthz", "/readyz", "/v1/healthz", "/v1/readyz", "/metrics"):
            assert path in EXEMPT_PATHS

    def test_a_flood_of_probes_is_never_refused(self, tight):
        app = FastAPI()
        app.add_middleware(RateLimitMiddleware)

        @app.get("/healthz")
        def healthz():
            return {"status": "ok"}

        with TestClient(app) as c:
            codes = {c.get("/healthz").status_code for _ in range(50)}
        assert codes == {200}, "a 429 to the kubelet restarts the pod under load"

    def test_a_normal_route_under_the_same_flood_is_refused(self, tight):
        """The control for the test above: the exemption is the reason, not a dead limiter."""
        app = FastAPI()
        app.add_middleware(RateLimitMiddleware)

        @app.get("/v1/thing")
        def thing():
            return {"ok": True}

        with TestClient(app) as c:
            codes = [c.get("/v1/thing").status_code for _ in range(50)]
        assert 429 in codes


class TestItIsKeyedByIdentityNotAddress:
    def test_two_keys_from_one_address_are_two_callers(self):
        """Behind an Ingress every request shares a peer address."""
        a = caller_key(_request({"Authorization": "Bearer aaa"}, host="10.0.0.9"))
        b = caller_key(_request({"Authorization": "Bearer bbb"}, host="10.0.0.9"))
        assert a != b

    def test_one_key_from_two_addresses_is_one_caller(self):
        a = caller_key(_request({"Authorization": "Bearer aaa"}, host="10.0.0.1"))
        b = caller_key(_request({"Authorization": "Bearer aaa"}, host="10.0.0.2"))
        assert a == b

    def test_the_credential_is_never_retained(self):
        key = caller_key(_request({"Authorization": "Bearer sk-super-secret-value"}))
        assert "sk-super-secret-value" not in key
        assert key.startswith("k:")

    def test_it_falls_back_to_the_address_when_there_is_no_token(self):
        assert caller_key(_request(host="10.0.0.7")) == "ip:10.0.0.7"

    def test_an_empty_bearer_is_not_an_identity(self):
        assert caller_key(_request({"Authorization": "Bearer   "})).startswith("ip:")


class TestTheTableIsBounded:
    def test_it_stops_growing_at_the_cap(self, limiter, tight, monkeypatch):
        """The middleware runs before route auth, so arbitrary tokens can create buckets."""
        monkeypatch.setattr(settings, "RATE_LIMIT_MAX_TRACKED", 25)
        for i in range(500):
            limiter.allow(f"k:{i}", float(i))
        assert len(limiter._buckets) <= 25

    def test_it_evicts_the_least_recently_seen(self, limiter, tight, monkeypatch):
        monkeypatch.setattr(settings, "RATE_LIMIT_MAX_TRACKED", 3)
        for name in ("a", "b", "c"):
            limiter.allow(name, 0.0)
        limiter.allow("a", 1.0)      # 'a' is now the most recent, 'b' the oldest
        limiter.allow("d", 2.0)
        assert "b" not in limiter._buckets
        assert "a" in limiter._buckets


class TestItIsMountedWhereItMustBe:
    def test_a_429_still_carries_cors_headers(self, tight):
        """Outside CORS, a browser client sees an opaque network error, not a 429."""
        app = FastAPI()
        app.add_middleware(RateLimitMiddleware)          # added first ⇒ innermost
        app.add_middleware(CORSMiddleware, allow_origins=["https://ui.example"],
                           allow_methods=["*"], allow_headers=["*"])

        @app.get("/v1/thing")
        def thing():
            return {"ok": True}

        with TestClient(app) as c:
            last = None
            for _ in range(10):
                last = c.get("/v1/thing", headers={"Origin": "https://ui.example"})
                if last.status_code == 429:
                    break
        assert last is not None and last.status_code == 429
        assert last.headers.get("access-control-allow-origin") == "https://ui.example"
        assert last.headers.get("retry-after")

    def test_main_adds_it_before_cors_and_logging(self):
        """`add_middleware` is reverse order, so 'added first' means 'innermost'."""
        from app import main
        src = inspect.getsource(main)
        i_rl = src.index("app.add_middleware(RateLimitMiddleware)")
        i_cors = src.index("app.add_middleware(\n    CORSMiddleware,")
        i_log = src.index("app.add_middleware(RequestLoggingMiddleware)")
        assert i_rl < i_cors < i_log

    def test_the_rejection_is_logged(self, tight, caplog):
        app = FastAPI()
        app.add_middleware(RateLimitMiddleware)

        @app.get("/v1/thing")
        def thing():
            return {"ok": True}

        with caplog.at_level("WARNING"), TestClient(app) as c:
            for _ in range(10):
                c.get("/v1/thing")
        assert any("rate_limit: 429" in r.message for r in caplog.records)


class TestTheLimitsAreWrittenDownNotHidden:
    def test_the_module_states_the_per_replica_multiplier(self):
        doc = " ".join((rate_limit.__doc__ or "").split())
        assert "per replica" in doc
        assert "not a fleet-wide quota" in doc

    def test_the_module_states_it_is_not_ddos_defence(self):
        doc = " ".join((rate_limit.__doc__ or "").split())
        assert "fair use, not DDoS defence" in doc
        assert "ingress" in doc
