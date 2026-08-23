"""Anticipatory / predictive detection — trend predicates + OLS ETA projection (ADR-010).

The detector engine stays zero-token: prediction is a hand-rolled least-squares
slope over range PromQL, never an LLM call.
"""
from __future__ import annotations

import asyncio

from app.detectors import engine as engine_mod
from app.detectors.engine import DetectorEngine, project_eta
from app.detectors.models import Finding, parse_detect_block

OOM_TREND = {
    "trend_predicates": [
        {
            "metric": 'container_memory_working_set_bytes / kube_pod_container_resource_limits',
            "window_minutes": 30,
            "projection_horizon_minutes": 120,
            "threshold": 1.0,
            "fire_if_eta_within_minutes": 30,
            "direction": "rising",
            "min_r2": 0.6,
        }
    ],
}


def _rising_series(ns="default", pod="payments-7d9", start=0.5, per_sec=0.0005, n=11, step=60):
    """A pod whose memory ratio climbs linearly toward its limit."""
    values = [[i * step, str(start + per_sec * i * step)] for i in range(n)]
    return {"metric": {"namespace": ns, "pod": pod}, "values": values}


class TestProjectEta:
    def test_linear_rising_crosses_on_schedule(self):
        # value(t) = 0.5 + 0.0005*t ; at last sample t=600 → 0.8 ; to reach 1.0 → 400s
        samples = [(i * 60, 0.5 + 0.0005 * i * 60) for i in range(11)]
        eta, r2 = project_eta(samples, threshold=1.0, direction="rising", min_r2=0.6)
        assert eta is not None
        assert abs(eta - 400.0) < 1.0
        assert r2 > 0.99

    def test_flat_series_does_not_project(self):
        samples = [(i * 60, 0.5) for i in range(11)]
        eta, _r2 = project_eta(samples, threshold=1.0, direction="rising", min_r2=0.6)
        assert eta is None

    def test_noisy_below_min_r2_does_not_project(self):
        samples = [(0, 0.5), (60, 0.9), (120, 0.4), (180, 0.95), (240, 0.45)]
        eta, _r2 = project_eta(samples, threshold=1.0, direction="rising", min_r2=0.6)
        assert eta is None

    def test_falling_series_toward_floor(self):
        # disk free falling: 1.0 → 0.4 over 600s ; to reach 0.1 floor
        samples = [(i * 60, 1.0 - 0.001 * i * 60) for i in range(11)]
        eta, _r2 = project_eta(samples, threshold=0.1, direction="falling", min_r2=0.6)
        assert eta is not None and eta > 0

    def test_rising_away_from_threshold_does_not_fire(self):
        # already above threshold → no future crossing
        samples = [(i * 60, 1.2 + 0.0005 * i * 60) for i in range(11)]
        eta, _ = project_eta(samples, threshold=1.0, direction="rising", min_r2=0.6)
        assert eta is None


class TestTrendParsing:
    def test_trend_predicate_compiles(self):
        block = parse_detect_block("OOMKilled", OOM_TREND)
        assert block is not None
        assert len(block.trend_predicates) == 1
        tp = block.trend_predicates[0]
        assert tp.threshold == 1.0
        assert tp.direction == "rising"
        assert tp.fire_if_eta_within_minutes == 30

    def test_block_with_only_trend_is_compiled(self):
        # trend predicates alone must be enough to compile (not None)
        assert parse_detect_block("x", {"trend_predicates": OOM_TREND["trend_predicates"]}) is not None

    def test_entry_without_threshold_is_skipped(self):
        block = parse_detect_block("x", {"trend_predicates": [{"metric": "m"}]})
        assert block is None  # nothing valid compiled


class TestFindingSeverity:
    def test_predicted_finding_to_dict(self):
        f = Finding(
            playbook="OOMKilled", cluster_id="c1", namespace="default",
            object_name="payments-7d9", evidence="predicted OOM in ~7m",
            severity="predicted", source="trend", eta_minutes=7.0,
        )
        d = f.to_dict()
        assert d["severity"] == "predicted"
        assert d["eta_minutes"] == 7.0
        assert d["source"] == "trend"

    def test_default_finding_is_warning(self):
        f = Finding(playbook="x", cluster_id="c", namespace="n", object_name="o", evidence="e")
        assert f.to_dict()["severity"] == "warning"
        assert f.to_dict()["eta_minutes"] is None


class TestEvaluateTrends:
    def test_fires_predicted_finding(self, monkeypatch):
        block = parse_detect_block("OOMKilled", OOM_TREND)
        eng = DetectorEngine(detectors=(block,), cluster_id="test")
        monkeypatch.setattr(engine_mod, "query_prometheus_series",
                            lambda *a, **k: ([_rising_series()], None))

        fired = asyncio.run(eng.evaluate_trends(now=1000.0))
        assert len(fired) == 1
        f = fired[0]
        assert f.severity == "predicted"
        assert f.namespace == "default" and f.object_name == "payments-7d9"
        assert f.eta_minutes is not None and f.eta_minutes > 0

    def test_dedup_no_refire_within_ttl(self, monkeypatch):
        block = parse_detect_block("OOMKilled", OOM_TREND)
        eng = DetectorEngine(detectors=(block,), cluster_id="test")
        monkeypatch.setattr(engine_mod, "query_prometheus_series",
                            lambda *a, **k: ([_rising_series()], None))

        first = asyncio.run(eng.evaluate_trends(now=1000.0))
        second = asyncio.run(eng.evaluate_trends(now=1060.0))  # 60s later, still predicting
        assert len(first) == 1
        assert len(second) == 0

    def test_flat_series_fires_nothing(self, monkeypatch):
        block = parse_detect_block("OOMKilled", OOM_TREND)
        eng = DetectorEngine(detectors=(block,), cluster_id="test")
        flat = {"metric": {"namespace": "default", "pod": "stable"},
                "values": [[i * 60, "0.5"] for i in range(11)]}
        monkeypatch.setattr(engine_mod, "query_prometheus_series", lambda *a, **k: ([flat], None))
        assert asyncio.run(eng.evaluate_trends(now=1000.0)) == []

    def test_prometheus_error_is_fail_open(self, monkeypatch):
        block = parse_detect_block("OOMKilled", OOM_TREND)
        eng = DetectorEngine(detectors=(block,), cluster_id="test")

        def _boom(*a, **k):
            raise RuntimeError("prometheus down")

        monkeypatch.setattr(engine_mod, "query_prometheus_series", _boom)
        assert asyncio.run(eng.evaluate_trends(now=1000.0)) == []  # no raise
        # …and fail-open is not fail-silent: the engine records that it could not see.
        assert eng.trend_blind_since == 1000.0
        assert "prometheus down" in (eng.last_trend_error or "")
