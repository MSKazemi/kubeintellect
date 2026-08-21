"""Agentic-workload SRE + GPU-health detectors (v5 P4, D17 / SD-D).

The one incumbent-EMPTY detector surface (per the SOTA sweep): agentic workloads (agent-sandbox
pools, tool-call rate/cost, A2A/MCP call graphs) and the C7 ACI-2026 device types (ResourceClaim /
DeviceClass / Kueue / InferencePool GPU health). These are the detection predicates + world-model
signal types — pure classification over observed metrics, so they are unit-testable without a
cluster; wiring them to the live sensorium watch stream is the follow-up (same as every detector).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DetectorHit:
    kind: str            # e.g. "agent-runaway", "sandbox-escape", "gpu-unhealthy"
    severity: str        # "warning" | "critical"
    detail: str


# ── Agentic-workload SRE ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class AgentSignal:
    tool_call_rate_per_min: float = 0.0
    cost_usd_per_min: float = 0.0
    sandbox_escape_attempts: int = 0


def detect_agent_runaway(
    sig: AgentSignal, *, rate_cap: float = 60.0, cost_cap: float = 1.0,
) -> DetectorHit | None:
    """Flag an agent that is looping / burning spend / probing its sandbox."""
    if sig.sandbox_escape_attempts > 0:
        return DetectorHit("sandbox-escape", "critical",
                           f"{sig.sandbox_escape_attempts} sandbox-escape attempt(s) — contain the agent")
    if sig.cost_usd_per_min > cost_cap:
        return DetectorHit("agent-runaway", "critical",
                           f"spend {sig.cost_usd_per_min:.2f} USD/min > cap {cost_cap:.2f} — likely a loop")
    if sig.tool_call_rate_per_min > rate_cap:
        return DetectorHit("agent-runaway", "warning",
                           f"tool-call rate {sig.tool_call_rate_per_min:.0f}/min > cap {rate_cap:.0f}")
    return None


# ── GPU / device health (ResourceClaim / DeviceClass) ─────────────────────────
@dataclass(frozen=True)
class GpuSignal:
    resourceclaim_pending: int = 0   # ResourceClaims stuck Pending (no allocatable device)
    alloc_failures: int = 0          # DeviceClass allocation failures
    ecc_errors: int = 0              # GPU ECC / Xid errors
    gpu_oom: int = 0                 # GPU out-of-memory events


def detect_gpu_unhealthy(sig: GpuSignal) -> DetectorHit | None:
    """Flag GPU/device-plane trouble that starves agentic/inference workloads."""
    if sig.ecc_errors > 0:
        return DetectorHit("gpu-unhealthy", "critical",
                           f"{sig.ecc_errors} GPU ECC/Xid error(s) — hardware fault, cordon the node")
    if sig.gpu_oom > 0:
        return DetectorHit("gpu-unhealthy", "warning", f"{sig.gpu_oom} GPU OOM event(s)")
    if sig.resourceclaim_pending > 0 or sig.alloc_failures > 0:
        return DetectorHit("gpu-unschedulable", "warning",
                           f"{sig.resourceclaim_pending} pending ResourceClaim(s), "
                           f"{sig.alloc_failures} alloc failure(s) — no allocatable device")
    return None
