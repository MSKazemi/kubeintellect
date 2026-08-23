"""Agentic-workload + GPU-health metric collector (v5 P4 activation, D17/SD-D).

Wires the pure detector predicates (``agentic_gpu``) to their metric source: query Prometheus for
the agent-sandbox / GPU signals, build the signal structs, run the predicates, return any hits. A
standard active detector — dormant until its metrics breach (a missing metric reads 0 ⇒ no hit),
firing the moment the signal appears. The scalar query is injected so this is unit-testable without
Prometheus; the default reads the live PromQL endpoint.
"""

from __future__ import annotations

from collections.abc import Callable

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

# None ⇒ the query could not be answered (unreachable / unconfigured / error). 0.0 ⇒ it was
# answered and the metric is genuinely absent or zero.
ScalarFn = Callable[[str], float | None]

UNAVAILABLE = "metrics-unavailable"


def _default_scalar(promql: str) -> float | None:
    """One scalar from the live PromQL endpoint. None if the query could not be answered."""
    try:
        from app.tools.prometheus_tool import query_prometheus_series
        results, error = query_prometheus_series(promql, 0)
        if error is not None:
            return None
        if results and results[0].get("value"):
            return float(results[0]["value"][1])
    except Exception:
        return None
    return 0.0


def collect_and_detect(
    *, scalar: ScalarFn | None = None, rate_cap: float = 60.0, cost_cap: float = 1.0,
) -> list[DetectorHit]:
    """Query the metrics, run the agentic + GPU predicates, return the hits.

    Empty means healthy **and** readable. If any signal could not be read, the first hit is
    `metrics-unavailable` — the operator is told the detectors are blind rather than shown an
    empty list that looks exactly like a clean bill of health.
    """
    s = scalar or _default_scalar
    hits: list[DetectorHit] = []
    unreadable: list[str] = []

    def read(promql: str) -> float:
        """The signal, or 0.0 with the query recorded as unreadable — never a silent zero."""
        value = s(promql)
        if value is None:
            unreadable.append(promql)
            return 0.0
        return value

    agent = AgentSignal(
        tool_call_rate_per_min=read(_Q_TOOL_RATE),
        cost_usd_per_min=read(_Q_COST_RATE),
        sandbox_escape_attempts=int(read(_Q_ESCAPE)),
    )
    gpu = GpuSignal(
        resourceclaim_pending=int(read(_Q_RC_PENDING)),
        alloc_failures=int(read(_Q_ALLOC_FAIL)),
        ecc_errors=int(read(_Q_ECC)),
        gpu_oom=int(read(_Q_GPU_OOM)),
    )

    if unreadable:
        # Emitted first, because everything after it was decided on signals that may not exist.
        hits.append(DetectorHit(
            kind=UNAVAILABLE,
            severity="warning",
            detail=(
                f"{len(unreadable)} of 7 agent/GPU signals could not be read — these detectors "
                "are blind, not clear. Unread: " + ", ".join(q[:60] for q in unreadable[:3])
                + (" …" if len(unreadable) > 3 else "")
            ),
        ))

    h = detect_agent_runaway(agent, rate_cap=rate_cap, cost_cap=cost_cap)
    if h is not None:
        hits.append(h)
    h2 = detect_gpu_unhealthy(gpu)
    if h2 is not None:
        hits.append(h2)

    return hits
