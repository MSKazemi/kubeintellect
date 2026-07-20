"""Live TNR probe (v5 P3) — real apply → oracle verify → rollback, on a kind cluster.

Exercises the transactional executor against the LIVE cluster with a reversible scale, testing both
the commit path and the auto-rollback path, then restores the deployment to its original replicas.
Safe: `web` scale is a versioned-workload (fully reversible). Deterministic; no LLM.

Run on n1:  KUBECONFIG_PATH=~/.kube/kind-aci.conf uv run python scripts/aci_transactional_probe.py
"""
from __future__ import annotations

import os
import sys

from app.core.config import settings
from app.tools.aci.postcondition import PostconditionResult, deployment_ready, parse_ready_column
from app.tools.aci.transactional import COMMITTED, ROLLED_BACK, execute_transactional

FAIL: list[str] = []
NS = "demo"
DEP = "web"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def _kubectl(cmd: str) -> str:
    from app.tools.kubectl_tool import run_kubectl
    return run_kubectl.invoke({"command": cmd})


def _current_replicas() -> int:
    out = _kubectl(f"get deployment {DEP} -n {NS}")
    parsed = parse_ready_column(out, DEP)
    return parsed[1] if parsed else 0


def main() -> int:
    settings.KUBECONFIG_PATH = os.environ.get("KUBECONFIG_PATH", os.path.expanduser("~/.kube/kind-aci.conf"))
    original = _current_replicas()
    print(f"original {DEP} replicas = {original}")
    restore = f"scale deployment/{DEP} -n {NS} --replicas={original}"

    # ── COMMIT path: scale to 3, real oracle confirms readiness ⇒ committed ──
    r1 = execute_transactional(
        f"scale deployment/{DEP} -n {NS} --replicas=3",
        lambda: deployment_ready(DEP, NS),
        rollback_command=restore,
    )
    check("commit path: real scale applied + oracle-verified ⇒ committed",
          r1.status == COMMITTED, f"{r1.status} / {r1.postcondition.detail if r1.postcondition else ''}")

    # ── ROLLBACK path: apply, but a forced-failing oracle ⇒ auto-rollback ──
    r2 = execute_transactional(
        f"scale deployment/{DEP} -n {NS} --replicas=6",
        lambda: PostconditionResult(False, 0, 999, "forced-fail (oracle demands impossible state)"),
        rollback_command=f"scale deployment/{DEP} -n {NS} --replicas=3",
    )
    check("rollback path: failed oracle ⇒ auto-rolled-back", r2.status == ROLLED_BACK, r2.status)
    check("rollback ran the inverse command", "scaled" in r2.rollback_output.lower() or r2.rollback_output != "")

    # verify the cluster reflects the rollback target (desired == 3, not the failed 6)
    after = _current_replicas()
    check("cluster reflects rollback (desired=3, not 6)", after == 3, f"desired now {after}")

    # ── restore original state ──
    _kubectl(restore)
    check("restored original replica count", _current_replicas() == original, f"back to {original}")

    print(f"\n==== {'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)} ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
