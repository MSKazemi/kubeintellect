"""Authentication hardening for a deployment that is not a laptop.

Two defects this covers, both of which are invisible in a passing test suite:

1. With no keys configured every unauthenticated caller is `admin`. Correct for a first-run
   `kubeintellect serve`; in a data centre it is full HITL-gated cluster write access granted
   silently, and nothing in the product looks wrong.
2. Key comparison used `token in <set>`, so `==` exited early on the first differing byte. Response
   time then varies with how many leading bytes of a real key the caller guessed — an oracle for
   recovering a key byte by byte.
"""
from __future__ import annotations

import hmac

import pytest
from fastapi import HTTPException

from app.api.v1 import auth as auth_mod
from app.core.config import settings


class FakeRequest:
    def __init__(self, token: str | None = None):
        self.headers = {"Authorization": f"Bearer {token}"} if token is not None else {}


@pytest.fixture
def no_keys(monkeypatch):
    for attr in ("superadmin_keys", "admin_keys", "operator_keys", "readonly_keys"):
        monkeypatch.setattr(type(settings), attr, property(lambda self: set()))
    monkeypatch.setattr(settings, "DEMO_KEY_HMAC_SECRET", None)
    yield


# ── the fail-open default, and the switch that closes it ──────────────────────────────────────

def test_unconfigured_server_still_grants_admin_for_local_development(no_keys, monkeypatch):
    """The quickstart must keep working — this is the behaviour REQUIRE_AUTH exists to override."""
    monkeypatch.setattr(settings, "REQUIRE_AUTH", False)
    assert auth_mod.get_user_role(FakeRequest()) == "admin"


def test_require_auth_refuses_to_grant_admin_to_an_unauthenticated_caller(no_keys, monkeypatch):
    monkeypatch.setattr(settings, "REQUIRE_AUTH", True)
    with pytest.raises(HTTPException) as exc:
        auth_mod.get_user_role(FakeRequest())
    assert exc.value.status_code == 401
    # The message must name the actual misconfiguration; "Unauthorized" sends an operator hunting
    # for a bad key when the real problem is that no keys exist.
    assert "REQUIRE_AUTH" in exc.value.detail


# ── constant-time comparison ──────────────────────────────────────────────────────────────────

def test_matches_accepts_only_the_exact_key():
    keys = {"secret-key-aaa", "secret-key-bbb"}
    assert auth_mod._matches("secret-key-aaa", keys) is True
    assert auth_mod._matches("secret-key-bbb", keys) is True
    assert auth_mod._matches("secret-key-aa", keys) is False    # prefix must not pass
    assert auth_mod._matches("secret-key-aaaa", keys) is False  # extension must not pass
    assert auth_mod._matches("", keys) is False


def test_matches_is_empty_safe():
    assert auth_mod._matches("anything", set()) is False


def test_comparison_goes_through_compare_digest(monkeypatch):
    """The property under test is timing, which cannot be asserted reliably on a shared CI box.
    Assert the MECHANISM instead: every candidate must be compared with hmac.compare_digest.
    If someone reverts to `token in keys`, this fails."""
    seen = []
    real = hmac.compare_digest

    def spy(a, b):
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth_mod.hmac, "compare_digest", spy)
    auth_mod._matches("guess", {"real-key-1", "real-key-2"})
    assert len(seen) == 2, "every candidate key must be compared in constant time"


def test_a_valid_key_still_resolves_to_its_role(monkeypatch):
    monkeypatch.setattr(type(settings), "superadmin_keys", property(lambda self: set()))
    monkeypatch.setattr(type(settings), "admin_keys", property(lambda self: {"adm-1"}))
    monkeypatch.setattr(type(settings), "operator_keys", property(lambda self: {"opr-1"}))
    monkeypatch.setattr(type(settings), "readonly_keys", property(lambda self: {"ro-1"}))
    monkeypatch.setattr(settings, "REQUIRE_AUTH", False)

    assert auth_mod.get_user_role(FakeRequest("adm-1")) == "admin"
    assert auth_mod.get_user_role(FakeRequest("opr-1")) == "operator"
    assert auth_mod.get_user_role(FakeRequest("ro-1")) == "readonly"
    with pytest.raises(HTTPException) as exc:
        auth_mod.get_user_role(FakeRequest("nope"))
    assert exc.value.status_code == 401
