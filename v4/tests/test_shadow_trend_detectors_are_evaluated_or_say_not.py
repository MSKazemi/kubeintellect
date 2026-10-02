"""tests/test_shadow_trend_detectors_are_evaluated_or_say_not.py

`evaluate_trends` iterated only `engine.detectors`, so a SHADOW detector whose predicate is a trend
predicate was loaded, reported `watching: true` ("evaluated on the predictive interval") by
`GET /v1/detectors/{name}/shadow-findings`, and evaluated by nothing. Its zero firings read as
"quiet, safe to promote" while meaning "never asked" — and the NL compiler is told to express a
metric condition as a trend predicate ALONE, so this is the common shape of an NL candidate.

The fix evaluates shadow trend predicates in the same sweep, with firings routed to the shadow
buffer ONLY (ADR-012's safety boundary: a shadow detector never reaches the watchtower), and makes
every status surface say what is evaluated in each `PREDICTIVE_DETECTION_ENABLED` state.
"""
from __future__ import annotations

import asyncio
from collections import deque

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.endpoints import detectors as ep
from app.core.config import settings
from app.detectors import engine as engine_mod
from app.detectors.engine import DetectorEngine
from app.detectors.models import parse_detect_block
from app.detectors.perception import perception_state

TREND = {
    "trend_predicates": [{
        "metric": "container_memory_working_set_bytes / kube_pod_container_resource_limits",
        "window_minutes": 30, "projection_horizon_minutes": 120, "threshold": 1.0,
        "fire_if_eta_within_minutes": 30, "direction": "rising", "min_r2": 0.6,
    }],
}


def _rising(pod="payments-7d9"):
    return {"metric": {"namespace": "default", "pod": pod},
            "values": [[i * 60, str(0.5 + 0.0005 * i * 60)] for i in range(11)]}


def _block(name):
    block = parse_detect_block(name, TREND)
    assert block is not None
    return block


@pytest.fixture
def rising(monkeypatch):
    monkeypatch.setattr(engine_mod, "query_prometheus_series",
                        lambda *a, **k: ([_rising()], None))


class TestAShadowTrendFiresIntoTheShadowBufferOnly:
    def test_shadow_prediction_reaches_the_shadow_buffer(self, rising):
        eng = DetectorEngine(detectors=(), shadow_detectors=(_block("nl:mem"),), cluster_id="t")
        asyncio.run(eng.evaluate_trends(now=1000.0))
        assert len(eng.shadow_findings) == 1
        f = eng.shadow_findings[0]
        assert f.playbook == "nl:mem" and f.severity == "predicted"
        assert f.source == "shadow" and f.eta_minutes is not None

    def test_shadow_prediction_never_reaches_the_watchtower(self, rising):
        """The ADR-012 boundary: no ring, no return value, no on_finding callback."""
        seen = []
        eng = DetectorEngine(detectors=(), shadow_detectors=(_block("nl:mem"),),
                             cluster_id="t", on_finding=seen.append)
        returned = asyncio.run(eng.evaluate_trends(now=1000.0))
        assert returned == [] and seen == [] and len(eng.findings) == 0

    def test_active_prediction_still_reaches_the_watchtower_not_the_shadow_buffer(self, rising):
        seen = []
        eng = DetectorEngine(detectors=(_block("OOMKilled"),), cluster_id="t",
                             on_finding=seen.append)
        returned = asyncio.run(eng.evaluate_trends(now=1000.0))
        assert len(returned) == 1 and seen == returned and len(eng.shadow_findings) == 0
        assert returned[0].source == "trend"

    def test_active_and_shadow_do_not_suppress_each_other(self, rising):
        """Same playbook name and object in both sets: separate re-fire windows."""
        eng = DetectorEngine(detectors=(_block("x"),), shadow_detectors=(_block("x"),),
                             cluster_id="t")
        returned = asyncio.run(eng.evaluate_trends(now=1000.0))
        assert len(returned) == 1 and len(eng.shadow_findings) == 1

    def test_shadow_prediction_is_deduplicated_within_the_ttl(self, rising):
        eng = DetectorEngine(detectors=(), shadow_detectors=(_block("nl:mem"),), cluster_id="t")
        asyncio.run(eng.evaluate_trends(now=1000.0))
        asyncio.run(eng.evaluate_trends(now=1060.0))
        assert len(eng.shadow_findings) == 1


class TestBlindnessIsDecidedPerSweep:
    def test_a_shadow_success_does_not_mask_an_active_outage(self, monkeypatch):
        def answer(metric, window):
            return ([], "Cannot reach Prometheus") if "active" in metric else ([], None)

        monkeypatch.setattr(engine_mod, "query_prometheus_series", answer)
        active = parse_detect_block("a", {"trend_predicates": [
            {**TREND["trend_predicates"][0], "metric": "active_metric"}]})
        shadow = parse_detect_block("s", {"trend_predicates": [
            {**TREND["trend_predicates"][0], "metric": "shadow_metric"}]})
        eng = DetectorEngine(detectors=(active,), shadow_detectors=(shadow,), cluster_id="t")
        asyncio.run(eng.evaluate_trends(now=500.0))
        assert eng.trend_blind_since == 500.0
        assert "Cannot reach Prometheus" in (eng.last_trend_error or "")

    def test_a_shadow_outage_makes_the_sweep_blind(self, monkeypatch):
        monkeypatch.setattr(engine_mod, "query_prometheus_series",
                            lambda *a, **k: ([], "Prometheus query timed out (15s)."))
        eng = DetectorEngine(detectors=(), shadow_detectors=(_block("nl:mem"),), cluster_id="t")
        asyncio.run(eng.evaluate_trends(now=500.0))
        assert eng.trend_blind_since == 500.0


class _Fake:
    def __init__(self, playbook, *, watch=(), trend=("m",)):
        self.playbook = playbook
        self.watch_predicates = tuple(watch)
        self.trend_predicates = tuple(trend)
        self.promql = ()


class _FakeEngine:
    def __init__(self, shadow=(), active=(), error=None, blind=None):
        self.detectors = tuple(active)
        self.shadow_detectors = tuple(shadow)
        self.shadow_findings = deque(maxlen=10)
        self.last_trend_error = error
        self.trend_blind_since = blind


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "NL_DETECTOR_AUTHORING_ENABLED", True)
    app = FastAPI()
    app.include_router(ep.router, prefix="/v1")
    return TestClient(app)


def _ask(client, monkeypatch, engine, name="nl:mem"):
    monkeypatch.setattr(ep, "get_engine", lambda: engine)
    return client.get(f"/v1/detectors/{name}/shadow-findings").json()


class TestWatchingTellsTheTruthInEveryFlagCombination:
    @pytest.mark.parametrize("flag", [True, False])
    def test_trend_only_shadow(self, client, monkeypatch, flag):
        monkeypatch.setattr(settings, "PREDICTIVE_DETECTION_ENABLED", flag)
        body = _ask(client, monkeypatch, _FakeEngine(shadow=[_Fake("nl:mem")]))
        assert body["watching"] is flag
        if flag:
            assert "shadow buffer only" in body["watching_reason"]
        else:
            assert "PREDICTIVE_DETECTION_ENABLED" in body["watching_reason"]

    def test_a_blind_trend_sweep_is_named_when_on(self, client, monkeypatch):
        monkeypatch.setattr(settings, "PREDICTIVE_DETECTION_ENABLED", True)
        eng = _FakeEngine(shadow=[_Fake("nl:mem")], error="Cannot reach Prometheus", blind=1.0)
        body = _ask(client, monkeypatch, eng)
        assert body["watching"] is True
        assert "BLIND" in body["watching_reason"] and "Cannot reach" in body["watching_reason"]

    def test_watch_plus_trend_is_only_half_evaluated_when_off(self, client, monkeypatch):
        monkeypatch.setattr(settings, "PREDICTIVE_DETECTION_ENABLED", False)
        body = _ask(client, monkeypatch,
                    _FakeEngine(shadow=[_Fake("nl:mem", watch=("pod",), trend=("m",))]))
        assert body["watching"] is True   # the watch half is evaluated
        assert "trend predicates are NOT evaluated" in body["watching_reason"]

    def test_watch_plus_trend_has_no_caveat_when_on(self, client, monkeypatch):
        monkeypatch.setattr(settings, "PREDICTIVE_DETECTION_ENABLED", True)
        body = _ask(client, monkeypatch,
                    _FakeEngine(shadow=[_Fake("nl:mem", watch=("pod",), trend=("m",))]))
        assert "NOT evaluated" not in body["watching_reason"]


class TestPerceptionCountsShadowTrendDetectors:
    def test_shadow_only_trend_detector_is_active_when_on(self, monkeypatch):
        monkeypatch.setattr(settings, "PREDICTIVE_DETECTION_ENABLED", True)
        st = perception_state(_FakeEngine(shadow=[_Fake("nl:mem")]))
        assert st.predictive == "active" and st.predictive_detectors == 1

    def test_shadow_only_trend_detector_is_blind_when_the_sweep_is(self, monkeypatch):
        monkeypatch.setattr(settings, "PREDICTIVE_DETECTION_ENABLED", True)
        st = perception_state(_FakeEngine(shadow=[_Fake("nl:mem")], error="down", blind=1.0))
        assert st.predictive == "blind" and st.predictive_error == "down"

    def test_everything_is_off_when_the_flag_is_off(self, monkeypatch):
        monkeypatch.setattr(settings, "PREDICTIVE_DETECTION_ENABLED", False)
        st = perception_state(_FakeEngine(shadow=[_Fake("nl:mem")], active=[_Fake("a")]))
        assert st.predictive == "off"

    def test_no_trend_detector_anywhere_is_off(self, monkeypatch):
        monkeypatch.setattr(settings, "PREDICTIVE_DETECTION_ENABLED", True)
        st = perception_state(_FakeEngine(shadow=[_Fake("nl:w", trend=())]))
        assert st.predictive == "off" and st.predictive_detectors == 0
