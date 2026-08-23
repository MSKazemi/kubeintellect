"""Authentication was a per-endpoint convention, so it existed where somebody remembered it.

Each handler called `get_user_role(request)` itself. Measured 2026-08-20 with auth enabled,
ten of twelve routes answered a request carrying no Authorization header at all:

    GET /v1/digest                        200   what the watchtower did, as a narrative
    GET /v1/findings                      200   detector firings
    GET /v1/episodes/{id}/replay          200   the flight recorder — every command and output
    GET /v1/episodes/{id}/postmortem      200   grounded incident narrative
    GET /v1/events/replay/{session}       200
    GET /v1/namespaces                    200
    GET /v1/v5/status                     200
    GET /v1/detectors                     200   (POST/promote/demote were correctly gated)
    GET /v1/preferences                   200   (PUT/DELETE were correctly gated)
    POST /v1/chat/completions             401   the one route that remembered

The shape of the gap is the tell: in `detectors.py` and `preferences.py` a `_require_writer`
helper gates every *mutation* and no *read*. That is the third consecutive audit pass to find
the same assumption — a read does not need a gate — after `run_helm` and the Loki/Prometheus
tools. A read is not a safe default; it is just a different thing to authorise.

The fix is structural rather than another per-endpoint call, because the convention is exactly
what failed: `api_router` carries the dependency, and the two probe endpoints are mounted on a
separate public router. This test enumerates the routes **from the application itself**, so a
route added tomorrow is covered without anyone remembering to add it here.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core import config
from app.main import app

# Liveness/readiness must answer an unauthenticated kubelet. Anything else added here is a
# deliberate decision to publish an endpoint, and should be argued for in the PR that adds it.
PUBLIC_PATHS = {"/healthz", "/readyz", "/v1/healthz", "/v1/readyz"}

# Bodies for routes that validate their payload before anything else.
BODIES = {
    "/v1/chat/completions": {"messages": [{"role": "user", "content": "x"}]},
    "/v1/auth/demo-keys": {"email": "a@b.com"},
    "/v1/preferences": {"key": "k", "value": "v"},
    "/v1/detectors": {"description": "x"},
}


def _routes():
    """Every (method, path) the application actually serves.

    Read from the app's own OpenAPI schema rather than a hand-written list, so a route added
    tomorrow is covered here without anyone remembering — which is the failure mode this whole
    file exists to close. (`app.routes` nests included routers in this FastAPI version, so it
    is not the flat enumeration it looks like.)
    """
    paths = app.openapi().get("paths", {})
    return sorted(
        (method.upper(), path)
        for path, ops in paths.items()
        for method in ops
        if method.upper() not in ("HEAD", "OPTIONS")
        and path.startswith(("/v1", "/healthz", "/readyz"))
    )


def _concrete(path: str) -> str:
    return (path.replace("{episode_id}", "abc").replace("{session_id}", "abc")
                .replace("{name}", "abc").replace("{key}", "abc"))


@pytest.fixture()
def auth_on(monkeypatch):
    monkeypatch.setattr(type(config.settings), "admin_keys",
                        property(lambda s: {"ki-test-admin"}))
    monkeypatch.setattr(type(config.settings), "operator_keys", property(lambda s: set()))
    monkeypatch.setattr(type(config.settings), "readonly_keys", property(lambda s: set()))
    monkeypatch.setattr(type(config.settings), "superadmin_keys", property(lambda s: set()))
    assert config.settings.auth_enabled


@pytest.fixture()
def client():
    # Deliberately not the `with` form: entering it runs the lifespan, which builds the agent
    # graph. Authentication happens before routing, so no startup is needed to assert it.
    return TestClient(app, raise_server_exceptions=False)


def test_the_route_table_is_not_empty():
    """A guard on the guard: an empty enumeration would make every test below vacuous."""
    assert len(_routes()) >= 12, _routes()


@pytest.mark.parametrize("method,path", _routes())
def test_every_route_challenges_an_anonymous_caller(method, path, client, auth_on):
    if path in PUBLIC_PATHS:
        pytest.skip("deliberately public probe endpoint")
    r = client.request(method, _concrete(path), json=BODIES.get(path))
    assert r.status_code == 401, (
        f"{method} {path} answered {r.status_code} with no Authorization header"
    )


@pytest.mark.parametrize("method,path", _routes())
def test_every_route_rejects_a_bad_key(method, path, client, auth_on):
    if path in PUBLIC_PATHS:
        pytest.skip("deliberately public probe endpoint")
    r = client.request(method, _concrete(path), json=BODIES.get(path),
                       headers={"Authorization": "Bearer not-a-key"})
    assert r.status_code == 401, f"{method} {path} accepted an invalid key"


@pytest.mark.parametrize("path", sorted(PUBLIC_PATHS))
def test_the_probe_endpoints_stay_public(path, client, auth_on):
    """Gating these would make the pod fail its kubelet probes and restart-loop."""
    r = client.get(path)
    assert r.status_code != 401, f"{path} must answer an unauthenticated kubelet"


class TestTheGateDoesNotChangeWhatCallersMayDo:
    def test_a_valid_key_still_gets_through(self, client, auth_on):
        for path in ("/v1/findings", "/v1/digest", "/v1/v5/status"):
            r = client.get(path, headers={"Authorization": "Bearer ki-test-admin"})
            assert r.status_code == 200, f"{path} -> {r.status_code}"

    def test_open_mode_is_preserved_when_no_keys_are_configured(self, client, monkeypatch):
        """Documented backward compatibility: no keys configured ⇒ every caller is admin."""
        for prop in ("admin_keys", "operator_keys", "readonly_keys", "superadmin_keys"):
            monkeypatch.setattr(type(config.settings), prop, property(lambda s: set()))
        monkeypatch.setattr(config.settings, "DEMO_KEY_HMAC_SECRET", "")
        assert not config.settings.auth_enabled
        assert client.get("/v1/findings").status_code == 200


class TestTheReadWriteAsymmetryIsGone:
    """The reads in these two files were open while their writes were gated."""

    @pytest.mark.parametrize("method,path", [
        ("GET", "/v1/detectors"),
        ("GET", "/v1/detectors/abc/shadow-findings"),
        ("GET", "/v1/preferences"),
    ])
    def test_the_read_halves_are_gated_too(self, method, path, client, auth_on):
        assert client.request(method, path).status_code == 401
