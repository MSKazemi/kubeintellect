"""Agentic-workload + GPU-health metric collector (v5 P4 activation, D17/SD-D).

Wires the pure detector predicates (``agentic_gpu``) to their metric source: query Prometheus for
the agent-sandbox / GPU signals, build the signal structs, run the predicates, return any hits. A
standard active detector — dormant until its metrics breach (a missing metric reads 0 ⇒ no hit),
firing the moment the signal appears. The scalar query is injected so this is unit-testable without
Prometheus; the default reads the live PromQL endpoint.
"""

from __future__ import annotations

from typing import Callable, Optional

from app.detectors.agentic_gpu import (
    AgentSignal,
    DetectorHit,
    GpuSignal,
    detect_agent_runaway,
    detect_gpu_unhealthy,
)

# PromQL for the KI-emitted agent/GPU signals (from the OTel spans / a node-exporter side channel).
_Q_TOOL_RATE = "sum(rate(ki_agent_tool_calls_total[1m])) * 60"
_Q_COST_RATE = "sum(rate(ki_agent_cost_usd_total[1m])) * 60"
_Q_ESCAPE = "sum(ki_agent_sandbox_escape_attempts)"
_Q_RC_PENDING = "sum(kube_resourceclaim_status_pending)"
_Q_ALLOC_FAIL = "sum(increase(dra_allocation_failures_total[5m]))"
_Q_ECC = "sum(increase(nvidia_gpu_ecc_errors_total[5m]))"
_Q_GPU_OOM = "sum(increase(nvidia_gpu_oom_events_total[5m]))"

ScalarFn = Callable[[str], float]


def _default_scalar(promql: str) -> float:
    """Read a single scalar from the live PromQL endpoint; 0.0 if absent/unreachable."""
    try:
        from app.tools.prometheus_tool import query_prometheus_range_raw
        results = query_prometheus_range_raw(promql, 0)
        if results and results[0].get("value"):
            return float(results[0]["value"][1])
    except Exception:
        pass
    return 0.0


def collect_and_detect(
    *, scalar: Optional[ScalarFn] = None, rate_cap: float = 60.0, cost_cap: float = 1.0,
) -> list[DetectorHit]:
    """Query the metrics, run the agentic + GPU predicates, return the hits (empty when healthy)."""
    s = scalar or _default_scalar
    hits: list[DetectorHit] = []

    agent = AgentSignal(
        tool_call_rate_per_min=s(_Q_TOOL_RATE),
        cost_usd_per_min=s(_Q_COST_RATE),
        sandbox_escape_attempts=int(s(_Q_ESCAPE)),
    )
    h = detect_agent_runaway(agent, rate_cap=rate_cap, cost_cap=cost_cap)
    if h is not None:
        hits.append(h)

    gpu = GpuSignal(
        resourceclaim_pending=int(s(_Q_RC_PENDING)),
        alloc_failures=int(s(_Q_ALLOC_FAIL)),
        ecc_errors=int(s(_Q_ECC)),
        gpu_oom=int(s(_Q_GPU_OOM)),
    )
    h2 = detect_gpu_unhealthy(gpu)
    if h2 is not None:
        hits.append(h2)

    return hits
