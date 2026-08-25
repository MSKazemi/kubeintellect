"""The gather prompt has always said "if a tool replies that it is not configured or unavailable,
do NOT retry it". Measured 2026-08-24 by driving all eight of those replies: only the two "the URL
is unset" ones contained either word.

A missing binary, a refused backend connection and an unreachable cluster — the three cases where
a retry provably cannot succeed — carried neither, so the rule had no trigger exactly where it
mattered most, and each ignored retry costs a gather round and a round-trip. Worse, kubectl's most
common way of saying the cluster is down ("The connection to the server ... was refused - did you
specify the right host or port?") matched no pattern in the hint layer either, so it produced no
hint and no classification at all.

Every case below is produced by calling the real tool with its transport mocked, never by writing
the string this file expects to see.
"""
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.core.config import settings
from app.tools import kubectl_errors
from app.tools.output_policy import POLICY_LINE_RE, RETRY_CLAUSE, UNAVAILABLE_MARKER

# Real kubectl/helm stderr, copied from the tools themselves rather than paraphrased.
CLUSTER_DOWN = (
    "The connection to the server 127.0.0.1:6443 was refused "
    "- did you specify the right host or port?"
)
DIAL_REFUSED = "Unable to connect to the server: dial tcp 10.0.0.1:6443: connect: connection refused"
NO_SUCH_HOST = "Unable to connect to the server: dial tcp: lookup api.example.com: no such host"
HELM_UNREACHABLE = "Error: Kubernetes cluster unreachable: dial tcp 127.0.0.1:6443: connection refused"
NOT_FOUND = 'Error from server (NotFound): pods "web" not found'
FORBIDDEN = 'Error from server (Forbidden): pods is forbidden: User "x" cannot list pods'


def kubectl_with_stderr(stderr: str, code: int = 1) -> str:
    from app.tools.kubectl_tool import run_kubectl
    proc = MagicMock(stdout="", stderr=stderr, returncode=code)
    with patch("subprocess.run", return_value=proc):
        return run_kubectl.invoke({"command": "kubectl get pods -n default", "stdin": None})


def helm_with_stderr(stderr: str, code: int = 1) -> str:
    from app.tools.helm_tool import run_helm
    proc = MagicMock(stdout="", stderr=stderr, returncode=code)
    with patch("subprocess.run", return_value=proc):
        return run_helm.invoke({"command": "helm list -n default"})


def _refusing_client() -> MagicMock:
    def boom(*a, **k):
        raise httpx.ConnectError("connection refused")
    client = MagicMock()
    client.__enter__.return_value.get.side_effect = boom
    client.__enter__.return_value.post.side_effect = boom
    return client


class TestEveryUnanswerableReplySaysSo:
    def test_prometheus_not_configured(self):
        from app.tools.prometheus_tool import query_prometheus
        with patch.object(settings, "PROMETHEUS_URL", ""):
            assert UNAVAILABLE_MARKER in query_prometheus.invoke({"promql": "up"})

    def test_loki_not_configured(self):
        from app.tools.loki_tool import query_loki
        with patch.object(settings, "LOKI_URL", ""):
            assert UNAVAILABLE_MARKER in query_loki.invoke({"logql": '{app="x"}'})

    def test_prometheus_unreachable(self):
        from app.tools.prometheus_tool import query_prometheus
        with patch("httpx.Client", return_value=_refusing_client()):
            assert UNAVAILABLE_MARKER in query_prometheus.invoke({"promql": "up"})

    def test_loki_unreachable(self):
        from app.tools.loki_tool import query_loki
        with patch("httpx.Client", return_value=_refusing_client()):
            assert UNAVAILABLE_MARKER in query_loki.invoke({"logql": '{app="x"}'})

    def test_kubectl_binary_missing(self):
        from app.tools.kubectl_tool import run_kubectl
        with patch("subprocess.run", side_effect=FileNotFoundError("kubectl")):
            out = run_kubectl.invoke({"command": "kubectl get pods -n default", "stdin": None})
        assert UNAVAILABLE_MARKER in out

    def test_helm_binary_missing(self):
        from app.tools.helm_tool import run_helm
        with patch("subprocess.run", side_effect=FileNotFoundError("helm")):
            assert UNAVAILABLE_MARKER in run_helm.invoke({"command": "helm list -n default"})

    @pytest.mark.parametrize("stderr", [CLUSTER_DOWN, DIAL_REFUSED, NO_SUCH_HOST])
    def test_kubectl_cannot_reach_the_cluster(self, stderr):
        assert UNAVAILABLE_MARKER in kubectl_with_stderr(stderr)

    def test_helm_cannot_reach_the_cluster(self):
        assert UNAVAILABLE_MARKER in helm_with_stderr(HELM_UNREACHABLE)


class TestItStaysOffTheAnswerableOnes:
    @pytest.mark.parametrize("stderr", [NOT_FOUND, FORBIDDEN])
    def test_a_cluster_fact_is_not_an_unavailable_tool(self, stderr):
        # A missing pod and an RBAC denial are answers about the cluster — a differently shaped
        # command can still get somewhere, so telling the model to stop calling the tool would
        # cost it the investigation.
        assert UNAVAILABLE_MARKER not in kubectl_with_stderr(stderr)

    def test_a_successful_command_is_untouched(self):
        from app.tools.kubectl_tool import run_kubectl
        proc = MagicMock(stdout="pod-1  1/1  Running", stderr="", returncode=0)
        with patch("subprocess.run", return_value=proc):
            out = run_kubectl.invoke({"command": "kubectl get pods -n default", "stdin": None})
        assert UNAVAILABLE_MARKER not in out

    def test_prometheus_answers_normally_when_it_can(self):
        from app.tools.prometheus_tool import query_prometheus
        payload = {"status": "success", "data": {"resultType": "vector", "result": [
            {"metric": {"__name__": "up"}, "value": [0, "1"]}]}}
        resp = MagicMock(status_code=200)
        resp.json.return_value = payload
        client = MagicMock()
        client.__enter__.return_value.get.return_value = resp
        with patch("httpx.Client", return_value=client):
            assert UNAVAILABLE_MARKER not in query_prometheus.invoke({"promql": "up"})


class TestTheOriginalErrorIsStillThere:
    def test_the_stderr_is_not_replaced_by_the_marker(self):
        out = kubectl_with_stderr(CLUSTER_DOWN)
        assert "127.0.0.1:6443" in out
        assert out.startswith("[kubectl exited 1]")

    def test_the_hint_layer_still_fires(self):
        assert "kube-apiserver unreachable" in kubectl_with_stderr(CLUSTER_DOWN)

    def test_the_marker_is_a_policy_line_so_the_trims_keep_it(self):
        marker_line = [ln for ln in kubectl_with_stderr(CLUSTER_DOWN).splitlines()
                       if UNAVAILABLE_MARKER in ln][0]
        assert POLICY_LINE_RE.search(marker_line)


class TestTheClassifierMatchesWhatKubectlActuallyPrints:
    @pytest.mark.parametrize("stderr,expected", [
        (CLUSTER_DOWN, "apiserver_unreachable"),
        (DIAL_REFUSED, "apiserver_unreachable"),
        (NO_SUCH_HOST, "unable_to_connect"),
    ])
    def test_a_cluster_that_is_down_is_classified(self, stderr, expected):
        name, hint = kubectl_errors.interpret(stderr)
        assert name == expected, f"unclassified, so no hint and no marker: {stderr!r}"
        assert hint

    @pytest.mark.parametrize("stderr", [CLUSTER_DOWN, DIAL_REFUSED, NO_SUCH_HOST])
    def test_and_classified_as_terminal(self, stderr):
        assert kubectl_errors.interpret(stderr)[0] in kubectl_errors.TERMINAL_PATTERNS

    @pytest.mark.parametrize("stderr", [NOT_FOUND, FORBIDDEN])
    def test_cluster_facts_are_classified_but_not_terminal(self, stderr):
        name, _ = kubectl_errors.interpret(stderr)
        assert name is not None
        assert name not in kubectl_errors.TERMINAL_PATTERNS

    def test_every_terminal_name_is_a_pattern_that_exists(self):
        names = {p.name for p in kubectl_errors._PATTERNS}
        assert kubectl_errors.TERMINAL_PATTERNS <= names
        assert kubectl_errors.TERMINAL_PATTERNS


class TestTheMarkerNeverLeads:
    """It is a trailing line, never a wrapper.

    Other readers of these replies key on how they *start*: the ACI read verbs decide "refusal,
    not content" from a leading `[Error]`. Wrapping that text in the notice turned a refused read
    into a successful one — `test_read_verb_refusal_is_not_content` caught it the same day.
    """

    def test_kubectl_missing_binary_still_starts_with_the_error_prefix(self):
        from app.tools.kubectl_tool import run_kubectl
        with patch("subprocess.run", side_effect=FileNotFoundError("kubectl")):
            out = run_kubectl.invoke({"command": "kubectl get pods -n default", "stdin": None})
        assert out.startswith("[Error]")
        assert out.rstrip().endswith("Retrying this tool will not change that.")

    def test_helm_missing_binary_still_starts_with_the_error_prefix(self):
        from app.tools.helm_tool import run_helm
        with patch("subprocess.run", side_effect=FileNotFoundError("helm")):
            assert run_helm.invoke({"command": "helm list -n default"}).startswith("[Error]")

    def test_the_aci_read_verb_still_calls_a_missing_binary_a_failure(self):
        # The exact regression, asserted from the consumer's side rather than the producer's.
        from app.tools.aci import read_verbs as rv
        with patch("subprocess.run", side_effect=FileNotFoundError("kubectl")):
            result = rv._run("inspect", "get pods -n prod", "pods in prod")
        assert result.ok is False

    @pytest.mark.parametrize("stderr", [CLUSTER_DOWN, HELM_UNREACHABLE])
    def test_the_marker_is_the_last_line_not_the_first(self, stderr):
        out = kubectl_with_stderr(stderr)
        assert not out.startswith(UNAVAILABLE_MARKER)
        assert UNAVAILABLE_MARKER in out.splitlines()[-1]


class TestTheModelIsToldWhatTheMarkerMeans:
    def test_the_gather_prompt_names_the_exact_marker(self):
        from app.cortex import graph
        assert RETRY_CLAUSE in graph._GATHER_SYSTEM
        assert UNAVAILABLE_MARKER in RETRY_CLAUSE

    def test_the_clause_says_not_to_call_it_again(self):
        assert "Do NOT" in RETRY_CLAUSE or "do NOT" in RETRY_CLAUSE

    def test_the_clause_forbids_reading_silence_as_evidence(self):
        # The failure this rule prevents is not just a wasted retry: a tool that cannot answer
        # must not be reported as a tool that found nothing.
        assert "evidence" in RETRY_CLAUSE.lower()
