"""Every guard in `run_kubectl` must survive a reordered command line.

`kubectl -n prod delete deployment api` is exactly as valid as
`kubectl delete deployment api -n prod`, and an LLM writes both. Until 2026-08-20 the verb was
read as `tokens[1]`, so the first form parsed its verb as `-n` — a token in no risk set, no
role set and no rejected set. Every gate in `run_kubectl` keys off that one value, so the
consequence was total, measured through the real tool:

    readonly key + "kubectl delete deployment api -n prod"   → [Permission Denied]
    readonly key + "kubectl -n prod delete deployment api"   → EXECUTED
    readonly key + "kubectl --namespace prod delete pvc x"   → EXECUTED
    readonly key + "kubectl -o wide drain node-1"            → EXECUTED
    any role     + "kubectl -n prod get secrets"             → EXECUTED, secrets returned

Six of eleven ordinary ways of writing a Secret read bypassed the block entirely. Nothing in
1093 tests noticed, because every existing test writes the canonical order.

Two defences, tested separately: `_extract_verb` skips flags (the correct parse), and
`_destructive_verbs_in` gates on any destructive verb present regardless of position (the
backstop for a shape nobody anticipated).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.tools.kubectl_tool import (
    _destructive_verbs_in,
    _extract_resource_type,
    _extract_verb,
    run_kubectl,
)

_READONLY = {"configurable": {"user_role": "readonly", "thread_id": "t"}}
_ADMIN = {"configurable": {"user_role": "admin", "thread_id": "t"}}

# Ordinary ways an operator or an LLM writes the same destructive command.
_DESTRUCTIVE_FORMS = [
    "kubectl delete deployment api -n prod",
    "kubectl -n prod delete deployment api",
    "kubectl --namespace prod delete deployment api",
    "kubectl --namespace=prod delete deployment api",
    "kubectl -o wide -n prod delete deployment api",
    # CHANGED-2026-08-20 (pass 89): was `--context staging`, which `run_kubectl` now refuses
    # outright as a connection/identity override — so this row stopped exercising the thing it
    # was written for (a two-token leading flag before the verb) and started passing for an
    # unrelated reason. `--request-timeout` is the same shape and is not a connection flag.
    "kubectl --request-timeout 30s delete pvc data-0 -n prod",
    "kubectl -o wide drain node-1 --ignore-daemonsets",
]

# Ordinary ways of asking for credential material.
_SECRET_FORMS = [
    "kubectl get secrets -n prod",
    "kubectl -n prod get secrets",
    "kubectl get -n prod secrets",
    "kubectl --namespace prod get secrets",
    "kubectl -n prod get secret my-secret -o yaml",
    "kubectl -o yaml get secrets -n prod",
    "kubectl -n prod describe secret my-secret",
    "kubectl --context prod-ctx get serviceaccounts -n prod",
]


def _invoke(command: str, config: dict) -> tuple[str, bool, bool]:
    """Run the real tool with subprocess patched. Returns (output, kubectl_ran, hit_hitl)."""
    with patch("subprocess.run") as run:
        proc = MagicMock()
        proc.stdout, proc.stderr, proc.returncode = "<REAL CLUSTER DATA>", "", 0
        run.return_value = proc
        try:
            out, hitl = str(run_kubectl.invoke({"command": command, "stdin": None},
                                               config=config)), False
        except KeyError as exc:
            # interrupt() needs LangGraph runtime context; reaching it *is* the HITL gate.
            if "__pregel_scratchpad" not in str(exc):
                raise
            out, hitl = "", True
        return out, run.called, hitl


class TestTheVerbIsFoundWhereverItIs:
    @pytest.mark.parametrize("command,expected", [
        ("kubectl get pods", "get"),
        ("kubectl -n prod get pods", "get"),
        ("kubectl --namespace prod delete deployment api", "delete"),
        ("kubectl --namespace=prod delete deployment api", "delete"),
        ("kubectl -o wide -n prod drain node-1", "drain"),
        ("kubectl --context staging --kubeconfig /tmp/kc apply -f -", "apply"),
        ("kubectl -l app=web get pods", "get"),
        ("kubectl", ""),
        ("kubectl -n prod", ""),
    ])
    def test_flags_before_the_verb_are_skipped(self, command, expected):
        assert _extract_verb(command.split()) == expected


class TestTheResourceIsFoundWhereverItIs:
    @pytest.mark.parametrize("command,expected", [
        ("kubectl get secrets -n prod", "secrets"),
        ("kubectl -n prod get secrets", "secrets"),
        ("kubectl get -n prod secrets", "secrets"),
        ("kubectl -o yaml get secret/my-secret", "secret"),
        ("kubectl --namespace=prod describe secret my-secret", "secret"),
        ("kubectl logs pod-1", None),          # not a resource verb
    ])
    def test_flags_around_the_verb_are_skipped(self, command, expected):
        tokens = command.split()
        assert _extract_resource_type(_extract_verb(tokens), tokens) == expected


class TestReadOnlyKeysCannotWriteInAnyForm:
    @pytest.mark.parametrize("command", _DESTRUCTIVE_FORMS)
    def test_a_readonly_key_is_refused(self, command):
        out, ran, hitl = _invoke(command, _READONLY)
        assert "[Permission Denied]" in out, f"{command!r} was not refused: {out[:150]}"
        assert not ran, "kubectl was executed for a read-only key"
        assert not hitl, "a read-only key must be refused outright, never offered an approval"


class TestCredentialReadsAreBlockedInAnyForm:
    @pytest.mark.parametrize("command", _SECRET_FORMS)
    def test_secrets_are_never_returned(self, command):
        out, ran, _ = _invoke(command, _ADMIN)
        assert "[Protected]" in out, f"{command!r} was not blocked: {out[:150]}"
        assert not ran and "REAL CLUSTER DATA" not in out


class TestDestructiveCommandsAlwaysReachTheApprovalGate:
    @pytest.mark.parametrize("command", _DESTRUCTIVE_FORMS)
    def test_an_admin_is_still_asked(self, command):
        out, ran, hitl = _invoke(command, _ADMIN)
        assert hitl, f"{command!r} executed without an approval prompt: {out[:150]}"
        assert not ran


class TestTheBackstopIsPositionIndependentButNotOverEager:
    def test_a_destructive_verb_anywhere_is_seen(self):
        assert _destructive_verbs_in("kubectl -n prod delete deploy api".split()) == {"delete"}

    @pytest.mark.parametrize("command", [
        "kubectl get pods -l app=delete",          # a label value, not a verb
        "kubectl get pods --dry-run=delete",       # a flag value
        "kubectl get deletejobs",                  # a longer word
    ])
    def test_lookalike_tokens_do_not_trip_it(self, command):
        assert _destructive_verbs_in(command.split()) == set(), (
            "Over-eager matching would gate ordinary read commands behind an approval "
            "prompt, which trains operators to approve without reading."
        )


class TestOrdinaryReadsAreUnaffected:
    """The fix must not turn reads into approvals — that is how a gate stops being read."""

    @pytest.mark.parametrize("command", [
        "kubectl get pods -n prod",
        "kubectl -n prod get pods",
        "kubectl describe deployment api -n prod",
        "kubectl -o json get nodes",
        "kubectl logs pod-1 -n prod",
    ])
    def test_reads_run_without_a_prompt(self, command):
        out, ran, hitl = _invoke(command, _READONLY)
        assert not hitl and "[Permission Denied]" not in out and "[Protected]" not in out
        assert ran, f"{command!r} should have executed: {out[:150]}"
