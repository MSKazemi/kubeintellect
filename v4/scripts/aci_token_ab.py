"""ADR-101 token-budget A/B: flat kubectl vs the ACI-bounded path (v5 spec 01, live).

Runs an identical realistic investigation of the seeded failures BOTH ways against the same
live cluster and measures the LLM-context cost of each tool result. Deterministic — no LLM.
'Tokens' are estimated as ceil(chars/4), the standard English/code heuristic; the point is the
*ratio*, which is estimator-independent.

  Flat path : raw run_kubectl output (what a naive kubectl tool dumps into context)
  ACI path  : the bounded AciResult.render() for the equivalent verb

Run:  KUBECONFIG_PATH=... uv run --project <v4> python scripts/aci_token_ab.py
"""

from __future__ import annotations

import asyncio
import math
import os
import shlex
import subprocess

from app.tools.aci import read_verbs
from app.tools.kubectl_tool import run_kubectl

NS = "demo"


def toks(s: str) -> int:
    return math.ceil(len(s) / 4)


def _raw(cmd: str) -> str:
    """TRUE unbounded kubectl output — what a naive 'dump stdout' tool costs."""
    env = {**os.environ, "KUBECONFIG": os.path.expanduser(os.environ["KUBECONFIG_PATH"])}
    p = subprocess.run(shlex.split(cmd), capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=30)
    return p.stdout or p.stderr or "(no output)"


def _flat(cmd: str) -> str:
    """v4's existing run_kubectl — already crude-capped at 8000 chars, mid-line, no cursor."""
    return run_kubectl.invoke({"command": cmd})


async def main() -> int:
    # A realistic triage of a BUSY namespace (dozens of pods, chatty logs) — the
    # regime the 100-line bound is designed for.
    steps = [
        ("list all pods (busy ns)",
         f"kubectl get pods -n {NS} -o wide",
         lambda: read_verbs.search.ainvoke({"kinds": ["pods"], "namespace": NS})),
        ("list pods all-namespaces",
         "kubectl get pods -A -o wide",
         lambda: read_verbs.search.ainvoke({"kinds": ["pods"], "all_namespaces": True})),
        ("cluster events",
         "kubectl get events -A",
         lambda: read_verbs.search.ainvoke({"kinds": ["events"], "all_namespaces": True})),
        ("inspect crashloop pod",
         f"kubectl describe pod crasher -n {NS}",
         lambda: read_verbs.inspect.ainvoke({"kind": "pod", "name": "crasher", "namespace": NS, "view": "summary"})),
        ("inspect deployment (full)",
         f"kubectl get deployment web -n {NS} -o yaml",
         lambda: read_verbs.inspect.ainvoke({"kind": "deployment", "name": "web", "namespace": NS, "view": "full"})),
        ("chatty pod logs (5000 lines)",
         f"kubectl logs chatty -n {NS}",
         lambda: read_verbs.logs.ainvoke({"namespace": NS, "pod": "chatty", "lines": 100})),
    ]

    raw_total = flat_total = aci_total = 0
    print(f"{'step':<28}{'raw tok':>9}{'v4cap':>8}{'aci':>7}{'vs raw':>8}")
    print("-" * 60)
    for label, cmd, aci_fn in steps:
        raw_out = _raw(cmd)
        flat_out = _flat(cmd)
        aci_out = await aci_fn()
        rt, ft, at = toks(raw_out), toks(flat_out), toks(aci_out)
        raw_total += rt
        flat_total += ft
        aci_total += at
        pct = (1 - at / rt) * 100 if rt else 0.0
        print(f"{label:<28}{rt:>9}{ft:>8}{at:>7}{pct:>7.0f}%")

    print("-" * 60)
    vs_raw = (1 - aci_total / raw_total) * 100 if raw_total else 0.0
    vs_cap = (1 - aci_total / flat_total) * 100 if flat_total else 0.0
    print(f"{'TOTAL':<28}{raw_total:>9}{flat_total:>8}{aci_total:>7}{vs_raw:>7.0f}%")
    print(f"\nraw kubectl (naive dump)   : ~{raw_total} tok")
    print(f"v4 run_kubectl (8k cap)    : ~{flat_total} tok")
    print(f"ACI bounded path           : ~{aci_total} tok")
    print(f"\nACI vs raw dump : {vs_raw:.0f}% reduction ({raw_total - aci_total} tok saved)")
    print(f"ACI vs v4 8k-cap: {vs_cap:.0f}% reduction ({flat_total - aci_total} tok saved)")
    print("Note: ACI's win over v4's cap is also QUALITATIVE — line-boundary cut + "
          "managedFields-strip + explicit pagination cursor vs mid-line 8k chop.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
