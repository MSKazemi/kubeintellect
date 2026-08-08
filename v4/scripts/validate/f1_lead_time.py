"""F1 predictive-detection lead-time A/B (ADR-010 kill criterion).

Measures the WARNING LEAD TIME of anticipatory detection: how long before a pod
is OOMKilled does the trend predicate fire a `predicted` finding?

It polls live Prometheus on a loop (no server restart needed):
  - runs the engine's `evaluate_trends` against the test workload → records the
    timestamp of the first `predicted` finding;
  - watches `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}`
    → records the timestamp of the realized OOM.
lead_time = t(realized OOM) - t(predicted finding).

Kill criterion (run over >=20 incidents): lead-time > 0 on >=60% with predicted
precision >= 0.7. This script measures ONE incident; loop it for the full gate.

Prereq: `kubectl apply -f scripts/validate/oom_leak.yaml` first.
Usage:   PROMETHEUS_URL=http://prometheus.local uv run python scripts/validate/f1_lead_time.py
"""
from __future__ import annotations

import asyncio
import os
import time

from app.detectors.engine import DetectorEngine
from app.detectors.models import DetectBlock, TrendPredicate
from app.tools.prometheus_tool import query_prometheus_range_raw

NS = os.environ.get("PREDICTIVE_TEST_NS", "predictive-test")
POLL_SECONDS = 30
MAX_MINUTES = 20

# A trend predicate tuned for the fast test leak (short window; fire early).
_TREND = TrendPredicate(
    metric=(
        f'sum by (namespace, pod) (container_memory_working_set_bytes{{namespace="{NS}",container!=""}})'
        f' / sum by (namespace, pod) (kube_pod_container_resource_limits{{namespace="{NS}",resource="memory"}})'
    ),
    threshold=1.0, window_minutes=5, projection_horizon_minutes=20,
    fire_if_eta_within_minutes=10, direction="rising", min_r2=0.5,
)
_BLOCK = DetectBlock(playbook="OOMKilled", trend_predicates=(_TREND,))


def _oom_count() -> float:
    rows = query_prometheus_range_raw(
        f'count(kube_pod_container_status_last_terminated_reason{{namespace="{NS}",reason="OOMKilled"}} == 1)', 5
    )
    vals = [float(v[1]) for r in rows for v in r.get("values", []) if v[1] not in ("NaN",)]
    return max(vals) if vals else 0.0


async def main() -> None:
    if not os.environ.get("PROMETHEUS_URL"):
        print("Set PROMETHEUS_URL (e.g. http://prometheus.local)")
        return
    engine = DetectorEngine(detectors=(_BLOCK,), cluster_id="predictive-test")
    t_pred: float | None = None
    t_oom: float | None = None
    deadline = time.time() + MAX_MINUTES * 60
    print(f"watching ns={NS} (poll {POLL_SECONDS}s, max {MAX_MINUTES}m)…")
    while time.time() < deadline and t_oom is None:
        now = time.time()
        if t_pred is None:
            fired = await engine.evaluate_trends(now=now)
            for f in fired:
                t_pred = now
                print(f"[{time.strftime('%H:%M:%S')}] PREDICTED OOM for {f.namespace}/{f.object_name} "
                      f"in ~{f.eta_minutes}m")
        if _oom_count() > 0:
            t_oom = now
            print(f"[{time.strftime('%H:%M:%S')}] REALIZED OOMKilled observed")
        if t_oom is None:
            await asyncio.sleep(POLL_SECONDS)

    print("\n=== RESULT ===")
    if t_pred and t_oom:
        lead = (t_oom - t_pred) / 60.0
        print(f"lead time = {lead:.1f} min  → {'PASS (>0)' if lead > 0 else 'FAIL (<=0)'}")
    elif t_pred and not t_oom:
        print("predicted, but no OOM observed within the window (extend MAX_MINUTES or the leak rate)")
    elif t_oom and not t_pred:
        print("OOM occurred but was NOT predicted → lead time <= 0 (kill-criterion miss for this incident)")
    else:
        print("neither predicted nor OOM within window — check the workload is running and has a memory limit")


if __name__ == "__main__":
    asyncio.run(main())
