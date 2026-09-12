"""OpsMemBench live driver — fault-inject + real snapshot capture (v5 spec 03, live).

Ties the deterministic spec-03 core (HarnessSpec / snapshot_manifest_hash / GradeArtifact /
separability gate) to REAL live-cluster state on Kind:

  1. capture a control-plane snapshot from the live cluster (pods/events/deployments as JSON) and
     freeze it into content-addressed layers,
  2. prove record/replay integrity: hashing the SAME frozen snapshot twice yields the SAME manifest
     hash (the property that makes the "reproducible replay" pillar survive live-cluster flakiness),
  3. prove state-sensitivity: injecting a new fault and re-capturing changes the manifest hash,
  4. build a real GradeArtifact over the live snapshot with a complete HarnessSpec,
  5. exercise the separability gate on a sample effect.

HONEST BOUNDARY: the full loop (LLM-driven SUT investigates the injected fault → predictions JSON →
M1–M5 grades) needs the agent runtime + a live LLM SUT and is NOT run here. This validates the
capture/replay/grading *spine* against real cluster state.

Run: KUBECONFIG_PATH=... uv run --project <v4> python scripts/opsmembench_live_probe.py
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess

from app.eval.opsmembench import (
    HarnessSpec,
    ScoreCard,
    build_grade,
    decompose,
    snapshot_manifest_hash,
)

NS = "demo"
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def _kubectl(cmd: str) -> str:
    env = {**os.environ, "KUBECONFIG": os.path.expanduser(os.environ["KUBECONFIG_PATH"])}
    return subprocess.run(shlex.split(cmd), capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=30).stdout


def capture_snapshot() -> dict[str, str]:
    """Freeze live control-plane state into content-addressed layers (digest per resource kind)."""
    layers = {}
    for kind in ("pods", "events", "deployments"):
        raw = _kubectl(f"kubectl get {kind} -n {NS} -o json")
        layers[kind] = hashlib.sha256(raw.encode()).hexdigest()
    return layers


def main() -> int:
    # 1. capture a real snapshot
    snap = capture_snapshot()
    check("captured 3 live control-plane layers", len(snap) == 3, str(list(snap)))
    h1 = snapshot_manifest_hash(snap)

    # 2. record/replay integrity: same frozen snapshot → same manifest hash
    h1_again = snapshot_manifest_hash(dict(snap))
    check("manifest hash is deterministic over a frozen snapshot (replay integrity)", h1 == h1_again,
          h1[:16])

    # 3. state-sensitivity: inject a new fault, re-capture → hash changes
    _kubectl(f"kubectl run probe-fault --image=nginx:no-such-tag-777 -n {NS}")
    import time as _t  # local; only for the settle wait, not in the hash path
    _t.sleep(6)
    snap2 = capture_snapshot()
    h2 = snapshot_manifest_hash(snap2)
    check("injecting a fault changes the manifest hash (captures real state transition)", h1 != h2,
          f"{h1[:12]} → {h2[:12]}")

    # 4. a real GradeArtifact over the live snapshot with a complete HarnessSpec
    harness = HarnessSpec(model="opus-4.8", max_gather_rounds=8, tool_surface="aci-v0",
                          memory_flags=("MEMORY_HYBRID_RETRIEVAL", "MEMORY_SECURITY_HARDENING"),
                          replay_fidelity="live", seed=1)
    # NOTE: placeholder M1/M2 — real values require an LLM SUT (see HONEST BOUNDARY above).
    art = build_grade(ScoreCard(m1_localized=0.0, m2_resolved=0.0, tokens=8587), harness, snap2,
                      notes=["placeholder metrics; SUT-RCA loop pending live LLM"])
    check("GradeArtifact builds + validates over live snapshot", art.manifest_hash == h2)
    check("GradeArtifact carries the full harness disclosure", art.to_dict()["harness"]["replay_fidelity"] == "live")

    # 5. separability gate on a sample effect
    proven = decompose(effect=0.30, harness_variance=0.0004)
    noise = decompose(effect=0.02, harness_variance=0.01)
    check("separability gate: large effect proven", proven.verdict == "proven", proven.detail)
    check("separability gate: sub-noise effect unproven", noise.verdict == "unproven", noise.detail)

    # cleanup the extra fault so the env is reusable
    _kubectl(f"kubectl delete pod probe-fault -n {NS} --ignore-not-found")

    print(f"\nlive manifest (post-fault): {h2}")
    print(f"==== {'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)} ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
