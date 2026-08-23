"""One flag between the verb and its target turned off the gate that cannot be turned off.

`_requires_always_confirm` is the only gate in `run_kubectl` that fires **through**
`hitl_bypass`: cascading deletes (namespace / pv / crd) and live workload mutations
(`set image`, `set resources`, `drain`) prompt the human even on an auto-approve session,
because none of them has a rollback path. Its docstring says so — *"there is no way to
silently auto-approve them"*.

It read the target as `args[2]`, which is the operand only when the command is written
verb-first with nothing in between. Measured 2026-08-20:

    kubectl delete namespace shop                     -> always-confirm  ✅
    kubectl delete -n prod namespace shop             -> NOT confirmed   ⚠️  (`-n` sat in args[2])
    kubectl -n prod delete namespace shop             -> NOT confirmed   ⚠️
    kubectl delete --force namespace shop             -> NOT confirmed   ⚠️
    kubectl delete --ignore-not-found pv my-volume    -> NOT confirmed   ⚠️
    kubectl -n prod set image deploy/api api=nginx    -> NOT confirmed   ⚠️
    kubectl set --record image deploy/api api=nginx   -> NOT confirmed   ⚠️

So on an auto-approve session the *natural* way to write a namespace deletion executed with
no prompt at all, while the awkward way stopped and asked. `drain` was never affected — it is
matched on the verb alone.

This is the same positional trap that made `_extract_verb` read `-n` as the verb (fixed pass
51) and `_extract_resource_type` read `prod` as the resource (fixed later). Both siblings were
corrected; this one was missed. All three now share `_operand_after_verb`.
"""
from __future__ import annotations

import pytest

from app.tools.kubectl_tool import (
    _ALWAYS_CONFIRM_DELETE_TARGETS,
    _ALWAYS_CONFIRM_SET_SUBCOMMANDS,
    _blocked_resource_hit,
    _extract_resource_type,
    _extract_verb,
    _operand_after_verb,
    _requires_always_confirm,
)


def _confirms(command: str) -> bool:
    args = command.split()
    return _requires_always_confirm(_extract_verb(args), args)


# Every rendering of the same two intentions. kubectl accepts all of them.
CASCADING_DELETES = [
    "kubectl delete namespace shop",
    "kubectl delete ns shop",
    "kubectl delete namespace/shop",
    "kubectl delete -n prod namespace shop",
    "kubectl -n prod delete namespace shop",
    "kubectl delete --force namespace shop",
    "kubectl delete --grace-period=0 --force ns shop",
    "kubectl delete --ignore-not-found pv my-volume",
    "kubectl -l app=api delete pv my-volume",
    "kubectl delete crd widgets.example.com",
    "kubectl delete --wait=false customresourcedefinition widgets.example.com",
    "kubectl delete persistentvolume my-volume",
]

LIVE_MUTATIONS = [
    "kubectl set image deploy/api api=nginx:1.27",
    "kubectl -n prod set image deploy/api api=nginx:1.27",
    "kubectl set --record image deploy/api api=nginx:1.27",
    "kubectl set resources deploy/api --limits=cpu=200m",
    "kubectl -n prod set resources deploy/api --limits=cpu=200m",
    "kubectl drain node-1",
    "kubectl -n prod drain node-1",
    "kubectl drain --ignore-daemonsets node-1",
]

# Things that must NOT be promoted to always-confirm: they are gated by ordinary HITL, and an
# auto-approve session is entitled to run them without a prompt.
ORDINARY = [
    "kubectl delete pod api-0",
    "kubectl delete -n prod deployment api",
    "kubectl delete --force pod api-0",
    "kubectl set env deploy/api LOG_LEVEL=debug",
    "kubectl -n prod set env deploy/api LOG_LEVEL=debug",
    "kubectl get pods",
    "kubectl -n prod get namespace shop",
    "kubectl describe namespace shop",
    "kubectl patch deployment api -p {}",
]


class TestTheGateSurvivesEveryRendering:

    @pytest.mark.parametrize("command", CASCADING_DELETES)
    def test_a_cascading_delete_always_confirms(self, command):
        assert _confirms(command), (
            "this rendering skipped the one gate that fires through hitl_bypass"
        )

    @pytest.mark.parametrize("command", LIVE_MUTATIONS)
    def test_a_live_workload_mutation_always_confirms(self, command):
        assert _confirms(command)

    @pytest.mark.parametrize("command", ORDINARY)
    def test_an_ordinary_action_is_left_to_normal_hitl(self, command):
        assert not _confirms(command), (
            "promoting this to always-confirm would prompt on an auto-approve session for "
            "something that has a rollback path"
        )

    def test_the_flagged_and_unflagged_forms_agree(self):
        """The property, stated once: a flag is not a decision about blast radius."""
        for command in CASCADING_DELETES + LIVE_MUTATIONS + ORDINARY:
            head, *tail = command.split()
            flagged = " ".join([head, "-n", "prod", *tail])
            assert _confirms(flagged) == _confirms(command), f"a leading -n changed {command!r}"


class TestOneParserForTheOperand:

    @pytest.mark.parametrize("command,expected", [
        ("kubectl delete namespace shop", "namespace"),
        ("kubectl delete -n prod namespace shop", "namespace"),
        ("kubectl -n prod delete namespace shop", "namespace"),
        ("kubectl set --record image deploy/api a=b", "image"),
        ("kubectl get pods", "pods"),
        ("kubectl get", ""),
        ("kubectl -n prod", ""),
        ("kubectl", ""),
    ])
    def test_the_operand_is_found_wherever_it_sits(self, command, expected):
        assert _operand_after_verb(command.split()) == expected

    def test_it_lowercases_like_every_other_gate(self):
        assert _operand_after_verb("kubectl delete NameSpace shop".split()) == "namespace"

    @pytest.mark.parametrize("command", [
        "kubectl get secrets",
        "kubectl -n prod get secrets",
        "kubectl get -n prod secrets",
        "kubectl delete deployment/api",
    ])
    def test_the_resource_parser_still_agrees_with_it(self, command):
        args = command.split()
        resource = _extract_resource_type(_extract_verb(args), args)
        assert resource == _operand_after_verb(args).split("/")[0]

    def test_a_verb_with_no_operand_returns_none_not_empty(self):
        """`_extract_resource_type` promises `None`, and callers test `is None`."""
        assert _extract_resource_type("delete", "kubectl delete".split()) is None
        assert _extract_resource_type("logs", "kubectl logs api-0".split()) is None


class TestTheTargetSetsAreStillTheOnesDocumented:

    def test_every_named_delete_target_confirms(self):
        for target in _ALWAYS_CONFIRM_DELETE_TARGETS:
            assert _confirms(f"kubectl delete -n prod {target} thing"), target

    def test_every_named_set_subcommand_confirms(self):
        for sub in _ALWAYS_CONFIRM_SET_SUBCOMMANDS:
            assert _confirms(f"kubectl -n prod set {sub} deploy/api a=b"), sub


class TestTheOperandDoesNotDependOnWhereTheVerbStringAppears:
    """The third parser had a second positional assumption, one level subtler.

    `_extract_resource_type` did not read a fixed index — it read `args.index(verb) + 1`, the
    *first* place the verb string appears. A flag value earlier in the command that happens to
    equal the verb takes that position, and the parse restarts from the wrong token. Namespace
    and context names are DNS labels, so `get`, `delete` and `patch` are all legal ones.

    Measured 2026-08-20, before the shared helper::

        kubectl --namespace get get secrets        -> resource "get"      (Secret block missed)
        kubectl -n delete delete secret db-creds   -> resource "delete"   (Secret block missed)

    Both now resolve to the real operand, so `_blocked_resource_hit` sees the Secret.
    """

    @pytest.mark.parametrize(("command", "resource"), [
        ("kubectl --namespace get get secrets", "secrets"),
        ("kubectl -n delete delete secret db-creds", "secret"),
        ("kubectl --context delete delete namespace shop", "namespace"),
        ("kubectl -n patch patch deployment api -p {}", "deployment"),
    ])
    def test_a_flag_value_that_spells_the_verb_is_not_the_verb(self, command, resource):
        args = command.split()
        assert _extract_resource_type(_extract_verb(args), args) == resource

    @pytest.mark.parametrize("command", [
        "kubectl --namespace get get secrets",
        "kubectl -n delete delete secret db-creds",
        "kubectl -n get get serviceaccount default",
    ])
    def test_the_secret_block_still_fires_from_such_a_namespace(self, command):
        args = command.split()
        assert _blocked_resource_hit(_extract_verb(args), args) is not None

    def test_the_always_confirm_gate_agrees_there_too(self):
        """The two parsers now share one helper, so neither can drift from the other again."""
        args = "kubectl --context delete delete namespace shop".split()
        assert _requires_always_confirm(_extract_verb(args), args) is True
