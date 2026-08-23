"""The guards must recognise the resource, however kubectl lets you spell it.

Pass 51 fixed *where* the resource token sits. This covers *what it may say*. The blocklist
compares literal strings, and kubectl accepts several spellings of the same object — measured
2026-08-20 through the real tool, each of these returned credential data through the block:

    kubectl get sa -n prod                  → ServiceAccounts returned  (official short name)
    kubectl get sa/default -n prod          → returned
    kubectl get serviceaccounts.v1. -n prod → returned  (resource.version.group form)
    kubectl get secrets.v1. -n prod         → returned

Uppercase was already handled. The same question applied to namespaces gave a second gap: a
protected namespace named as the command's *target* rather than via `-n` was not covered, so
`kubectl delete pod x -n kube-system` was refused outright while
`kubectl delete namespace kube-system` merely reached an approval prompt.

These are deliberately over-inclusive: blocking a spelling kubectl might reject costs nothing,
while missing one returns credentials.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.tools.kubectl_tool import _extract_verb, _resource_spellings, _targeted_namespaces, run_kubectl

_ADMIN = {"configurable": {"user_role": "admin", "thread_id": "t"}}


def _invoke(command: str, config: dict | None = None) -> tuple[str, bool, bool]:
    with patch("subprocess.run") as run:
        proc = MagicMock()
        proc.stdout, proc.stderr, proc.returncode = "NAME\ndefault\n<CREDENTIAL DATA>", "", 0
        run.return_value = proc
        try:
            out, hitl = str(run_kubectl.invoke({"command": command, "stdin": None},
                                               config=config or _ADMIN)), False
        except KeyError as exc:
            if "__pregel_scratchpad" not in str(exc):
                raise
            out, hitl = "", True
        return out, run.called, hitl


class TestCredentialSpellings:
    @pytest.mark.parametrize("command", [
        "kubectl get serviceaccounts -n prod",
        "kubectl get serviceaccount default -n prod",
        "kubectl get sa -n prod",
        "kubectl get sa/default -n prod",
        "kubectl -n prod get sa",
        "kubectl get serviceaccounts.v1. -n prod",
        "kubectl get secrets.v1. -n prod",
        "kubectl get secret.v1. -n prod",
        "kubectl get SECRETS -n prod",
        "kubectl describe sa default -n prod",
    ])
    def test_every_spelling_is_blocked(self, command):
        out, ran, _ = _invoke(command)
        assert "[Protected]" in out, f"{command!r} was not blocked: {out[:150]}"
        assert not ran and "CREDENTIAL DATA" not in out

    def test_spellings_helper_covers_short_name_and_group_suffix(self):
        assert "serviceaccounts" in _resource_spellings("sa")
        assert "secrets" in _resource_spellings("secrets.v1.")
        assert _resource_spellings(None) == set()

    @pytest.mark.parametrize("resource", ["secretstores", "secretproviderclasses", "sealedsecrets"])
    def test_unrelated_resources_that_merely_start_the_same_are_not_blocked(self, resource):
        """Over-inclusive on spelling, not on prefix — these are ordinary CRDs."""
        assert not (_resource_spellings(resource) & {"secret", "secrets",
                                                     "serviceaccount", "serviceaccounts"})


class TestProtectedNamespacesNamedAsTheTarget:
    @pytest.mark.parametrize("command", [
        "kubectl delete namespace kube-system",
        "kubectl delete ns kube-system",
        "kubectl get ns kube-system",
        "kubectl describe namespace kubeintellect",
        "kubectl -o yaml get ns kube-system",
    ])
    def test_the_protected_namespace_is_refused_not_merely_prompted(self, command):
        out, ran, hitl = _invoke(command)
        assert "[Protected]" in out, f"{command!r} reached execution or approval: {out[:150]}"
        assert not ran and not hitl, (
            "An approval prompt is not protection: the docs say infrastructure namespaces are "
            "blocked including reads, and `-n kube-system` is refused outright. The syntax "
            "used to name the namespace must not decide which of those is true."
        )

    def test_listing_namespaces_is_still_allowed(self):
        """The list is filtered, not refused — refusing it would break ordinary triage."""
        for command in ("kubectl get namespaces", "kubectl get ns"):
            out, ran, _ = _invoke(command)
            assert "[Protected]" not in out and ran, f"{command!r} should still run"

    def test_an_ordinary_namespace_is_not_over_blocked(self):
        """Deleting a tenant namespace stays a normal HITL-gated operation."""
        out, ran, hitl = _invoke("kubectl delete namespace tenant-a")
        assert hitl and not ran and "[Protected]" not in out

    def test_helper_ignores_a_bare_list(self):
        tokens = "kubectl get namespaces".split()
        assert _targeted_namespaces(_extract_verb(tokens), tokens) == []
