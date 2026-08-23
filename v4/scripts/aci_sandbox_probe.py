"""Live capability-sandbox probe (v5 P3) — RBAC enforces the read-only impersonated identity.

Creates a read-only ServiceAccount (bound to the built-in `view` ClusterRole), then verifies via
impersonation that it CAN read but is FORBIDDEN to mutate — proving the second HITL axis (RBAC, not
just app policy) actually bounds the agent. Idempotent RBAC setup; no lasting cluster change.

Run on n1:  KUBECONFIG_PATH=~/.kube/kind-aci.conf uv run python scripts/aci_sandbox_probe.py
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys

from app.core.config import settings
from app.tools.aci.sandbox import READ_ONLY, as_impersonated

FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def _kubectl(argstr: str) -> str:
    """Run kubectl directly (RBAC is the thing under test; run_kubectl's graph-context HITL is not)."""
    env = dict(os.environ, KUBECONFIG=os.environ.get("KUBECONFIG_PATH", os.path.expanduser("~/.kube/kind-aci.conf")))
    args = shlex.split(argstr)
    if args and args[0] == "kubectl":
        args = args[1:]
    r = subprocess.run(["kubectl", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    return (r.stdout + r.stderr).strip()


def main() -> int:
    ns = settings.KI_V5_SANDBOX_SA_NAMESPACE
    sa = settings.KI_V5_SANDBOX_READONLY_SA

    # idempotent RBAC setup: namespace + SA + view binding
    _kubectl(f"create namespace {ns}")
    _kubectl(f"create serviceaccount {sa} -n {ns}")
    _kubectl(f"create clusterrolebinding {sa}-view --clusterrole=view --serviceaccount={ns}:{sa}")

    # the sandbox module builds the impersonation flags; kubectl + RBAC enforce them.
    read = _kubectl(as_impersonated("get pods -n demo", READ_ONLY))
    check("read-only SA can LIST pods", "forbidden" not in read.lower() and ("web" in read or "NAME" in read),
          read[:70].replace("\n", " "))

    mutate = _kubectl(as_impersonated("scale deployment/web -n demo --replicas=1", READ_ONLY))
    check("read-only SA is FORBIDDEN to scale (RBAC bounds the agent)",
          "forbidden" in mutate.lower() or "cannot" in mutate.lower(), mutate[:90].replace("\n", " "))

    print(f"\n==== {'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)} ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
