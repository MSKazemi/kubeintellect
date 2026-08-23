"""The protected-namespace guard read one name, in one of the two places kubectl puts it.

`_targeted_namespaces` (was `_targeted_namespace`) exists because a protected namespace can be
the command's positional *target* rather than its `-n` value, and the docs say infrastructure
namespaces are blocked including reads. It found the target by taking the first non-flag token
*after* the resource kind — which is one of the ways kubectl accepts the name, and it accepts
several. Measured 2026-08-20, against the default blocklist:

    kubectl delete namespace kube-system      -> [Protected]   ✅
    kubectl delete ns/kube-system             -> ran           ⚠️  name lives inside the operand
    kubectl delete namespace/kube-system      -> ran           ⚠️
    kubectl get    ns/kube-system             -> ran           ⚠️  an ungated *read*
    kubectl delete ns shop kube-system        -> ran           ⚠️  only "shop" was ever examined

The slash form is the same `resource/name` shorthand `_extract_resource_type` documents and
handles — the guard's own sibling parses it, this one did not. The multi-name form is ordinary
kubectl: `delete` takes as many names as you give it, and the guard looked at the first.

Both reach a *hard refusal*, not a prompt, so this is not "the human still sees it": for the
read there was no gate at all, and for the delete the difference is between refusing and
offering a protected namespace for approval.

Third instance of the pass-51 family, and the same shape as pass 98: a parse the guards depend
on made an assumption about how the command happened to be typed. The remaining
`args.index(verb)` in this function went with it — `_operand_index()` is now the one parse.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.core.config import settings
from app.tools.kubectl_tool import (
    _extract_verb,
    _operand_index,
    _targeted_namespaces,
    run_kubectl,
)

_ADMIN = {"configurable": {"user_role": "admin", "thread_id": "t"}}


def _targets(command: str) -> list[str]:
    args = command.split()
    return _targeted_namespaces(_extract_verb(args), args)


def _invoke(command: str) -> tuple[str, bool, bool]:
    """(output, kubectl-was-called, hit-the-HITL-interrupt)."""
    with patch("subprocess.run") as run:
        proc = MagicMock()
        proc.stdout, proc.stderr, proc.returncode = "NAME\nkube-system\n", "", 0
        run.return_value = proc
        try:
            out, hitl = str(run_kubectl.invoke({"command": command, "stdin": None},
                                               config=_ADMIN)), False
        except KeyError as exc:
            if "__pregel_scratchpad" not in str(exc):
                raise
            out, hitl = "", True
        return out, run.called, hitl


class TestEveryWayKubectlNamesANamespace:
    """The refusal must not depend on which valid spelling the model chose."""

    @pytest.mark.parametrize("command", [
        "kubectl delete namespace kube-system",
        "kubectl delete ns kube-system",
        "kubectl delete ns/kube-system",
        "kubectl delete namespace/kube-system",
        "kubectl delete namespaces/kube-system",
        "kubectl get ns/kube-system",
        "kubectl get namespace/kube-system",
        "kubectl describe ns/monitoring",
        "kubectl delete ns shop kube-system",
        "kubectl delete namespace shop kube-system",
        "kubectl delete ns shop cert-manager other",
        "kubectl delete ns/shop ns/kube-public",
        "kubectl delete ns shop --now kube-node-lease",
        "kubectl -n default delete ns/ingress-nginx",
        "kubectl delete --ignore-not-found ns/kubeintellect",
    ])
    def test_a_protected_namespace_is_refused_however_it_is_written(self, command):
        out, ran, hitl = _invoke(command)
        assert "[Protected]" in out, f"{command!r} was not refused (ran={ran}, hitl={hitl})"
        assert not ran, f"{command!r} reached kubectl"

    @pytest.mark.parametrize("command", [
        "kubectl delete ns/kube-system",
        "kubectl delete ns shop kube-system",
        "kubectl get ns/monitoring",
    ])
    def test_the_refusal_names_the_namespace_it_found(self, command):
        out, _, _ = _invoke(command)
        assert "namespace '" in out and "infrastructure namespace" in out

    def test_a_read_is_refused_not_merely_prompted(self):
        """`get` never reaches HITL, so the guard is the only thing between it and the data."""
        out, ran, hitl = _invoke("kubectl get ns/kube-system")
        assert "[Protected]" in out and not ran and not hitl


class TestOrdinaryNamespacesAreStillOrdinary:

    @pytest.mark.parametrize("command", [
        "kubectl get namespaces",
        "kubectl get ns",
        "kubectl get ns -o wide",
    ])
    def test_listing_is_filtered_not_refused(self, command):
        """A listing runs and is filtered. The withheld-note also starts `[Protected]`, so the
        refusal has to be matched on its own sentence, not on that prefix."""
        out, ran, _ = _invoke(command)
        assert ran, f"{command!r} should still run"
        assert "is not permitted" not in out, f"{command!r} was refused"
        assert "withheld" in out, f"{command!r} should still be filtered"

    def test_a_bare_listing_names_no_target(self):
        assert _targets("kubectl get namespaces") == []
        assert _targets("kubectl get ns -o wide") == []

    def test_a_tenant_namespace_is_not_over_blocked(self):
        out, ran, hitl = _invoke("kubectl delete namespace tenant-a")
        assert hitl and not ran and "[Protected]" not in out

    def test_several_tenant_namespaces_are_not_over_blocked(self):
        out, ran, hitl = _invoke("kubectl delete ns tenant-a tenant-b")
        assert hitl and not ran and "[Protected]" not in out

    def test_a_non_namespace_resource_has_no_positional_target(self):
        assert _targets("kubectl delete pod kube-system") == []
        assert _targets("kubectl get secrets -n prod") == []


class TestTheParseItself:

    @pytest.mark.parametrize(("command", "expected"), [
        ("kubectl delete ns a", ["a"]),
        ("kubectl delete ns a b c", ["a", "b", "c"]),
        ("kubectl delete ns/a", ["a"]),
        ("kubectl delete ns/a b", ["a", "b"]),
        ("kubectl delete ns/a ns/b", ["a", "b"]),
        ("kubectl delete ns a --now", ["a"]),
        ("kubectl delete ns --now a", ["a"]),
        ("kubectl -n x delete ns a", ["a"]),
        ("kubectl delete ns A", ["a"]),
    ])
    def test_it_finds_every_name_and_folds_case(self, command, expected):
        assert _targets(command) == expected

    def test_a_flag_value_is_never_read_as_a_name(self):
        """`-o` takes a value; the value is not a namespace the command targets."""
        assert _targets("kubectl get ns -o json") == []
        assert _targets("kubectl delete ns a -o name") == ["a"]

    def test_the_operand_index_is_the_one_shared_parse(self):
        """Same helper `_operand_after_verb` and `_extract_resource_type` use (pass 98)."""
        args = "kubectl -n prod delete ns/kube-system".split()
        assert args[_operand_index(args)] == "ns/kube-system"

    def test_the_verb_string_appearing_earlier_does_not_move_the_parse(self):
        """The `args.index(verb)` this function used could land on a flag's value."""
        assert _targets("kubectl --context delete delete ns kube-system") == ["kube-system"]


class TestTheGuardStillMatchesTheConfiguredList:

    def test_every_default_blocked_namespace_is_refused_as_a_target(self):
        for ns in sorted(settings.kubectl_blocked_namespaces):
            out, ran, _ = _invoke(f"kubectl delete ns/{ns}")
            assert "[Protected]" in out and not ran, ns

    def test_and_as_a_trailing_positional(self):
        for ns in sorted(settings.kubectl_blocked_namespaces):
            out, ran, _ = _invoke(f"kubectl delete ns tenant-a {ns}")
            assert "[Protected]" in out and not ran, ns
