"""Predictive pre-capture (v5 P4, A-CH-20-16) — arm recorders before an imminent predicted death."""
from __future__ import annotations

from app.detectors.models import Finding
from app.sensorium.pre_capture import CRIU_CHECKPOINT, LOG_VERBOSITY, plan_pre_capture


def _finding(severity="predicted", eta=8.0):
    return Finding(playbook="OOMKilled", cluster_id="c", namespace="demo", object_name="pod/web",
                   evidence="mem trending up", severity=severity, eta_minutes=eta)


class TestPlan:
    def test_imminent_prediction_arms_capture(self):
        p = plan_pre_capture(_finding(eta=8.0), eta_threshold_min=15.0)
        assert p is not None and p.target == "pod/web" and p.namespace == "demo"
        assert LOG_VERBOSITY in p.actions and CRIU_CHECKPOINT in p.actions
        assert "before death" in p.reason

    def test_far_off_prediction_skipped(self):
        assert plan_pre_capture(_finding(eta=60.0), eta_threshold_min=15.0) is None

    def test_realized_finding_too_late(self):
        assert plan_pre_capture(_finding(severity="warning", eta=5.0)) is None

    def test_no_eta_skipped(self):
        assert plan_pre_capture(_finding(eta=None)) is None

    def test_custom_actions(self):
        p = plan_pre_capture(_finding(eta=5.0), actions=[LOG_VERBOSITY])
        assert p.actions == [LOG_VERBOSITY]
