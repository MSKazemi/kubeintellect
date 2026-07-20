"""Flag-ON cortex integration probe (v5 P2 live validation).

Drives the real P2 investigation path against a LIVE Azure LLM + the Kind cluster:
  gather_llm  → run_fanout → parallel read-only subagents → ACI verbs vs the cluster
  synthesize  → adversarial verify ladder + escalation brief

Exercises what the unit tests can only mock: real LLM tool-calling through the ACI verbs, the
fan-out reconciliation, and the verify/brief tails. Needs AZURE_* creds in .env and a kind
kubeconfig. Deterministic-ish PASS/FAIL markers; prints the produced answer.

Run on n1:
  KUBECONFIG_PATH=~/.kube/kind-aci.conf uv run python scripts/cortex_flagon_probe.py
"""
from __future__ import annotations

import asyncio
import os
import sys

from langchain_core.messages import HumanMessage

from app.agent.state import PlanStep
from app.core.config import settings

FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


async def main() -> int:
    # ── flip the P2 flags ON (default-off in prod) ──
    settings.CORTEX_V5_ENABLED = True
    settings.KI_V5_HARNESS_FANOUT = True
    settings.KI_V5_ACI_READ_VERBS_ENABLED = True
    settings.KI_V5_VERIFY_LADDER = True
    settings.KI_V5_ESCALATION_BRIEFS = True
    settings.KI_V5_RUNBOOK_SKILLS = True
    settings.KI_V5_HARNESS_MAX_SUBAGENTS = 2
    settings.KI_V5_HARNESS_MAX_SUBAGENT_ROUNDS = 2
    settings.KI_V5_HARNESS_SUBAGENT_LARGE_MODEL = True   # small models mis-investigate
    kubeconfig = os.environ.get("KUBECONFIG_PATH", os.path.expanduser("~/.kube/kind-aci.conf"))
    settings.KUBECONFIG_PATH = kubeconfig

    print(f"provider={settings.LLM_PROVIDER} coordinator={settings.AZURE_COORDINATOR_DEPLOYMENT} "
          f"kubeconfig={kubeconfig}")

    from app.cortex import graph as cx

    # Faithful to production: context_fetcher gives the graph a real cluster snapshot. Build one
    # here (a bare probe otherwise starves the fan-out of context — as an earlier run showed).
    from app.tools.kubectl_tool import run_kubectl
    snap = run_kubectl.invoke({"command": "get pods -n demo -o wide"})
    snapshot = snap.content if hasattr(snap, "content") else str(snap)
    check("built a real cluster snapshot", "crasher" in snapshot, snapshot[:80].replace("\n", " "))

    session = "flagon-probe"
    state = {
        "messages": [HumanMessage(content="Investigate why the crasher pod in namespace demo is not healthy.")],
        "session_id": session,
        "user_id": "probe",
        "user_role": "admin",
        "cluster_id": "aci",
        "memory_context": "",
        "cluster_snapshot": snapshot,
        "matched_playbooks": ["CrashLoopBackOff"],
        "investigation_plan": [PlanStep(description="Inspect the crasher pod and its recent logs", status="in_progress")],
        "plan_cursor": 0,
        "gather_rounds": 0,
        "turn_start_index": 1,
        "triage_mode": "investigate",
    }
    cfg = {"configurable": {"thread_id": session}}

    # ── gather via the fan-out body ──
    print("\n--- gather_llm (fan-out) ---")
    gout = await cx.gather_llm(state, cfg)
    msgs = gout.get("messages", [])
    evidence = msgs[0].content if msgs and isinstance(getattr(msgs[0], "content", None), str) else ""
    check("fan-out returned an evidence bundle", bool(evidence.strip()), evidence[:80].replace("\n", " "))
    check("fan-out message has no dangling tool_calls (routes to synthesize)",
          not (getattr(msgs[0], "tool_calls", None) or []) if msgs else False)
    check("evidence references a read-only fan-out", "fan-out" in evidence.lower() or "finding" in evidence.lower())

    # thread the fan-out message into state for synthesis
    state["messages"] = state["messages"] + msgs

    # ── synthesize with verify + brief ──
    print("\n--- synthesize (verify + brief) ---")
    sout = await cx.synthesize(state, cfg)
    ans = sout["messages"][-1].content
    check("synthesis produced an answer", bool(ans.strip()), f"{len(ans)} chars")
    check("escalation brief appended (investigate mode)", "Responder brief" in ans)
    low = ans.lower()
    crash_signal = any(k in low for k in ("crashloop", "restart", "exit code", "back-off", "backoff"))
    false_absence = "not found" in low or "does not exist" in low or "no pods" in low
    check("RCA identifies the real crash (not a false 'not found')", crash_signal and not false_absence)

    print("\n================ ANSWER ================\n")
    print(ans[:2000])
    print("\n=======================================")

    print(f"\n==== {'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)} ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
