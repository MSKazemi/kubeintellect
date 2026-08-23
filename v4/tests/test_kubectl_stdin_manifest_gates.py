"""`kubectl apply -f -` puts the resource in `kind:` and the namespace in `metadata.namespace:`.

Every protected-access check parsed argv. `kubectl apply -f -` names neither a resource nor a
namespace there — both live in the YAML on stdin — so the checks saw an empty command and let it
through to an approval prompt. The identical intent written on the command line was refused
outright. Measured 2026-08-20 for `operator` and `admin`:

    kubectl apply -f -  (Pod, metadata.namespace: kube-system)   → approval prompt
    kubectl apply -f - -n kube-system                            → [Protected]
    kubectl apply -f -  (kind: Secret)                           → approval prompt
    kubectl create secret generic mine --from-literal=a=b        → [Protected]

The sharp edge is that this is not an obscure form: `_REJECTED_VERBS` refuses `kubectl edit` with
a message that *recommends* `kubectl apply -f -` with stdin as the way to do it instead. The
documented workaround was the bypass.

An approval prompt is not equivalent to a refusal. The namespace and resource blocks exist
precisely because that decision is not the operator's to make, and here the thing being approved
is a wall of YAML in which one `namespace:` line is easy to miss.

Scope is deliberate and tested in both directions: the manifest's `kind` and
`metadata.namespace`, nothing deeper. A Pod that mounts a Secret in its own namespace is what
Pods are for and must still apply.
"""

from __future__ import annotations

import textwrap
from unittest.mock import MagicMock, patch

import pytest

from app.tools.kubectl_tool import _manifest_kinds, _manifest_namespaces, run_kubectl


def _yaml(text: str) -> str:
    return textwrap.dedent(text).lstrip()


POD_IN_KUBE_SYSTEM = _yaml("""
    apiVersion: v1
    kind: Pod
    metadata:
      name: helper
      namespace: kube-system
    spec:
      containers:
        - name: c
          image: busybox
    """)

SECRET = _yaml("""
    apiVersion: v1
    kind: Secret
    metadata:
      name: mine
      namespace: prod
    stringData:
      token: abc
    """)

SERVICE_ACCOUNT_IN_KUBE_SYSTEM = _yaml("""
    apiVersion: v1
    kind: ServiceAccount
    metadata:
      name: escalate
      namespace: kube-system
    """)

DEPLOYMENT_IN_PROD = _yaml("""
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: api
      namespace: prod
    spec:
      replicas: 2
    """)

CONFIGMAP_NO_NAMESPACE = _yaml("""
    apiVersion: v1
    kind: ConfigMap
    metadata:
      name: cfg
    data:
      a: b
    """)

SECRETSTORE_CRD = _yaml("""
    apiVersion: external-secrets.io/v1beta1
    kind: SecretStore
    metadata:
      name: vault
      namespace: prod
    """)

LIST_HIDING_A_SECRET = _yaml("""
    apiVersion: v1
    kind: List
    items:
      - apiVersion: v1
        kind: ConfigMap
        metadata: {name: cfg, namespace: prod}
      - apiVersion: v1
        kind: Secret
        metadata: {name: sneaky, namespace: prod}
    """)

MULTIDOC_SECOND_IS_PROTECTED = _yaml("""
    apiVersion: v1
    kind: ConfigMap
    metadata: {name: cfg, namespace: prod}
    ---
    apiVersion: v1
    kind: Pod
    metadata: {name: p, namespace: monitoring}
    spec:
      containers: [{name: c, image: busybox}]
    """)


def _invoke(command: str, stdin: str | None, role: str, **cfg) -> tuple[str, bool, bool]:
    configurable = {"user_role": role, "thread_id": "t", **cfg}
    with patch("subprocess.run") as run:
        proc = MagicMock()
        proc.stdout, proc.stderr, proc.returncode = "<DONE>", "", 0
        run.return_value = proc
        try:
            out, hitl = str(run_kubectl.invoke({"command": command, "stdin": stdin},
                            config={"configurable": configurable})), False
        except KeyError as exc:
            if "__pregel_scratchpad" not in str(exc):
                raise
            out, hitl = "", True
        return out, run.called, hitl


class TestAManifestIsRefusedForWhatItActuallyTargets:
    @pytest.mark.parametrize("role", ["operator", "admin"])
    @pytest.mark.parametrize("verb", ["apply", "create"])
    def test_a_protected_namespace_in_metadata_is_blocked_not_prompted(self, role, verb):
        out, ran, hitl = _invoke(f"kubectl {verb} -f -", POD_IN_KUBE_SYSTEM, role)
        assert "[Protected]" in out, f"{verb} as {role} was only gated: {out[:120]!r} hitl={hitl}"
        assert "kube-system" in out
        assert not ran and not hitl

    @pytest.mark.parametrize("role", ["operator", "admin", "superadmin"])
    def test_a_protected_kind_is_blocked_for_every_role(self, role):
        """The resource block holds for superadmin too — that is its documented invariant."""
        out, ran, hitl = _invoke("kubectl apply -f -", SECRET, role)
        assert "[Protected]" in out, f"as {role}: {out[:120]!r} hitl={hitl}"
        assert not ran and not hitl

    @pytest.mark.parametrize("role", ["operator", "admin"])
    def test_a_serviceaccount_manifest_is_blocked(self, role):
        out, _ran, _hitl = _invoke("kubectl apply -f -", SERVICE_ACCOUNT_IN_KUBE_SYSTEM, role)
        assert "[Protected]" in out

    def test_a_protected_kind_hidden_in_a_list_is_found(self):
        out, ran, hitl = _invoke("kubectl apply -f -", LIST_HIDING_A_SECRET, "admin")
        assert "[Protected]" in out, f"kind: List items were not walked: {out[:120]!r} hitl={hitl}"
        assert not ran and not hitl

    def test_a_protected_namespace_in_the_second_document_is_found(self):
        out, ran, hitl = _invoke("kubectl apply -f -", MULTIDOC_SECOND_IS_PROTECTED, "admin")
        assert "[Protected]" in out and "monitoring" in out, f"{out[:120]!r} hitl={hitl}"
        assert not ran and not hitl


class TestTheManifestFormMatchesTheCommandLineForm:
    """The two spellings of one intent must produce the same answer, which is the whole point."""

    @pytest.mark.parametrize("role", ["operator", "admin"])
    def test_namespace_in_yaml_matches_namespace_on_the_command_line(self, role):
        via_yaml, _, _ = _invoke("kubectl apply -f -", POD_IN_KUBE_SYSTEM, role)
        via_flag, _, _ = _invoke("kubectl apply -f - -n kube-system", DEPLOYMENT_IN_PROD, role)
        assert ("[Protected]" in via_yaml) == ("[Protected]" in via_flag) is True


class TestOrdinaryManifestsStillApply:
    @pytest.mark.parametrize("stdin,label", [
        (DEPLOYMENT_IN_PROD, "an ordinary Deployment in an ordinary namespace"),
        (CONFIGMAP_NO_NAMESPACE, "a manifest that names no namespace at all"),
        (SECRETSTORE_CRD, "a CRD whose kind merely starts with 'Secret'"),
    ])
    def test_it_runs(self, stdin, label):
        out, ran, _hitl = _invoke("kubectl apply -f -", stdin, "admin", hitl_bypass=True)
        assert ran, f"over-blocked {label}: {out[:120]!r}"

    def test_superadmin_keeps_its_namespace_bypass_for_manifests(self):
        stdin = CONFIGMAP_NO_NAMESPACE.replace("name: cfg", "name: cfg\n  namespace: kube-system")
        out, ran, _hitl = _invoke("kubectl apply -f -", stdin, "superadmin", hitl_bypass=True)
        assert ran, f"superadmin's documented bypass was removed: {out[:120]!r}"

    def test_a_command_with_no_stdin_is_unaffected(self):
        out, ran, _hitl = _invoke("kubectl get pods -n prod", None, "readonly")
        assert ran, out[:120]


class TestTheParsersThemselves:
    def test_kinds_and_namespaces_are_extracted(self):
        assert _manifest_kinds(POD_IN_KUBE_SYSTEM) == {"Pod"}
        assert _manifest_namespaces(POD_IN_KUBE_SYSTEM) == {"kube-system"}

    def test_list_items_are_walked(self):
        assert _manifest_kinds(LIST_HIDING_A_SECRET) >= {"List", "ConfigMap", "Secret"}

    @pytest.mark.parametrize("stdin", [None, "", "not: [a, mapping", "- just\n- a\n- list\n"])
    def test_unparseable_or_unexpected_stdin_yields_nothing_rather_than_raising(self, stdin):
        assert _manifest_kinds(stdin) == set()
        assert _manifest_namespaces(stdin) == set()

    def test_a_namespace_is_lowercased_for_comparison(self):
        assert _manifest_namespaces("kind: Pod\nmetadata: {namespace: KUBE-SYSTEM}\n") == {
            "kube-system"
        }
