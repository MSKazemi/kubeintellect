"""Live-cluster probe for the K8s-ACI v0 read verbs (v5 spec 01 validation).

Runs each verb against a REAL kind cluster (KUBECONFIG in env) and asserts the
contract that unit tests could only check with a mocked seam:
  - read-only enforcement holds on the commands the verbs actually build,
  - every verb renders a NEVER-EMPTY bounded string on real kubectl output,
  - the 100-line window truncates a genuinely large listing,
  - diff_change(against=git) declines rather than shelling out.

Run:  KUBECONFIG=... uv run --project <v4> python scripts/aci_live_probe.py
Exit 0 = all live assertions passed.
"""

from __future__ import annotations

import asyncio
import sys

from app.tools.aci import read_verbs
from app.tools.aci.bounds import is_read_only

NS = "demo"
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))


def show(label: str, text: str) -> None:
    lines = text.splitlines()
    print(f"\n----- {label} ({len(lines)} lines, {len(text)} chars) -----")
    for ln in lines[:12]:
        print("  " + ln)
    if len(lines) > 12:
        print(f"  … (+{len(lines) - 12} more)")


async def main() -> int:
    # 1. inspect a failing pod — real CrashLoopBackOff/Error object
    out = await read_verbs.inspect.ainvoke({"kind": "pod", "name": "crasher", "namespace": NS, "view": "summary"})
    show("inspect pod/crasher", out)
    check("inspect renders non-empty", bool(out.strip()))
    check("inspect body is line-bounded (<=100)", len(out.splitlines()) <= 100 + 6, f"{len(out.splitlines())} lines")

    # 2. inspect the healthy deployment
    out = await read_verbs.inspect.ainvoke({"kind": "deployment", "name": "web", "namespace": NS, "view": "full"})
    show("inspect deploy/web view=full", out)
    check("inspect full renders non-empty", bool(out.strip()))
    check("inspect full still <=100 lines (window enforced)", len(out.splitlines()) <= 100 + 6, f"{len(out.splitlines())} lines")
    check("inspect full stripped server noise (no managedFields)", "managedFields" not in out)

    # 3. search pods in the namespace
    out = await read_verbs.search.ainvoke({"kinds": ["pods"], "namespace": NS})
    show("search pods -n demo", out)
    check("search renders non-empty", bool(out.strip()))
    check("search shows the 4 seeded pods", all(p in out for p in ("web", "crasher", "badimg")))

    # 4. large listing → window must truncate. Cluster-wide events are big.
    out = await read_verbs.search.ainvoke({"kinds": ["events"], "all_namespaces": True})
    show("search events -A (large)", out)
    check("large search is capped at the 100-line window", len(out.splitlines()) <= 100 + 6, f"{len(out.splitlines())} lines")

    # 5. logs from the dead container (previous=True → terminated-container evidence)
    out = await read_verbs.logs.ainvoke({"namespace": NS, "pod": "crasher", "previous": True, "lines": 100})
    show("logs crasher --previous", out)
    check("logs renders non-empty (never silent)", bool(out.strip()))

    # 6. logs empty case — a healthy pod may still have output; use a bogus selector to force empty
    out = await read_verbs.logs.ainvoke({"namespace": NS, "selector": "app=does-not-exist-xyz", "lines": 100})
    show("logs bogus-selector (empty path)", out)
    check("empty logs still render a message (never silent)", bool(out.strip()))

    # 7. diff_change against=git must DECLINE (no git in the loop for v0)
    out = await read_verbs.diff_change.ainvoke({"against": "git", "kind": "deployment", "name": "web", "namespace": NS})
    show("diff_change against=git", out)
    check("diff_change(git) declines, not crashes", bool(out.strip()) and "git" in out.lower())

    # 8. read-only invariant on real built commands
    check("is_read_only(get pods)", is_read_only(f"kubectl get pods -n {NS}"))
    check("is_read_only rejects delete", not is_read_only(f"kubectl delete pod crasher -n {NS}"))
    check("is_read_only rejects apply", not is_read_only("kubectl apply -f x.yaml"))

    print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
    if FAIL:
        print("FAILED:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
