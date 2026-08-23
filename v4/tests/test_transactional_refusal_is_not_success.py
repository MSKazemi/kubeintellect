"""A refused mutation is not a successful one — and must never trigger a rollback.

`execute_transactional` promises a mitigation "either commits (postcondition holds) or leaves
the cluster as it was (rolled back)". It decided whether the apply had happened with

    _ERR = ("error", "exit=1", "not found", "forbidden", "invalid")
    _ok(output) = not any(e in output.lower() for e in _ERR)

— a substring scan over prose. Measured 2026-08-20 by driving the **real `run_kubectl`** (these
gates answer before kubectl is ever invoked, so no cluster is involved): every safety gate in the
project returns a string that test read as SUCCESS. The executor then failed the oracle — nothing
had changed, so of course it did — and issued the **rollback command against the live cluster**,
undoing something that was never done.

The other direction is as bad and more likely: `deployment.apps/error-budget-exporter configured`
is a successful apply whose *resource name* contains "error", so the apply was reported failed,
the postcondition never ran, and the change stayed live and unverified.
"""
from __future__ import annotations

import pytest

from app.tools.aci.postcondition import PostconditionResult
from app.tools.aci.transactional import (
    APPLIED,
    APPLY_FAILED,
    APPLY_REFUSED,
    COMMITTED,
    FAILED,
    REFUSED,
    ROLLED_BACK,
    classify_apply,
    execute_transactional,
)
from app.tools.kubectl_tool import run_kubectl

# (label, command, role) — each is refused by a different gate.
REFUSED_BY_A_REAL_GATE = [
    ("readonly key, a write", "delete deployment web -n prod", "readonly"),
    ("operator key, high-risk verb", "delete namespace prod", "operator"),
    ("admin key, infrastructure namespace", "delete deployment api -n kube-system", "admin"),
    ("admin key, cluster-wide mutation", "delete pods --all-namespaces", "admin"),
    ("a verb that needs a terminal", "edit deployment web -n prod", "admin"),
]

# Verbatim kubectl (bitnami/kubectl:latest, 2026-08-20).
REAL_KUBECTL_ERRORS = [
    'error: the path "/nope.yaml" does not exist',
    'error: unable to decode "STDIN": Object \'Kind\' is missing in \'{"apiVersion":"v1"}\'',
    "The connection to the server localhost:8080 was refused - "
    "did you specify the right host or port?",
]


def _refusal(command: str, role: str) -> str:
    return run_kubectl.invoke({"command": command},
                              config={"configurable": {"user_role": role, "hitl_bypass": True}})


def _unmet():
    return PostconditionResult(False, 0, 3, "0/3 ready")


def _met():
    return PostconditionResult(True, 3, 3, "3/3 ready")


class TestEveryGateReadsAsRefusedNotApplied:
    @pytest.mark.parametrize("label,command,role", REFUSED_BY_A_REAL_GATE,
                             ids=[c[0] for c in REFUSED_BY_A_REAL_GATE])
    def test_the_real_refusal_string_classifies_as_refused(self, label, command, role):
        out = _refusal(command, role)
        assert classify_apply(out) == REFUSED, f"{label}: {out.splitlines()[0]}"

    @pytest.mark.parametrize("label,command,role", REFUSED_BY_A_REAL_GATE,
                             ids=[c[0] for c in REFUSED_BY_A_REAL_GATE])
    def test_a_refused_mutation_never_issues_a_rollback(self, label, command, role):
        refusal = _refusal(command, role)
        issued: list[str] = []

        def apply_fn(cmd: str) -> str:
            issued.append(cmd)
            return refusal

        r = execute_transactional(
            "kubectl scale deploy/web --replicas=3",
            lambda: pytest.fail("the oracle must not run for a refused command"),
            rollback_command="kubectl scale deploy/web --replicas=1",
            apply_fn=apply_fn,
        )
        assert r.status == APPLY_REFUSED
        assert issued == ["kubectl scale deploy/web --replicas=3"], "a rollback was issued"

    def test_the_refusal_text_is_kept_for_the_caller(self):
        r = execute_transactional("kubectl scale deploy/web --replicas=3", _unmet,
                                  apply_fn=lambda c: _refusal(*REFUSED_BY_A_REAL_GATE[0][1:]))
        assert "[Permission Denied]" in r.apply_output


class TestKubectlErrorsAreStillFailures:
    @pytest.mark.parametrize("text", REAL_KUBECTL_ERRORS)
    def test_a_real_kubectl_error_classifies_as_failed(self, text):
        assert classify_apply(text) == FAILED

    def test_an_admission_webhook_denial_does_not_verify_or_roll_back(self):
        # The pre-existing contract (tests/test_transactional.py) — kept exactly.
        issued: list[str] = []
        r = execute_transactional(
            "kubectl apply -f bad.yaml",
            lambda: pytest.fail("must not verify"),
            rollback_command="kubectl delete -f bad.yaml",
            apply_fn=lambda c: issued.append(c) or "Error: admission webhook denied the request",
        )
        assert r.status == APPLY_FAILED
        assert issued == ["kubectl apply -f bad.yaml"]

    def test_a_connection_failure_is_not_a_successful_apply(self):
        # Real kubectl text, and it contains none of the old keywords — it was read as success.
        out = REAL_KUBECTL_ERRORS[2]
        assert classify_apply(out) == FAILED
        issued: list[str] = []
        r = execute_transactional("kubectl scale deploy/web --replicas=3", _unmet,
                                  rollback_command="kubectl scale deploy/web --replicas=1",
                                  apply_fn=lambda c: issued.append(c) or out)
        assert r.status == APPLY_FAILED
        assert issued == ["kubectl scale deploy/web --replicas=3"]


class TestASuccessfulApplyIsNotReadAsAnError:
    @pytest.mark.parametrize("output", [
        "deployment.apps/error-budget-exporter configured",
        "deployment.apps/invalid-config-detector scaled",
        "configmap/not-found-handler created",
        "deployment.apps/forbidden-paths-webhook unchanged",
    ])
    def test_a_resource_name_containing_a_keyword_does_not_fail_the_apply(self, output):
        assert classify_apply(output) == APPLIED

    def test_the_oracle_runs_and_the_change_commits(self):
        r = execute_transactional("kubectl apply -f -", _met,
                                  apply_fn=lambda c: "deployment.apps/error-budget-exporter configured")
        assert r.status == COMMITTED
        assert r.postcondition is not None, "the postcondition never ran"

    def test_a_warning_line_does_not_fail_the_apply(self):
        out = ("Warning: resource deployments/web is missing the "
               "kubectl.kubernetes.io/last-applied-configuration annotation\n"
               "deployment.apps/web configured")
        assert classify_apply(out) == APPLIED


class TestTheHappyPathIsUnchanged:
    def test_commit_still_commits(self):
        issued: list[str] = []
        r = execute_transactional("kubectl scale deploy/web --replicas=3", _met,
                                  rollback_command="kubectl scale deploy/web --replicas=2",
                                  apply_fn=lambda c: issued.append(c) or "deployment.apps/web scaled")
        assert r.status == COMMITTED
        assert issued == ["kubectl scale deploy/web --replicas=3"]

    def test_a_genuinely_unmet_oracle_still_rolls_back(self):
        issued: list[str] = []
        r = execute_transactional("kubectl scale deploy/web --replicas=3", _unmet,
                                  rollback_command="kubectl scale deploy/web --replicas=2",
                                  apply_fn=lambda c: issued.append(c) or "deployment.apps/web scaled")
        assert r.status == ROLLED_BACK
        assert issued == ["kubectl scale deploy/web --replicas=3",
                          "kubectl scale deploy/web --replicas=2"]

    def test_an_unrecognised_output_reaches_the_oracle(self):
        # Deliberate: unknown is not "failed". The oracle is the authority on what the cluster
        # looks like; a keyword is not.
        assert classify_apply("something no version of kubectl has ever printed") == APPLIED
