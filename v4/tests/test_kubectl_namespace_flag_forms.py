"""The protected-namespace check must see the namespace however kubectl lets you spell the flag.

kubectl parses flags with pflag, which accepts a shorthand's value attached to it, with or
without an `=`. These five are one command:

    -n kube-system   --namespace kube-system   --namespace=kube-system
    -n=kube-system   -nkube-system

`_extract_namespace` read the first three. For the other two it returned None, so
`_check_protected_resources` never ran its namespace comparison at all — the guard did not
decide the namespace was permitted, it never learned there was one. Measured 2026-08-20 through
the real tool:

    kubectl get pods -n kube-system      admin/operator → [Protected]
    kubectl get pods -nkube-system       admin/operator → RAN
    kubectl delete pod x -n kube-system  admin          → [Protected]
    kubectl delete pod x -nkube-system   admin          → approval prompt (approve → it runs)

This is the pass-52 finding one level out: 52 covered how the *resource* may be spelled, this
covers how the *flag carrying the namespace* may be. A guard that compares strings has to be
handed every string the tool it guards would accept.

Over-blocking is tested just as hard — `--no-headers` also begins with a dash and an `n`, and an
unprotected namespace must stay usable in every one of the five forms.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.tools.kubectl_tool import _extract_namespace, run_kubectl

_SPELLINGS = [
    "-n {ns}",
    "--namespace {ns}",
    "--namespace={ns}",
    "-n={ns}",
    "-n{ns}",
]


def _invoke(command: str, role: str) -> tuple[str, bool, bool]:
    with patch("subprocess.run") as run:
        proc = MagicMock()
        proc.stdout, proc.stderr, proc.returncode = "<DONE>", "", 0
        run.return_value = proc
        try:
            out, hitl = str(run_kubectl.invoke({"command": command, "stdin": None},
                            config={"configurable": {"user_role": role, "thread_id": "t"}})), False
        except KeyError as exc:
            if "__pregel_scratchpad" not in str(exc):
                raise
            out, hitl = "", True
        return out, run.called, hitl


class TestEveryFormIsParsed:
    @pytest.mark.parametrize("form", _SPELLINGS)
    def test_the_namespace_is_found(self, form):
        args = f"kubectl get pods {form.format(ns='kube-system')}".split()
        assert _extract_namespace(args) == "kube-system"

    @pytest.mark.parametrize("form", _SPELLINGS)
    def test_it_is_found_before_the_verb_too(self, form):
        """Global flags may precede the verb — the pass-51 ordering, in every spelling."""
        args = f"kubectl {form.format(ns='kube-system')} get pods".split()
        assert _extract_namespace(args) == "kube-system"


class TestAProtectedNamespaceIsRefusedInEveryForm:
    @pytest.mark.parametrize("form", _SPELLINGS)
    @pytest.mark.parametrize("role", ["operator", "admin"])
    def test_a_read_is_refused(self, form, role):
        cmd = f"kubectl get pods {form.format(ns='kube-system')}"
        out, ran, hitl = _invoke(cmd, role)
        assert "[Protected]" in out, f"{cmd!r} as {role} was permitted: {out[:120]}"
        assert not ran and not hitl

    @pytest.mark.parametrize("form", _SPELLINGS)
    def test_a_write_is_refused_outright_not_merely_prompted(self, form):
        """An approval prompt is not the same answer as a refusal — an admin can approve one."""
        cmd = f"kubectl delete pod api-1 {form.format(ns='kube-system')}"
        out, ran, hitl = _invoke(cmd, "admin")
        assert "[Protected]" in out, f"{cmd!r} was only gated, not blocked: {out[:120]}"
        assert not ran and not hitl


class TestSuperadminKeepsItsDocumentedBypass:
    @pytest.mark.parametrize("form", _SPELLINGS)
    def test_reads_still_run(self, form):
        cmd = f"kubectl get pods {form.format(ns='kube-system')}"
        _out, ran, _hitl = _invoke(cmd, "superadmin")
        assert ran, "superadmin's infra-namespace read bypass was removed by this fix"


class TestOrdinaryNamespacesAreUnaffected:
    @pytest.mark.parametrize("form", _SPELLINGS)
    def test_an_unprotected_namespace_still_works(self, form):
        cmd = f"kubectl get pods {form.format(ns='prod')}"
        out, ran, hitl = _invoke(cmd, "readonly")
        assert ran, f"{cmd!r} was blocked: {out[:120]}"
        assert not hitl

    @pytest.mark.parametrize("args,expected", [
        (["kubectl", "get", "pods"], None),
        (["kubectl", "get", "pods", "--no-headers"], None),          # dash + n, not this flag
        (["kubectl", "get", "nodes", "-o", "wide"], None),
        (["kubectl", "get", "pods", "--namespace="], None),          # empty value, not ""
        (["kubectl", "get", "pods", "-n="], None),
        (["kubectl", "get", "pods", "-n"], None),                    # trailing, no value
        (["kubectl", "get", "pods", "-nprod", "-nkube-system"], "prod"),  # first wins, as before
    ])
    def test_nothing_is_invented(self, args, expected):
        assert _extract_namespace(args) == expected

    def test_no_headers_does_not_become_a_namespace_in_the_gate(self):
        out, ran, _hitl = _invoke("kubectl get pods --no-headers", "readonly")
        assert ran, f"--no-headers was misread as a namespace: {out[:120]}"
