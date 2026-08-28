"""A page that states its own permissions has to have asked.

`deploy/huggingface-space/app.py` printed, in fixed HTML, *"This demo key holds the
`readonly` role, so a write is refused by RBAC before it runs"*. Nothing read the key's
actual role. `KI_API_KEY` is an environment variable, the `ki-op-`/`ki-ro-` prefix is a
naming convention rather than a grant, and an unrecognised key is `readonly` while a
deployment with no keys configured makes every caller `admin` — so the sentence was true
only for the one deployment it was written for.

It cost a real take: the first chat-UI recording (2026-08-28, `scripts/demo/chat-ui/`) was
made with an operator key, and the page asserted `readonly` underneath an agent that would
have executed the write. Cropping the frame was available and wrong.

The fix is the missing question, not better wording: the server grew `GET /v1/auth/whoami`,
and the footer renders what comes back. Two properties are pinned here — the server reports
the caller's own role and no one else's, and the page never renders the readonly sentence
for a key that is not readonly, including when it could not find out.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import config
from app.main import app

_SPACE_APP = Path(__file__).resolve().parents[2] / "deploy" / "huggingface-space" / "app.py"


# ── the server side: /v1/auth/whoami ─────────────────────────────────────────


@pytest.fixture
def keyed(monkeypatch):
    """A server with one key of each role configured."""
    monkeypatch.setattr(config.settings, "KUBEINTELLECT_SUPERADMIN_KEYS", "ki-sa-test", raising=False)
    monkeypatch.setattr(config.settings, "KUBEINTELLECT_ADMIN_KEYS", "ki-admin-test", raising=False)
    monkeypatch.setattr(config.settings, "KUBEINTELLECT_OPERATOR_KEYS", "ki-op-test", raising=False)
    monkeypatch.setattr(config.settings, "KUBEINTELLECT_READONLY_KEYS", "ki-ro-test", raising=False)
    return TestClient(app)


@pytest.mark.parametrize(
    "key,role",
    [
        ("ki-sa-test", "superadmin"),
        ("ki-admin-test", "admin"),
        ("ki-op-test", "operator"),
        ("ki-ro-test", "readonly"),
    ],
)
def test_whoami_reports_the_callers_own_role(keyed, key, role):
    response = keyed.get("/v1/auth/whoami", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 200
    assert response.json() == {"role": role}


def test_whoami_is_not_a_way_to_probe_without_a_key(keyed):
    """It sits on the authenticated router, so an absent key never reaches the handler."""
    assert keyed.get("/v1/auth/whoami").status_code == 401
    assert keyed.get(
        "/v1/auth/whoami", headers={"Authorization": "Bearer ki-not-a-key"}
    ).status_code in (401, 403)


# ── the client side: the Space footer ────────────────────────────────────────


def _load_space_app():
    """Import the Space app with Gradio stubbed — it is a client, not a server package.

    The app is a single file deployed to Hugging Face and is not a workspace member, so
    `gradio` is not installed in this venv. Everything under test here builds HTML strings;
    the stub only has to survive the module-level `with gr.Blocks()` UI construction.
    """
    stub = MagicMock()
    saved = sys.modules.get("gradio")
    sys.modules["gradio"] = stub
    try:
        spec = importlib.util.spec_from_file_location("ki_space_app", _SPACE_APP)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if saved is None:
            sys.modules.pop("gradio", None)
        else:
            sys.modules["gradio"] = saved
        sys.modules.pop("ki_space_app", None)


@pytest.fixture(scope="module")
def space() -> ModuleType:
    return _load_space_app()


READONLY_CLAIM = "a write is refused by RBAC before it runs"


def test_the_readonly_sentence_is_reachable_at_all(space):
    """A guard that can never fire is worth nothing — the true case must still render."""
    assert READONLY_CLAIM in space.role_sentence("readonly")
    assert "<code>readonly</code>" in space.role_sentence("readonly")


@pytest.mark.parametrize("role", ["operator", "admin", "superadmin"])
def test_a_write_capable_key_never_renders_the_readonly_sentence(space, role):
    sentence = space.role_sentence(role)
    assert READONLY_CLAIM not in sentence
    assert f"<code>{role}</code>" in sentence
    assert "human approval" in sentence


def test_an_unanswered_probe_claims_nothing(space):
    """The failure mode must not be the flattering one."""
    sentence = space.role_sentence(space._ROLE_UNKNOWN)
    assert READONLY_CLAIM not in sentence
    assert "write-capable" in sentence


def test_an_unrecognised_role_is_named_but_not_described(space):
    sentence = space.role_sentence("auditor")
    assert READONLY_CLAIM not in sentence
    assert "<code>auditor</code>" in sentence
    assert "write-capable" in sentence


def test_the_footer_asks_before_it_states(space, monkeypatch):
    """This is the defect itself: an operator key, and the page must not say readonly."""
    monkeypatch.setattr(space, "key_role", lambda: "operator")
    footer = space.footer_panel()
    assert READONLY_CLAIM not in footer
    assert "<code>operator</code>" in footer

    monkeypatch.setattr(space, "key_role", lambda: "readonly")
    assert READONLY_CLAIM in space.footer_panel()


def test_the_cluster_chip_stops_saying_read_only_too(space, monkeypatch):
    """The status chip carried the same unchecked claim in three fewer words."""
    monkeypatch.setattr(space, "key_role", lambda: "operator")
    monkeypatch.setattr(
        space.httpx, "Client", lambda **kw: _FakeClient({"namespaces": ["default"]})
    )
    assert "read-only" not in space.cluster_panel()
    assert "operator" in space.cluster_panel()


# ── the probe itself ─────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload, status=200):
        self._response = _FakeResponse(payload, status)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, *args, **kwargs):
        return self._response


def test_key_role_returns_unknown_when_the_server_will_not_say(space, monkeypatch):
    """A 404 from a server too old to serve the route lands here, and must not be cached."""
    monkeypatch.setattr(space, "_role_cache", None, raising=False)
    monkeypatch.setattr(space.httpx, "Client", lambda **kw: _FakeClient({}, status=404))
    assert space.key_role() == space._ROLE_UNKNOWN
    assert space._role_cache is None

    monkeypatch.setattr(space.httpx, "Client", lambda **kw: _FakeClient({"role": "operator"}))
    assert space.key_role() == "operator"
    assert space._role_cache == "operator"
