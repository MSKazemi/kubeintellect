r"""Authentication is proven for every route; authorization by role was not.

`test_every_route_is_authenticated.py` enumerates every route from the app itself and proves
each one challenges an anonymous caller and rejects a bad key. Its `auth_on` fixture configures
one admin key and leaves `readonly_keys` empty, so no *valid but insufficient* key is ever sent
anywhere. Nothing asserted that a readonly key cannot write — and this repository has twice
found RBAC silently switched off by an edit that looked unrelated (widening a
`config: RunnableConfig` annotation; a `ruff --fix` rewriting an `Optional[...]`), which is
exactly the failure a per-endpoint convention cannot survive.

**The naive version of this test passes for the wrong reason,** which is the point of the
`_reachable` fixture below. Measured before writing it, with one readonly key configured:

    POST   /v1/detectors          404   ← NL authoring is off by default
    POST   /v1/detectors/abc/promote  404
    PUT    /v1/preferences        503   ← memory hierarchy not active
    DELETE /v1/preferences/abc    503
    POST   /v1/auth/demo-keys     403   ← the only route whose role check was reached

Four of the six never reached their role check at all. A test asserting "not 200" would have
been green while `_require_writer` was deleted from both files. So the fixture turns the
feature flags on and makes the store look active, and every assertion below demands exactly
**403** — the role check answering, not an earlier guard hiding it.

The route list comes from the application's OpenAPI schema, so a mutating route added tomorrow
is covered without anyone remembering this file exists.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core import config
from app.main import app

# Routes a readonly caller may legitimately call with a mutating verb.
# `/v1/chat/completions` is a POST because it creates a turn, not because it changes the
# cluster: the public demo key is readonly by design, and every state-changing *action* inside
# it is gated in the tool layer (`kubectl_tool` checks the role before the HITL block).
# Anything added here is a deliberate decision that readonly may do it.
READONLY_MAY_CALL = {("POST", "/v1/chat/completions")}

BODIES = {
    "/v1/preferences": {"key": "k", "value": "v"},
    "/v1/detectors": {"description": "x"},
    "/v1/auth/demo-keys": {"email": "a@b.com"},
}


def _mutating_routes():
    paths = app.openapi().get("paths", {})
    return sorted(
        (method.upper(), path)
        for path, ops in paths.items()
        for method in ops
        if method.upper() in ("POST", "PUT", "PATCH", "DELETE") and path.startswith("/v1")
    )


_ROUTES = [r for r in _mutating_routes() if r not in READONLY_MAY_CALL]


def _concrete(path: str) -> str:
    return (path.replace("{episode_id}", "abc").replace("{session_id}", "abc")
                .replace("{name}", "abc").replace("{key}", "abc"))


@pytest.fixture()
def readonly_caller(monkeypatch):
    """One admin key and one readonly key configured."""
    for prop, val in (("admin_keys", {"ki-admin"}), ("operator_keys", set()),
                      ("readonly_keys", {"ki-ro"}), ("superadmin_keys", set())):
        monkeypatch.setattr(type(config.settings), prop, property(lambda s, v=val: v))
    assert config.settings.auth_enabled


@pytest.fixture()
def reachable(monkeypatch):
    """Make the role check the *first* thing a write route can fail on.

    Without this the feature flags and the missing store answer 404/503 first and the role
    check is never executed, so the assertions below would hold with RBAC removed entirely.
    """
    monkeypatch.setattr(config.settings, "NL_DETECTOR_AUTHORING_ENABLED", True)
    monkeypatch.setattr(config.settings, "PREFERENCE_MEMORY_ENABLED", True)
    monkeypatch.setattr(config.settings, "DEMO_KEY_HMAC_SECRET", "s" * 32)
    from app.memory import service
    monkeypatch.setattr(service, "_pool", object())


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def test_there_are_mutating_routes_to_check():
    """A guard on the guard: an empty list would make every case below vacuous."""
    assert len(_ROUTES) >= 5, _ROUTES


@pytest.mark.parametrize(("method", "path"), _ROUTES, ids=[f"{m} {p}" for m, p in _ROUTES])
def test_a_readonly_key_cannot_reach_a_write_route(method, path, client,
                                                   readonly_caller, reachable):
    response = client.request(method, _concrete(path), json=BODIES.get(path),
                              headers={"Authorization": "Bearer ki-ro"})
    assert response.status_code == 403, (
        f"{method} {path} answered {response.status_code} to a readonly key. 403 means the "
        f"role check ran and refused; anything else means it did not run — including 404/503 "
        f"from a feature flag or an absent store, which hides a missing check rather than "
        f"being one."
    )


@pytest.mark.parametrize(("method", "path"), _ROUTES, ids=[f"{m} {p}" for m, p in _ROUTES])
def test_the_same_route_gets_past_the_role_check_for_an_admin(method, path, client,
                                                              readonly_caller, reachable):
    """The mirror: without it, a route that 403s everyone would look perfectly guarded."""
    response = client.request(method, _concrete(path), json=BODIES.get(path),
                              headers={"Authorization": "Bearer ki-admin"})
    assert response.status_code != 403, (
        f"{method} {path} refuses an admin key — the role check is not merely present, it is wrong"
    )


class TestTheFixtureItselfIsLoadBearing:
    """If `reachable` stopped working, every case above would pass for the wrong reason."""

    def test_without_it_the_role_check_is_never_reached(self, client, readonly_caller):
        masked = {}
        for method, path in _ROUTES:
            r = client.request(method, _concrete(path), json=BODIES.get(path),
                               headers={"Authorization": "Bearer ki-ro"})
            masked[f"{method} {path}"] = r.status_code
        assert any(code in (404, 503) for code in masked.values()), (
            "no route is masked by a feature flag or an absent store any more — if that is a "
            f"deliberate change, this test and the `reachable` fixture can go: {masked}"
        )
