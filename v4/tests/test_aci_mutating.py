"""ACI mutating-verb chokepoint (v5 P3) — rollback classification + write-authority decision."""
from __future__ import annotations

import pytest

from app.autonomy.budget import BudgetDecision, disengage_kill_switch, engage_kill_switch
from app.tools.aci.mutating import (
    DECLARATIVE_REVERT,
    IRREVERSIBLE,
    VERSIONED_WORKLOAD,
    classify_rollback,
    decide_write,
)


@pytest.fixture(autouse=True)
def reset_kill():
    disengage_kill_switch()
    yield
    disengage_kill_switch()


class TestClassifyRollback:
    @pytest.mark.parametrize("cmd,cls", [
        ("kubectl scale deploy/web --replicas=3", VERSIONED_WORKLOAD),
        ("kubectl set image deploy/web app=nginx:2", VERSIONED_WORKLOAD),
        ("kubectl rollout restart deploy/api", VERSIONED_WORKLOAD),
        ("kubectl apply -f manifest.yaml", DECLARATIVE_REVERT),
        ("kubectl patch deploy/web -p '{...}'", DECLARATIVE_REVERT),
        ("kubectl label pod/x team=a", DECLARATIVE_REVERT),
        ("kubectl delete pod/web", VERSIONED_WORKLOAD),        # controller recreates ⇒ versioned
        ("kubectl delete pvc/data", IRREVERSIBLE),             # data loss
        ("kubectl delete namespace demo", IRREVERSIBLE),       # cascade
        ("kubectl delete statefulset/db", IRREVERSIBLE),
    ])
    def test_classification(self, cmd, cls):
        assert classify_rollback(cmd) == cls

    def test_unknown_verb_fails_closed(self):
        assert classify_rollback("kubectl frobnicate x") == IRREVERSIBLE

    def test_empty_fails_closed(self):
        assert classify_rollback("") == IRREVERSIBLE

    def test_without_kubectl_prefix(self):
        assert classify_rollback("scale deploy/web --replicas=1") == VERSIONED_WORKLOAD


class TestDecideWrite:
    def test_budget_denial_denies(self):
        engage_kill_switch()
        p = decide_write("kubectl scale deploy/web --replicas=3", earned_rung="L4")
        assert p.decision == "deny"

    def test_irreversible_always_approves(self):
        p = decide_write("kubectl delete pvc/data", earned_rung="L4",
                         budget=BudgetDecision(True))
        assert p.decision == "approve" and p.rollback_class == IRREVERSIBLE

    def test_earned_l4_auto(self):
        p = decide_write("kubectl scale deploy/web --replicas=3", earned_rung="L4",
                         budget=BudgetDecision(True))
        assert p.decision == "auto" and p.rollback_class == VERSIONED_WORKLOAD

    def test_below_l4_requires_approval(self):
        p = decide_write("kubectl apply -f m.yaml", earned_rung="L3", budget=BudgetDecision(True))
        assert p.decision == "approve" and "approval required" in p.reason

    def test_denial_precedes_reversibility(self):
        # even an irreversible op is DENIED (not approve) when the budget gate blocks writes
        p = decide_write("kubectl delete pvc/data", budget=BudgetDecision(False, "kill switch"))
        assert p.decision == "deny"


class TestValidateMutation:
    def test_appends_server_dry_run(self):
        from app.tools.aci.mutating import validate_mutation
        seen = {}
        def runner(cmd):
            seen["cmd"] = cmd
            return "deployment.apps/web scaled (server dry run)"
        r = validate_mutation("kubectl scale deploy/web --replicas=3", _runner=runner)
        assert "--dry-run=server" in seen["cmd"] and r.ok is True

    def test_does_not_double_dry_run(self):
        from app.tools.aci.mutating import validate_mutation
        seen = {}
        def runner(cmd):
            seen["cmd"] = cmd
            return "ok"
        validate_mutation("kubectl apply -f m.yaml --dry-run=server", _runner=runner)
        assert seen["cmd"].count("--dry-run=server") == 1

    def test_admission_denial_detected(self):
        from app.tools.aci.mutating import validate_mutation
        r = validate_mutation("kubectl apply -f bad.yaml",
                              _runner=lambda c: "Error: admission webhook denied the request: policy X")
        assert r.ok is False and r.admission_denied is True

    def test_runner_exception_safe(self):
        from app.tools.aci.mutating import validate_mutation
        def boom(c):
            raise RuntimeError("no cluster")
        r = validate_mutation("kubectl scale deploy/web --replicas=1", _runner=boom)
        assert r.ok is False and "dry-run error" in r.output


class TestPlanMutation:
    def test_denied_write_skips_dry_run(self):
        from app.tools.aci.mutating import plan_mutation
        called = []
        proposal, dr = plan_mutation("kubectl scale deploy/web --replicas=3",
                                     budget=BudgetDecision(False, "freeze"),
                                     _runner=lambda c: called.append(c) or "ok")
        assert proposal.decision == "deny" and dr is None and called == []

    def test_authorized_write_runs_dry_run(self):
        from app.tools.aci.mutating import plan_mutation
        proposal, dr = plan_mutation("kubectl scale deploy/web --replicas=3", earned_rung="L4",
                                     budget=BudgetDecision(True), _runner=lambda c: "scaled (dry run)")
        assert proposal.decision == "auto" and dr is not None and dr.ok is True
