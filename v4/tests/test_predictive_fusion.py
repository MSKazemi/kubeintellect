"""Predictive-detection fusion (v5 P4) — predicted findings launch read-only investigations."""
from __future__ import annotations

from app.detectors.models import Finding
from app.sensorium import predictive_fusion as pf


def _finding(severity="predicted", eta=8.0):
    return Finding(playbook="OOMKilled", cluster_id="cl-1", namespace="demo",
                   object_name="pod/web", evidence="memory trending up",
                   severity=severity, eta_minutes=eta)


class TestPredicates:
    def test_is_prediction(self):
        assert pf.is_prediction(_finding("predicted")) is True
        assert pf.is_prediction(_finding("warning")) is False

    def test_objective_read_only_and_grounded(self):
        obj = pf.objective_for(_finding())
        assert "PREDICTED OOMKilled" in obj and "pod/web" in obj and "ETA ~8m" in obj
        assert "read-only" in obj and "never drives a fix" in obj

    def test_task_is_prediction_scoped(self):
        t = pf.task_for(_finding())
        assert t.kind == "predicted" and t.dedup_key == "predicted/demo/pod/web"


class TestFuse:
    async def test_predicted_launches_investigation(self, mocker):
        mocker.patch.object(pf.settings, "CORTEX_V5_ENABLED", True)
        mocker.patch.object(pf.settings, "KI_V5_PREDICTIVE_FUSION", True)
        async def runner(state, config):
            return {"messages": [state["messages"][0].content]}
        out = await pf.fuse(_finding("predicted"), runner=runner)
        assert out is not None and "PREDICTED OOMKilled" in out["messages"][0]

    async def test_realized_finding_not_fused(self, mocker):
        mocker.patch.object(pf.settings, "CORTEX_V5_ENABLED", True)
        mocker.patch.object(pf.settings, "KI_V5_PREDICTIVE_FUSION", True)
        called = []
        out = await pf.fuse(_finding("warning"), runner=lambda s, c: called.append(1))
        assert out is None and called == []

    async def test_flag_off_not_fused(self, mocker):
        mocker.patch.object(pf.settings, "CORTEX_V5_ENABLED", True)
        mocker.patch.object(pf.settings, "KI_V5_PREDICTIVE_FUSION", False)
        assert await pf.fuse(_finding("predicted"), runner=lambda s, c: 1) is None
