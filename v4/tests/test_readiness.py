"""`/readyz` — readiness must be a *different* answer from liveness.

The defect these tests lock down: `/healthz` is deliberately static (a liveness probe that
touches a dependency turns one blip into a restart loop), and the Helm chart pointed **both**
probes at it. So a replica kept reporting "route traffic to me" right up until process exit —
during a rolling update Kubernetes removes the pod from Endpoints asynchronously, and requests
kept landing on a replica that was already closing its pools.

Two properties are asserted, and the second is the one that actually matters:

1. `/readyz` reports 200 while serving.
2. `/readyz` reports **503 once draining** — so the pod leaves the Service *before* teardown.

Also asserted: `/readyz` does **not** consult Postgres. That is not an oversight to be fixed
later; a readiness probe that pings a shared database takes every replica out of rotation
simultaneously when that database blips, converting degradation into a total outage.
"""

from __future__ import annotations

import pytest
from app.api.v1.endpoints.health import router as health_router
from app.core import readiness
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(health_router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _restore_readiness():
    """Never leak readiness state between tests — it is process-global by design."""
    before = readiness.is_ready()
    yield
    readiness.set_ready(before)


class TestReadyz:
    def test_ready_returns_200(self, client):
        readiness.set_ready(True)
        r = client.get("/readyz")
        assert r.status_code == 200
        assert r.json()["status"] == "ready"

    def test_draining_returns_503(self, client):
        """The load-bearing case: once shutdown starts, stop taking traffic."""
        readiness.set_ready(False)
        r = client.get("/readyz")
        assert r.status_code == 503, (
            "a draining replica still advertised itself as ready — this is exactly the "
            "rolling-update request loss /readyz exists to prevent"
        )
        assert r.json()["status"] == "draining"

    def test_liveness_stays_200_while_draining(self, client):
        """Liveness must NOT follow readiness: a draining pod is not a wedged pod, and
        failing liveness here would have Kubernetes kill it mid-drain."""
        readiness.set_ready(False)
        assert client.get("/healthz").status_code == 200

    def test_readyz_does_not_touch_the_database(self, client, monkeypatch):
        """Deliberate non-dependency: if this ever starts probing Postgres, a DB blip empties
        the Service of endpoints everywhere at once."""
        from app.db import audit

        def _boom(*_a, **_kw):  # pragma: no cover - must never be called
            raise AssertionError("/readyz consulted the database")

        monkeypatch.setattr(audit, "init_audit_pool", _boom, raising=False)
        monkeypatch.setattr(audit, "log_request", _boom, raising=False)
        readiness.set_ready(True)
        assert client.get("/readyz").status_code == 200


class TestReadinessState:
    def test_defaults_to_not_ready(self):
        """A process that has not finished startup must not advertise readiness."""
        readiness.set_ready(False)
        assert readiness.is_ready() is False

    def test_set_ready_round_trips(self):
        readiness.set_ready(True)
        assert readiness.is_ready() is True
        readiness.set_ready(False)
        assert readiness.is_ready() is False
