"""`GET /v1/namespaces` removed protected namespaces and did not say it had.

The 2026-08-20 pass added two things on the same day: the protected-namespace filter, and the
`withheld_note` vocabulary whose entire premise is *"a filtered listing and a complete listing
are the same bytes."* `run_kubectl` got both. This endpoint got only the filter — and its own
docstring said "the same guarantee, two code paths, one of them not enforcing it" about the
half it did receive.

So the product answered one question three ways. Measured 2026-08-24 against a cluster holding
`default kube-system monitoring shop`:

    kubectl get ns monitoring   -> [Protected] Access to namespace 'monitoring' is not permitted
    kubectl get ns              -> ... "This listing is NOT the complete set."
    GET /v1/namespaces          -> {"namespaces": ["default", "shop"]}          <- silence

Silence is what travelled. `kq` treats a definite absence as proof, so `/ns monitoring` printed
**"Namespace 'monitoring' not found in the cluster"** — false — and then recommended
`list all ns`, the one path that would have contradicted it.

The marker is the shared one, not a new spelling: structured payloads carry `withheldByPolicy`
(`namespace_guard.WITHHELD_KEY`) rather than a trailing sentence, because a `[Protected]` line
appended after `json.dumps` is not JSON. The field carries a count, never the withheld names —
saying a listing is short must not re-publish what was taken out of it.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest
from app.api.v1.endpoints.namespaces import NamespacesResponse, router
from app.core.config import settings
from app.tools.namespace_guard import WITHHELD_KEY, withheld_sentence
from fastapi import FastAPI
from starlette.testclient import TestClient

CLUSTER = "default kube-system monitoring shop"


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def _get(stdout: str, returncode: int = 0) -> dict:
    proc = subprocess.CompletedProcess(args=["kubectl"], returncode=returncode,
                                       stdout=stdout, stderr="")
    with patch("app.api.v1.endpoints.namespaces.subprocess.run", return_value=proc):
        return _client().get("/namespaces").json()


@pytest.fixture(autouse=True)
def _blocklist_is_real():
    """Vacuity guard — every assertion below is empty if these stopped being blocked."""
    assert {"kube-system", "monitoring"} <= set(settings.kubectl_blocked_namespaces)


# ── 1. a short listing admits it ──────────────────────────────────────────────────────────────


class TestTheListingSaysWhenItIsShort:
    def test_the_filter_still_removes_them(self):
        assert _get(CLUSTER)["namespaces"] == ["default", "shop"]

    def test_and_now_says_so(self):
        body = _get(CLUSTER)
        assert body[WITHHELD_KEY], "a filtered listing that reads as complete is the defect"

    def test_the_count_is_right(self):
        assert _get(CLUSTER)[WITHHELD_KEY] == withheld_sentence(2, "namespace")

    def test_the_sentence_is_the_shared_one(self):
        """Not a second spelling of the same idea — `kubectl get ns -o json` says this exact
        thing, and an operator should not have to learn two."""
        assert "NOT the complete set" in _get(CLUSTER)[WITHHELD_KEY]

    def test_it_does_not_republish_what_it_withheld(self):
        body = _get(CLUSTER)
        assert "monitoring" not in body[WITHHELD_KEY]
        assert "kube-system" not in body[WITHHELD_KEY]


# ── 2. and stays quiet when it is not short ───────────────────────────────────────────────────


class TestNothingRemovedMeansNothingSaid:
    @pytest.mark.parametrize("stdout", ["default shop", "default", ""])
    def test_no_marker(self, stdout):
        assert _get(stdout)[WITHHELD_KEY] == ""

    def test_an_empty_cluster_is_not_a_filtered_one(self):
        """`{"namespaces": []}` with no marker means the cluster is empty. That distinction is
        the reason this field is not simply "was anything filtered, ever"."""
        body = _get("")
        assert body["namespaces"] == [] and body[WITHHELD_KEY] == ""

    def test_the_field_defaults_to_empty(self):
        assert NamespacesResponse(namespaces=[]).withheldByPolicy == ""


# ── 3. the earlier fix on this endpoint still holds ───────────────────────────────────────────


class TestTheFailurePathIsUnchanged:
    def test_a_failure_is_still_a_503_not_an_empty_list(self):
        proc = subprocess.CompletedProcess(args=["kubectl"], returncode=1, stdout="",
                                           stderr="Unable to connect to the server")
        with patch("app.api.v1.endpoints.namespaces.subprocess.run", return_value=proc):
            r = _client().get("/namespaces")
        assert r.status_code == 503
        assert "Unable to connect" in r.json()["detail"]

    def test_a_503_carries_no_namespace_list_to_misread(self):
        proc = subprocess.CompletedProcess(args=["kubectl"], returncode=1, stdout="", stderr="x")
        with patch("app.api.v1.endpoints.namespaces.subprocess.run", return_value=proc):
            assert "namespaces" not in _client().get("/namespaces").json()


# ── 4. the field name tracks the constant ─────────────────────────────────────────────────────


class TestOneVocabulary:
    def test_the_field_is_named_for_the_shared_key(self):
        """If `WITHHELD_KEY` is renamed and this field is not, a client keys off a name the rest
        of the product stopped using — silently, because the field would just read as absent."""
        assert WITHHELD_KEY in NamespacesResponse.model_fields

    def test_the_response_is_still_plain_json(self):
        body = _get(CLUSTER)
        assert isinstance(body, dict) and isinstance(body["namespaces"], list)
