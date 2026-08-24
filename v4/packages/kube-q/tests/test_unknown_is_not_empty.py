"""'I could not reach the cluster' must never be rendered as 'that does not exist'.

`/ns <name>` validates against the backend's namespace list, and the REPL is deliberately careful
about it: it keeps three states — present, absent, and *undetermined* — and only rejects on a
definite absent, so a backend outage cannot stop an operator setting their namespace.

That care was defeated one layer down. `fetch_namespaces` mapped any 200 to
`body.get("namespaces", [])`, so a response it could not interpret became an empty list, which the
caller reads as proof. Combined with the server bug fixed in the same pass (a failed `kubectl`
returned `200 {"namespaces": []}`), an expired kubeconfig produced:

    Namespace 'prod' not found in the cluster.

— a confident, wrong, actionable statement, mid-incident, pointing at the wrong problem entirely.

The rule these tests hold: only a genuine list is an answer. Anything else is "unknown", and
unknown must fail open.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

import kube_q.cli.repl as repl_mod
import kube_q.cli.store as store_mod
from kube_q.cli.repl import ReplConfig, run_repl
from kube_q.core.transport import check_health, fetch_namespaces

BASE = "http://localhost:8000"


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_mod, "DB_PATH", tmp_path / "history.db")
    monkeypatch.setattr(repl_mod, "_HISTORY_FILE", str(tmp_path / "history"))


class _FakePromptSession:
    def __init__(self, inputs):
        self._inputs = list(inputs)

    def prompt(self, *_args, **_kwargs) -> str:
        if not self._inputs:
            raise EOFError
        return self._inputs.pop(0)


def _run(inputs: list[str]) -> None:
    cfg = ReplConfig(url=BASE, stream=True, user_id="u1", initial_conversation_id="s1",
                     skip_health_check=True, show_header=False, quiet=True)
    with (
        patch.object(repl_mod, "_make_prompt_session", return_value=_FakePromptSession(inputs)),
        patch("kube_q.transport.Live"),
        patch("kube_q.cli.repl._print_logo"),
    ):
        run_repl(cfg)


class TestFetchNamespacesOnlyAnswersWhenItKnows:
    """`None` means undetermined; `[]` means the cluster really has none."""

    @pytest.mark.parametrize(
        ("label", "status", "body", "expected"),
        [
            ("a real list",              200, {"namespaces": ["default", "prod"]},
             ["default", "prod"]),
            ("a genuinely empty list",   200, {"namespaces": []},                  []),
            ("body missing the key",     200, {"detail": "oops"},                  None),
            ("key present but null",     200, {"namespaces": None},                None),
            ("key present but a string", 200, {"namespaces": "prod"},              None),
            ("server reports 503",       503, {"detail": "cannot list namespaces"}, None),
            ("server errors 500",        500, {"detail": "boom"},                  None),
            ("auth rejected",            401, {"detail": "no key"},                None),
        ],
    )
    def test_only_a_list_counts_as_an_answer(self, label, status, body, expected):
        def handler(_request):
            return httpx.Response(status, json=body)

        with patch("kube_q.core.transport.make_client",
                   return_value=httpx.Client(transport=httpx.MockTransport(handler))):
            assert fetch_namespaces(BASE, "u1") == expected, label

    def test_a_transport_failure_is_undetermined_not_empty(self):
        def handler(_request):
            raise httpx.ConnectError("refused")

        with patch("kube_q.core.transport.make_client",
                   return_value=httpx.Client(transport=httpx.MockTransport(handler))):
            assert fetch_namespaces(BASE, "u1") is None


class TestTheReplRejectsOnlyOnProof:
    """The user-visible half: what `/ns prod` actually prints."""

    def _ns_route(self, status: int, body: dict):
        respx.get(f"{BASE}/v1/namespaces").mock(return_value=httpx.Response(status, json=body))

    @respx.mock
    def test_a_known_namespace_is_accepted(self, capsys):
        self._ns_route(200, {"namespaces": ["default", "prod"]})
        _run(["/ns prod"])
        out = capsys.readouterr().out
        assert "Active namespace set to" in out and "not found" not in out

    @respx.mock
    def test_a_namespace_absent_from_a_good_list_is_rejected(self, capsys):
        """The behaviour worth keeping — a definite answer still rejects."""
        self._ns_route(200, {"namespaces": ["default", "kube-system"]})
        _run(["/ns prod"])
        assert "not found in the cluster" in capsys.readouterr().out

    @respx.mock
    def test_a_backend_that_cannot_reach_the_cluster_does_not_reject(self, capsys):
        """503 from the server ⇒ undetermined ⇒ accept. This is the incident case."""
        self._ns_route(503, {"detail": "Cannot list namespaces: connection refused"})
        _run(["/ns prod"])
        out = capsys.readouterr().out
        assert "not found in the cluster" not in out, (
            "an operator whose cluster is unreachable was told their namespace does not exist"
        )
        assert "Active namespace set to" in out

    @respx.mock
    def test_an_uninterpretable_200_does_not_reject(self, capsys):
        """The client-side half of the fix: a 200 we cannot read is not zero namespaces."""
        self._ns_route(200, {"detail": "gateway says hello"})
        _run(["/ns prod"])
        out = capsys.readouterr().out
        assert "not found in the cluster" not in out
        assert "Active namespace set to" in out


class TestHealthReportsTheTimeoutItActuallyUsed:
    """`KUBE_Q_HEALTH_TIMEOUT` is configurable; the message used to say '5 s' regardless."""

    @pytest.mark.parametrize("timeout", [2.0, 5.0, 30.0])
    def test_the_message_names_the_configured_timeout(self, timeout):
        def handler(_request):
            raise httpx.ConnectTimeout("too slow")

        with patch("kube_q.core.transport.make_client",
                   return_value=httpx.Client(transport=httpx.MockTransport(handler))):
            ok, reason = check_health(BASE, timeout=timeout)
        assert ok is False
        assert f"within {timeout:g} s" in reason, reason
