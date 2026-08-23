"""Which cluster the command talks to, and as whom, are not the caller's to choose.

Pass 89 of the standing audit (T38), reached by re-asking pass 88's question — *is this check a
deny-list of the cases someone thought of?* — of the kubectl **flag** surface, where the answer
turned out to be that there was no check at all.

Every gate in `kubectl_tool` reasons about *what is being asked*: the verb, the resource, the
namespace, the role. None of them looked at *where the answer comes from* or *under whose
identity*. Measured 2026-08-20 by capturing the argv that reaches `subprocess.run`, all eight of
these executed byte-for-byte, none refused, on the plain read path with no role required:

    kubectl get pods --as=system:masters -A
    kubectl get pods --as system:admin -A
    kubectl get pods --as-group=system:masters -A
    kubectl get pods --server=http://attacker.example.com:8080 -A
    kubectl get pods --kubeconfig=/tmp/other.conf -A
    kubectl get pods --token=eyJhbGciOiJSUzI1NiJ9.SECRET -A
    kubectl get pods --insecure-skip-tls-verify -A
    kubectl get pods --context=prod-admin -A

`run_helm` had the same hole in Helm's spelling (`--kube-as-user`, `--kube-apiserver`,
`--kube-token`, `--kube-context`, `--kube-insecure-skip-tls-verify`, `--kubeconfig`).

**The two halves have different severities and both are stated honestly.**

*Impersonation* (`--as…`) still needs the ServiceAccount to hold `impersonate`, which the shipped
chart does not grant — so it failed closed **at the API server**, not here. That is defence by
someone else's configuration, and the chart offers `rbac.clusterAdmin: true`, under which it
would have worked. `docs/security.md`'s attack-surface table meanwhile claims SA-token
impersonation is *"Blocked in-app"*.

*Redirection* (`--server`, `--kubeconfig`, `--context`, `--insecure-skip-tls-verify`) needs **no
cluster permission at all**. Nothing in Kubernetes stops it. The response is then whatever the
named endpoint returns, handed to the model as cluster truth — and every namespace filter in this
module runs over it, so `[Protected] N result(s) withheld` would be computed from attacker-
supplied text. The same document already instructs readers to treat returned log lines as
instruction-like, so the induction path is one this project has written down as real.

Refused rather than stripped: silently dropping a flag answers a different question than the one
asked, which is the failure mode pass 84 closed for filtered listings.
"""
from __future__ import annotations

import pytest

from app.tools import helm_tool as ht
from app.tools import kubectl_tool as kt

KUBECTL_OVERRIDES = [
    "--as=system:masters", "--as system:admin", "--as-group=system:masters",
    "--as-uid=0", "--user=admin", "--username=admin", "--password=hunter2",
    "--token=eyJhbGciOiJSUzI1NiJ9.SECRET",
    "--server=http://attacker.example.com:8080", "-s http://attacker.example.com:8080",
    "--kubeconfig=/tmp/other.conf", "--context=prod-admin", "--cluster=other",
    "--insecure-skip-tls-verify", "--certificate-authority=/tmp/ca.crt",
    "--client-certificate=/tmp/c.crt", "--client-key=/tmp/c.key",
    "--tls-server-name=evil",
]
HELM_OVERRIDES = [
    "--kube-as-user system:masters", "--kube-as-group system:masters",
    "--kube-apiserver https://attacker.example.com", "--kube-token eyJSECRET",
    "--kube-context prod-admin", "--kube-insecure-skip-tls-verify",
    "--kube-ca-file /tmp/ca.crt", "--kubeconfig /tmp/other.conf",
]


@pytest.fixture
def ran(monkeypatch):
    """Capture the argv that would reach the binary; empty means nothing ran."""
    seen: dict = {}

    class _Proc:
        returncode = 0
        stdout = "NAMESPACE  NAME\nshop  api-0\n"
        stderr = ""

    def _fake(argv, **kwargs):
        seen["argv"] = argv
        return _Proc()

    monkeypatch.setattr(kt.subprocess, "run", _fake)
    monkeypatch.setattr(ht.subprocess, "run", _fake)
    return seen


# ── 1. kubectl ────────────────────────────────────────────────────────────────

class TestKubectlRefusesAConnectionOverride:
    @pytest.mark.parametrize("flag", KUBECTL_OVERRIDES)
    def test_it_is_refused_and_never_executed(self, ran, flag):
        out = kt.run_kubectl.func(f"kubectl get pods {flag} -A")
        assert "argv" not in ran, f"{flag} reached the binary"
        assert out.startswith("[Protected]")

    @pytest.mark.parametrize("flag", KUBECTL_OVERRIDES)
    def test_the_refusal_names_the_flag(self, ran, flag):
        name = flag.split("=")[0].split(" ")[0]
        assert name in kt.run_kubectl.func(f"kubectl get pods {flag} -A")

    def test_an_unlisted_impersonation_flag_is_caught_by_the_prefix(self, ran):
        """Every kubectl impersonation flag is spelled `--as…`."""
        out = kt.run_kubectl.func("kubectl get pods --as-something-new=x -A")
        assert "argv" not in ran
        assert out.startswith("[Protected]")

    def test_it_is_refused_before_the_verb_is_trusted(self, ran):
        """The gate must not depend on parsing the verb correctly."""
        out = kt.run_kubectl.func("kubectl --as=system:masters get pods -A")
        assert "argv" not in ran
        assert out.startswith("[Protected]")

    def test_a_write_verb_does_not_change_the_answer(self, ran):
        out = kt.run_kubectl.func("kubectl delete pod x --as=system:masters -n shop")
        assert "argv" not in ran
        assert "[Protected]" in out


# ── 2. helm — the same guarantee, the second code path ────────────────────────

class TestHelmRefusesTheSame:
    @pytest.mark.parametrize("flag", HELM_OVERRIDES)
    def test_it_is_refused_and_never_executed(self, ran, flag):
        out = ht.run_helm.func(f"helm list -A {flag}")
        assert "argv" not in ran, f"{flag} reached the binary"
        assert out.startswith("[Protected]")

    def test_an_unlisted_kube_flag_is_caught_by_the_prefix(self, ran):
        out = ht.run_helm.func("helm list -A --kube-something-new=x")
        assert "argv" not in ran
        assert out.startswith("[Protected]")


# ── 3. Ordinary commands are untouched ────────────────────────────────────────

class TestOrdinaryCommandsStillRun:
    @pytest.mark.parametrize("cmd", [
        "kubectl get pods -A",
        "kubectl get pods -n shop -o json",
        "kubectl get pods -o wide --sort-by=.metadata.name",
        "kubectl logs api-0 -n shop --since=1h --tail=50",
        "kubectl get pods -l app=api --field-selector=status.phase=Running -n shop",
        "kubectl describe pod api-0 -n shop",
    ])
    def test_kubectl(self, ran, cmd):
        kt.run_kubectl.func(cmd)
        assert "argv" in ran, f"{cmd} was refused"

    @pytest.mark.parametrize("cmd", [
        "helm list -A",
        "helm status api -n shop",
        "helm get values api -n shop",
        "helm history api -n shop",
    ])
    def test_helm(self, ran, cmd):
        ht.run_helm.func(cmd)
        assert "argv" in ran, f"{cmd} was refused"

    def test_a_resource_named_like_a_flag_value_is_fine(self, ran):
        kt.run_kubectl.func("kubectl get pod as-server-0 -n shop")
        assert "argv" in ran


# ── 4. The sets themselves ────────────────────────────────────────────────────

class TestTheFamilies:
    @pytest.mark.parametrize("flag", ["--as", "--as-group", "--as-uid", "--server", "-s",
                                      "--kubeconfig", "--context", "--cluster", "--user",
                                      "--token", "--username", "--password",
                                      "--insecure-skip-tls-verify", "--certificate-authority",
                                      "--client-certificate", "--client-key",
                                      "--tls-server-name"])
    def test_kubectl_family_membership(self, flag):
        assert kt._connection_flag_in([flag]) == flag

    @pytest.mark.parametrize("flag", ["-n", "--namespace", "-o", "--output", "-l", "--selector",
                                      "--field-selector", "-A", "--all-namespaces",
                                      "--sort-by", "--tail", "--since", "-c", "--previous"])
    def test_query_flags_are_not_in_the_family(self, flag):
        assert kt._connection_flag_in([flag]) is None

    def test_a_bare_word_is_not_a_flag(self):
        assert kt._connection_flag_in(["kubectl", "get", "pods", "server", "as"]) is None

    def test_helm_family_membership(self):
        assert ht._connection_flag_in(["--kube-apiserver"]) == "--kube-apiserver"
        assert ht._connection_flag_in(["--kubeconfig=/x"]) == "--kubeconfig"
        assert ht._connection_flag_in(["-n", "shop", "list"]) is None
