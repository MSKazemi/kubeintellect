"""Live probe for the P3 mutating-verb chokepoint (v5 P3 validation).

Runs the server-side dry-run path against a REAL kind cluster: a valid mutation validates cleanly,
an invalid one is rejected — all WITHOUT changing the cluster. Also exercises the full chokepoint
(authorize → dry-run) and rollback classification. Deterministic; no LLM.

Run on n1:  KUBECONFIG_PATH=~/.kube/kind-aci.conf uv run python scripts/aci_mutating_probe.py
"""
from __future__ import annotations

import os
import sys

from app.autonomy.budget import BudgetDecision, disengage_kill_switch, engage_kill_switch
from app.core.config import settings
from app.tools.aci.mutating import (
    IRREVERSIBLE,
    VERSIONED_WORKLOAD,
    classify_rollback,
    plan_mutation,
    validate_mutation,
)

FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def main() -> int:
    settings.KUBECONFIG_PATH = os.environ.get("KUBECONFIG_PATH", os.path.expanduser("~/.kube/kind-aci.conf"))
    disengage_kill_switch()

    # 1. valid mutation validates server-side (no change to the cluster)
    r = validate_mutation("kubectl scale deployment/web -n demo --replicas=3")
    check("valid scale passes server-side dry-run", r.ok, r.output[:80].replace("\n", " "))

    # 2. invalid mutation (nonexistent target) is rejected by the dry-run
    r2 = validate_mutation("kubectl scale deployment/does-not-exist-xyz -n demo --replicas=3")
    check("nonexistent target fails dry-run", not r2.ok, r2.output[:80].replace("\n", " "))

    # 3. full chokepoint: authorized L4 versioned-workload change → dry-run runs and passes
    proposal, dr = plan_mutation("kubectl scale deployment/web -n demo --replicas=2",
                                 earned_rung="L4", budget=BudgetDecision(True))
    check("chokepoint authorizes + validates an earned L4 change",
          proposal.decision == "auto" and dr is not None and dr.ok, proposal.reason)
    check("scale is classified versioned-workload", proposal.rollback_class == VERSIONED_WORKLOAD)

    # 4. kill switch denies BEFORE any dry-run touches the cluster
    engage_kill_switch()
    proposal2, dr2 = plan_mutation("kubectl scale deployment/web -n demo --replicas=5",
                                   earned_rung="L4")
    check("kill switch denies write without dry-run", proposal2.decision == "deny" and dr2 is None)
    disengage_kill_switch()

    # 5. irreversible op is approve (HITL), never auto — even at L4
    proposal3, _ = plan_mutation("kubectl delete pvc/data -n demo", earned_rung="L4",
                                 budget=BudgetDecision(True))
    check("irreversible delete → approve (never auto)",
          proposal3.decision == "approve" and classify_rollback("kubectl delete pvc/data") == IRREVERSIBLE)

    # confirm the cluster was NOT mutated (web still exists; replica count is whatever it was)
    from app.tools.kubectl_tool import run_kubectl
    pods = run_kubectl.invoke({"command": "get deployment web -n demo"})
    check("cluster unchanged (web deployment still present)", "web" in pods)

    print(f"\n==== {'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)} ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
