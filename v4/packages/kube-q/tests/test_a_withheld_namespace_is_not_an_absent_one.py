"""`/ns monitoring` told the operator a namespace that exists was "not found in the cluster".

This client is deliberately careful: it keeps **three** states for "does this namespace exist",
and only refuses on a definite absence, so that an unreachable backend cannot stop an operator
working. That care was defeated from the other end. The backend removes
`KUBECTL_BLOCKED_NAMESPACES` from `GET /v1/namespaces` and, until 2026-08-24, said nothing about
having done it — so a withheld namespace and an absent one arrived here as the same bytes, and
the third state was unreachable no matter how carefully this side was written.

What the operator saw:

    /ns monitoring
    Namespace 'monitoring' not found in the cluster. Use `list all ns` to see available namespaces.

Both halves are wrong. The namespace exists, and `list all ns` goes through `run_kubectl`, which
*does* end its listing with "This listing is NOT the complete set" — the advice pointed at the
one path that would have contradicted the message giving it.

The fix is a distinction, not a message: `fetch_namespace_listing` reports whether the listing
was complete, and *absent from a listing that admits it is short* is no longer evidence. Note
which way that resolves — an unknown namespace is now **accepted** when the listing is short.
That is the same trade this client already made for an unreachable backend, and the subsequent
kubectl call refuses a protected namespace out loud anyway.
"""

import httpx
import respx

from kube_q.cli.repl import namespace_set_result
from kube_q.core.transport import (
    NamespaceListing,
    fetch_namespace_listing,
    fetch_namespaces,
    namespace_verdict,
)

BASE = "http://ki.test"
WITHHELD = ("[Protected] 2 namespace(s) withheld — they belong to a namespace in "
            "KUBECTL_BLOCKED_NAMESPACES. This listing is NOT the complete set.")


def _mock(body: dict | None = None, status: int = 200, text: str | None = None):
    resp = (httpx.Response(status, text=text) if text is not None
            else httpx.Response(status, json=body or {}))
    respx.get(f"{BASE}/v1/namespaces").mock(return_value=resp)


def _listing() -> NamespaceListing:
    return fetch_namespace_listing(BASE, "u1")


# The REPL's decision is `namespace_verdict` itself — imported, not re-created here. A test that
# rebuilds the logic it is checking cannot fail when the original drifts away from it.
verdict = namespace_verdict


# ── 1. the distinction the backend now carries ────────────────────────────────────────────────


@respx.mock
def test_a_short_listing_is_marked_incomplete():
    _mock({"namespaces": ["default", "shop"], "withheldByPolicy": WITHHELD})
    assert _listing() == NamespaceListing(["default", "shop"], False)


@respx.mock
def test_a_full_listing_is_complete():
    _mock({"namespaces": ["default", "shop"], "withheldByPolicy": ""})
    assert _listing().complete is True


@respx.mock
def test_an_empty_cluster_is_complete_not_unknown():
    _mock({"namespaces": [], "withheldByPolicy": ""})
    assert _listing() == NamespaceListing([], True)


@respx.mock
def test_an_older_backend_without_the_field_is_assumed_complete():
    """Exactly what this client did before the field existed — no behaviour change against a
    server that cannot tell us, and the server it ships with always can."""
    _mock({"namespaces": ["default"]})
    assert _listing().complete is True


# ── 2. the sentence that was false ────────────────────────────────────────────────────────────


@respx.mock
def test_a_withheld_namespace_is_no_longer_reported_absent():
    _mock({"namespaces": ["default", "shop"], "withheldByPolicy": WITHHELD})
    assert verdict(_listing(), "monitoring") is None, "None means 'not proven absent'"


@respx.mock
def test_a_genuinely_absent_one_is_still_refused_when_the_list_is_whole():
    _mock({"namespaces": ["default", "shop"], "withheldByPolicy": ""})
    assert verdict(_listing(), "typo-ns") is False


@respx.mock
def test_a_present_namespace_is_still_accepted_either_way():
    _mock({"namespaces": ["default", "shop"], "withheldByPolicy": WITHHELD})
    assert verdict(_listing(), "shop") is True


@respx.mock
def test_an_unreachable_backend_is_still_unknown():
    _mock(status=503)
    assert verdict(_listing(), "anything") is None


@respx.mock
def test_an_unreadable_body_is_unknown_not_empty():
    _mock(text="not json at all")
    assert _listing() == NamespaceListing(None, True)


@respx.mock
def test_a_200_whose_namespaces_field_is_wrong_shape_is_unknown():
    _mock({"namespaces": "default shop"})
    assert _listing().names is None


# ── 3. the old entry point still behaves ──────────────────────────────────────────────────────


@respx.mock
def test_fetch_namespaces_still_returns_a_plain_list():
    """Tab-completion imports this name and wants the visible namespaces, nothing more."""
    _mock({"namespaces": ["default", "shop"], "withheldByPolicy": WITHHELD})
    assert fetch_namespaces(BASE, "u1") == ["default", "shop"]


@respx.mock
def test_fetch_namespaces_still_returns_none_on_failure():
    _mock(status=500)
    assert fetch_namespaces(BASE, "u1") is None


# ── 4. the REPL uses the distinction, not just the data ───────────────────────────────────────


class TestWhatTheOperatorIsActuallyTold:
    """The rule being right is worth nothing if `/ns` does not use it, and an imported name
    stays importable whether or not it is called. `namespace_set_result` is the REPL's own
    branch, so these run the sentence rather than scanning the source for one."""

    def test_a_withheld_namespace_is_not_called_missing(self):
        accept, message = namespace_set_result(NamespaceListing(["default"], False), "monitoring")
        assert accept is True
        assert "not found" not in message

    def test_and_it_does_not_send_them_to_the_path_that_contradicts_it(self):
        _, message = namespace_set_result(NamespaceListing(["default"], False), "monitoring")
        assert "list all ns" not in message

    def test_a_definitely_absent_one_is_still_refused(self):
        accept, message = namespace_set_result(NamespaceListing(["default"], True), "typo-ns")
        assert accept is False
        assert "not found in the cluster" in message

    def test_an_unreachable_backend_does_not_block_the_operator(self):
        accept, _ = namespace_set_result(NamespaceListing(None), "anything")
        assert accept is True

    def test_a_present_namespace_is_accepted_and_named(self):
        accept, message = namespace_set_result(NamespaceListing(["shop"], True), "shop")
        assert accept is True and "shop" in message


def test_fetch_namespaces_keeps_an_empty_cluster_distinct_from_unknown():
    """`[] or None` is the easy way to lose exactly the distinction this pass is about."""
    with respx.mock:
        _mock({"namespaces": [], "withheldByPolicy": ""})
        assert fetch_namespaces(BASE, "u1") == []
