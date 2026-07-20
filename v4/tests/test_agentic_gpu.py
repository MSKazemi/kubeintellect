"""Agentic-workload SRE + GPU-health detectors (v5 P4, D17/SD-D)."""
from __future__ import annotations

from app.detectors.agentic_gpu import (
    AgentSignal,
    GpuSignal,
    detect_agent_runaway,
    detect_gpu_unhealthy,
)


class TestAgentRunaway:
    def test_escape_is_critical_and_wins(self):
        h = detect_agent_runaway(AgentSignal(tool_call_rate_per_min=5, cost_usd_per_min=0.1,
                                             sandbox_escape_attempts=2))
        assert h and h.kind == "sandbox-escape" and h.severity == "critical"

    def test_cost_runaway_critical(self):
        h = detect_agent_runaway(AgentSignal(cost_usd_per_min=5.0), cost_cap=1.0)
        assert h and h.kind == "agent-runaway" and h.severity == "critical"

    def test_rate_runaway_warning(self):
        h = detect_agent_runaway(AgentSignal(tool_call_rate_per_min=200), rate_cap=60)
        assert h and h.kind == "agent-runaway" and h.severity == "warning"

    def test_healthy_agent_no_hit(self):
        assert detect_agent_runaway(AgentSignal(tool_call_rate_per_min=10, cost_usd_per_min=0.2)) is None

    def test_cost_precedes_rate(self):
        h = detect_agent_runaway(AgentSignal(tool_call_rate_per_min=200, cost_usd_per_min=5.0),
                                 rate_cap=60, cost_cap=1.0)
        assert h.severity == "critical"   # cost (critical) reported over rate (warning)


class TestGpuHealth:
    def test_ecc_critical_wins(self):
        h = detect_gpu_unhealthy(GpuSignal(ecc_errors=1, gpu_oom=3, resourceclaim_pending=2))
        assert h and h.kind == "gpu-unhealthy" and h.severity == "critical"

    def test_gpu_oom_warning(self):
        h = detect_gpu_unhealthy(GpuSignal(gpu_oom=2))
        assert h and h.severity == "warning"

    def test_unschedulable(self):
        h = detect_gpu_unhealthy(GpuSignal(resourceclaim_pending=3, alloc_failures=1))
        assert h and h.kind == "gpu-unschedulable"

    def test_healthy_gpu_no_hit(self):
        assert detect_gpu_unhealthy(GpuSignal()) is None
