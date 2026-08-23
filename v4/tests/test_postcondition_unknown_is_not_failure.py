""""I could not look" is not "the fix did not work" — and it must not trigger a rollback.

`deployment_ready` reads the cluster through `run_kubectl`, which returns a **string** and throws
the exit code away. Anything that is not a `kubectl get` table therefore contains no READY column,
`parse_ready_column` returns None, and the oracle used to answer `met=False,
"deployment 'web' not found in 'prod'"` — the same verdict it gives for a genuinely unhealthy
deployment.

Measured 2026-08-20 against the real `run_kubectl` (these paths answer before kubectl is invoked,
so no cluster is involved):

  read of a protected namespace   → `[Protected] Access to namespace 'kube-system' is not permitted…`
  kubectl missing from PATH       → `[Error] kubectl is not installed or not found in PATH…`

Both produced `met=False, detail="deployment 'web' not found in 'prod'"` — a health verdict about
a namespace the oracle never looked at. `execute_transactional` reads `met=False` as a failed
mitigation and runs the rollback command, so an instrument outage became a live mutation against a
cluster we had just been told we cannot read.
"""
from __future__ import annotations

import pytest

from app.tools.aci.postcondition import PostconditionResult, deployment_ready
from app.tools.aci.transactional import (
    ROLLED_BACK,
    VERIFY_INCONCLUSIVE,
    execute_transactional,
)
from app.tools.kubectl_tool import run_kubectl

# A real `kubectl get deployment` table, in the format `parse_ready_column` documents.
GET_TABLE = (
    "NAME   READY   UP-TO-DATE   AVAILABLE   AGE\n"
    "web    3/3     3            3           40h\n"
    "api    1/2     2            1           2h\n"
)

# Verbatim kubectl (bitnami/kubectl:latest, 2026-08-20) — a cluster that cannot be reached.
UNREACHABLE = ("The connection to the server localhost:8080 was refused - "
               "did you specify the right host or port?")


def _real_run_kubectl(command: str, role: str = "admin") -> str:
    return run_kubectl.invoke({"command": command},
                              config={"configurable": {"user_role": role}})


class TestABlindOracleSaysSoInsteadOfGuessing:
    def test_a_refused_read_is_not_a_health_verdict(self):
        refusal = _real_run_kubectl("get deployment web -n kube-system")
        assert refusal.startswith("[Protected]"), refusal[:120]
        r = deployment_ready("web", "prod", _runner=lambda c: refusal)
        assert r.evaluated is False
        assert "could not read" in r.detail
        # and it does not claim anything about the namespace it never looked at
        assert "not found in 'prod'" not in r.detail

    def test_a_missing_kubectl_binary_is_not_a_health_verdict(self):
        # Reproducible on any machine without kubectl on PATH, including this one.
        out = _real_run_kubectl("get deployment web -n prod")
        if not out.startswith("[Error] kubectl is not installed"):
            pytest.skip("kubectl is installed here; this path needs a machine without it")
        r = deployment_ready("web", "prod", _runner=lambda c: out)
        assert r.evaluated is False

    def test_an_unreachable_cluster_is_not_a_health_verdict(self):
        r = deployment_ready("web", "prod", _runner=lambda c: UNREACHABLE)
        assert r.evaluated is False
        assert "connection to the server" in r.detail

    def test_no_output_at_all_is_not_a_health_verdict(self):
        # run_kubectl's own placeholder when a command produced neither stdout nor stderr.
        r = deployment_ready("web", "prod", _runner=lambda c: "(no output)")
        assert r.evaluated is False

    def test_a_raised_exception_is_not_a_health_verdict(self):
        def boom(c):
            raise RuntimeError("no cluster")
        r = deployment_ready("web", "prod", _runner=boom)
        assert r.evaluated is False and "read error" in r.detail


class TestARealReadStillProducesARealVerdict:
    def test_ready_is_still_met(self):
        r = deployment_ready("web", "demo", _runner=lambda c: GET_TABLE)
        assert (r.met, r.evaluated, r.ready, r.desired) == (True, True, 3, 3)

    def test_partially_ready_is_still_not_met(self):
        r = deployment_ready("api", "demo", _runner=lambda c: GET_TABLE)
        assert (r.met, r.evaluated) == (False, True)

    def test_a_successful_read_that_lacks_the_row_is_a_real_observation(self):
        # The read worked; the deployment is genuinely not in the table. That is evidence.
        r = deployment_ready("ghost", "demo", _runner=lambda c: GET_TABLE)
        assert r.evaluated is True and r.met is False and "not found" in r.detail


class TestTheExecutorEscalatesInsteadOfRollingBack:
    def test_an_unevaluated_oracle_does_not_trigger_a_rollback(self):
        issued: list[str] = []
        refusal = _real_run_kubectl("get deployment web -n kube-system")
        r = execute_transactional(
            "kubectl scale deploy/web --replicas=3",
            lambda: deployment_ready("web", "prod", _runner=lambda c: refusal),
            rollback_command="kubectl scale deploy/web --replicas=1",
            apply_fn=lambda c: issued.append(c) or "deployment.apps/web scaled",
        )
        assert r.status == VERIFY_INCONCLUSIVE
        assert issued == ["kubectl scale deploy/web --replicas=3"], "a rollback was issued"
        assert r.postcondition is not None and r.postcondition.evaluated is False

    def test_a_genuinely_unmet_oracle_still_rolls_back(self):
        issued: list[str] = []
        r = execute_transactional(
            "kubectl scale deploy/web --replicas=3",
            lambda: deployment_ready("api", "demo", _runner=lambda c: GET_TABLE),
            rollback_command="kubectl scale deploy/web --replicas=1",
            apply_fn=lambda c: issued.append(c) or "deployment.apps/web scaled",
        )
        assert r.status == ROLLED_BACK
        assert issued == ["kubectl scale deploy/web --replicas=3",
                          "kubectl scale deploy/web --replicas=1"]

    def test_the_default_result_is_evaluated_so_existing_oracles_are_unaffected(self):
        # Any postcondition written before `evaluated` existed keeps its meaning.
        assert PostconditionResult(False, 0, 3, "0/3 ready").evaluated is True
