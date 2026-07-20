"""Transactional oracle-verified mitigation (v5 P3, TNR) — commit / rollback / failure paths."""
from __future__ import annotations

from app.tools.aci.postcondition import PostconditionResult
from app.tools.aci.transactional import (
    APPLY_FAILED,
    COMMITTED,
    ROLLED_BACK,
    VERIFY_FAILED_NO_ROLLBACK,
    execute_transactional,
)


def _met():
    return PostconditionResult(True, 3, 3, "3/3 ready")


def _unmet():
    return PostconditionResult(False, 1, 3, "1/3 ready")


class TestCommit:
    def test_apply_then_verified_commits(self):
        applied = []
        r = execute_transactional("kubectl scale deploy/web --replicas=3", _met,
                                  rollback_command="kubectl scale deploy/web --replicas=2",
                                  apply_fn=lambda c: applied.append(c) or "scaled")
        assert r.status == COMMITTED
        assert applied == ["kubectl scale deploy/web --replicas=3"]   # rollback NOT run


class TestRollback:
    def test_failed_postcondition_rolls_back(self):
        applied = []
        r = execute_transactional("kubectl scale deploy/web --replicas=3", _unmet,
                                  rollback_command="kubectl scale deploy/web --replicas=2",
                                  apply_fn=lambda c: applied.append(c) or "scaled")
        assert r.status == ROLLED_BACK
        assert applied == ["kubectl scale deploy/web --replicas=3",
                           "kubectl scale deploy/web --replicas=2"]   # rollback ran

    def test_no_rollback_command_escalates(self):
        r = execute_transactional("kubectl scale deploy/web --replicas=3", _unmet,
                                  apply_fn=lambda c: "scaled")
        assert r.status == VERIFY_FAILED_NO_ROLLBACK


class TestApplyFailure:
    def test_apply_error_no_verify_no_rollback(self):
        calls = []
        def apply(c):
            calls.append(c)
            return "Error: admission webhook denied the request"
        r = execute_transactional("kubectl apply -f bad.yaml",
                                  lambda: (_ for _ in ()).throw(AssertionError("must not verify")),
                                  rollback_command="kubectl delete -f bad.yaml", apply_fn=apply)
        assert r.status == APPLY_FAILED
        assert calls == ["kubectl apply -f bad.yaml"]   # only the apply was attempted
