"""Agentic/GPU metric collector (v5 P4) — wires predicates to the metric source."""
from __future__ import annotations

from app.detectors.agentic_gpu_collector import collect_and_detect


def _scalar(values):
    """values: {promql-substring: value} → a scalar fn matching by substring."""
    def fn(q):
        for k, v in values.items():
            if k in q:
                return float(v)
        return 0.0
    return fn


class TestCollect:
    def test_healthy_metrics_no_hits(self):
        assert collect_and_detect(scalar=_scalar({"tool_calls": 10, "cost": 0.1})) == []

    def test_missing_metrics_read_zero_no_hit(self):
        # everything absent (0) ⇒ no hits, no error — dormant-but-active
        assert collect_and_detect(scalar=lambda q: 0.0) == []

    def test_agent_runaway_detected(self):
        hits = collect_and_detect(scalar=_scalar({"tool_calls_total": 300}), rate_cap=60)
        assert any(h.kind == "agent-runaway" for h in hits)

    def test_sandbox_escape_critical(self):
        hits = collect_and_detect(scalar=_scalar({"sandbox_escape": 1}))
        assert any(h.kind == "sandbox-escape" and h.severity == "critical" for h in hits)

    def test_gpu_ecc_detected(self):
        hits = collect_and_detect(scalar=_scalar({"ecc_errors": 2}))
        assert any(h.kind == "gpu-unhealthy" for h in hits)

    def test_both_planes_hit(self):
        hits = collect_and_detect(scalar=_scalar({"cost_usd_total": 5, "resourceclaim": 3}), cost_cap=1)
        kinds = {h.kind for h in hits}
        assert "agent-runaway" in kinds and "gpu-unschedulable" in kinds

    def test_unreadable_metrics_are_none_not_zero(self):
        """CHANGED 2026-08-20 (pass 79). This asserted `_default_scalar("anything") == 0.0` and
        called it "swallows errors → 0 → no hit" — which is the defect, written down as a
        feature: with Prometheus unreachable every signal read 0.0, including
        `sandbox_escape_attempts`, and `collect_and_detect()` returned `[]`, i.e. "healthy".
        The seam now answers None for "I could not ask"; 0.0 means the query was answered.
        """
        from app.detectors.agentic_gpu_collector import _default_scalar
        assert _default_scalar("anything") is None
