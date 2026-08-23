"""A detector that cannot reach Prometheus must not report zero attacks and zero errors.

`_query_raw` never raises — it returns `(series, error)`. `query_prometheus_range_raw` threw the
error half away, and the two deterministic (non-LLM) consumers were built on it:

* `agentic_gpu_collector._default_scalar` — documented as *"0.0 if absent/unreachable"*. Measured
  2026-08-20 against a **real closed port** (`http://127.0.0.1:9`, a genuine `httpx.ConnectError`,
  nothing mocked): every signal read `0.0` — `sandbox_escape_attempts`, GPU ECC errors, DRA
  allocation failures, agent cost — and `collect_and_detect()` returned `[]`, which its own
  docstring glossed as *"empty when healthy"*. Identical result with `PROMETHEUS_URL` unset.
* `DetectorEngine.evaluate_trends` — documented *"Fail-open: a Prometheus outage … yields no
  findings"*, with a `logger.warning("trend_query_error")` in an `except` block that a Prometheus
  outage can never reach, because the outage arrives as a returned tuple, not an exception. So the
  predictive layer — the one whose entire job is to warn *before* a failure — went silent with no
  finding and no log line.

The LLM-facing `query_prometheus` tool was always honest: it returns the error string to the model.
Only the deterministic consumers dropped it.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.config import settings
from app.detectors import agentic_gpu_collector as collector
from app.detectors import engine as engine_mod
from app.detectors.agentic_gpu_collector import UNAVAILABLE, collect_and_detect
from app.detectors.engine import DetectorEngine
from app.detectors.models import parse_detect_block
from app.tools import prometheus_tool as prom

# A port nothing listens on (RFC 863 discard, not bound here) — a real connection refusal.
CLOSED_PORT = "http://127.0.0.1:9"

TREND = {
    "trend_predicates": [{
        "metric": 'container_memory_working_set_bytes{pod=~"payments.*"}',
        "window_minutes": 30,
        "threshold": 1.0,
        "direction": "rising",
    }],
}


@pytest.fixture
def unreachable_prometheus(monkeypatch):
    monkeypatch.setattr(settings, "PROMETHEUS_URL", CLOSED_PORT)


@pytest.fixture
def unconfigured_prometheus(monkeypatch):
    monkeypatch.setattr(settings, "PROMETHEUS_URL", "")


class TestTheSeamKeepsTheError:
    def test_a_real_connection_refusal_comes_back_as_an_error(self, unreachable_prometheus):
        series, error = prom.query_prometheus_series("up", 0)
        assert series == []
        assert error is not None and "Cannot reach Prometheus" in error

    def test_an_unconfigured_url_is_an_error_not_an_empty_result(self, unconfigured_prometheus):
        series, error = prom.query_prometheus_series("up", 0)
        assert series == [] and error is not None and "not configured" in error

    def test_the_lossy_helper_still_exists_and_still_loses_it(self, unreachable_prometheus):
        # Kept deliberately for data-only callers; the point is that it is now the exception.
        assert prom.query_prometheus_range_raw("up", 0) == []

    def test_the_llm_tool_was_always_honest(self, unreachable_prometheus):
        assert "Cannot reach Prometheus" in prom.query_prometheus.invoke({"promql": "up"})


class TestABlindSecurityDetectorSaysSo:
    def test_an_unreachable_prometheus_is_not_a_clean_bill_of_health(self, unreachable_prometheus):
        hits = collect_and_detect()
        assert hits, "an unreadable metrics backend produced an empty (= healthy) hit list"
        assert hits[0].kind == UNAVAILABLE and hits[0].severity == "warning"
        assert "blind, not clear" in hits[0].detail

    def test_an_unconfigured_prometheus_is_not_a_clean_bill_of_health(
        self, unconfigured_prometheus
    ):
        hits = collect_and_detect()
        assert hits and hits[0].kind == UNAVAILABLE

    def test_the_scalar_seam_distinguishes_absent_from_unaskable(self, unreachable_prometheus):
        assert collector._default_scalar(collector._Q_ESCAPE) is None

    def test_a_readable_but_empty_metric_is_still_zero_and_still_healthy(self):
        # The documented behaviour is preserved: a metric that simply is not there reads 0.
        assert collect_and_detect(scalar=lambda q: 0.0) == []

    def test_a_real_breach_still_fires_and_is_not_masked(self):
        def scalar(q):
            return 3.0 if q == collector._Q_ESCAPE else 0.0
        kinds = [h.kind for h in collect_and_detect(scalar=scalar)]
        assert "sandbox-escape" in kinds
        assert UNAVAILABLE not in kinds

    def test_one_unreadable_signal_among_readable_ones_is_still_reported(self):
        def scalar(q):
            return None if q == collector._Q_ECC else 0.0
        hits = collect_and_detect(scalar=scalar)
        assert hits and hits[0].kind == UNAVAILABLE
        assert "1 of 7" in hits[0].detail


class TestPredictiveDetectionSaysWhenItIsBlind:
    def _engine(self):
        return DetectorEngine(detectors=(parse_detect_block("OOMKilled", TREND),), cluster_id="t")

    def test_an_outage_is_recorded_not_swallowed(self, monkeypatch):
        monkeypatch.setattr(engine_mod, "query_prometheus_series",
                            lambda *a, **k: ([], "Cannot reach Prometheus at http://x"))
        eng = self._engine()
        assert asyncio.run(eng.evaluate_trends(now=500.0)) == []   # still fail-open
        assert eng.trend_blind_since == 500.0                      # but not fail-silent
        assert "Cannot reach Prometheus" in (eng.last_trend_error or "")

    def test_the_blind_flag_clears_when_prometheus_answers_again(self, monkeypatch):
        eng = self._engine()
        monkeypatch.setattr(engine_mod, "query_prometheus_series",
                            lambda *a, **k: ([], "Prometheus query timed out (15s)."))
        asyncio.run(eng.evaluate_trends(now=500.0))
        assert eng.trend_blind_since is not None
        monkeypatch.setattr(engine_mod, "query_prometheus_series", lambda *a, **k: ([], None))
        asyncio.run(eng.evaluate_trends(now=560.0))
        assert eng.trend_blind_since is None and eng.last_trend_error is None

    def test_a_healthy_empty_answer_is_not_blindness(self, monkeypatch):
        monkeypatch.setattr(engine_mod, "query_prometheus_series", lambda *a, **k: ([], None))
        eng = self._engine()
        assert asyncio.run(eng.evaluate_trends(now=500.0)) == []
        assert eng.trend_blind_since is None


class TestTheFindingsEndpointReportsPredictiveBlindness:
    """Same rule as pass 71's `sensorium` field, one layer over.

    `kq findings` renders an empty list as reassurance. It is only reassurance if the detectors
    could see, and for the predictive ones that means Prometheus answered.
    """

    def _get(self, monkeypatch, engine, *, flag=True):
        import asyncio as _asyncio

        from app.api.v1.endpoints.findings import list_findings
        from app.detectors import service
        from app.sensorium import k8s_watcher
        from app.sensorium.k8s_watcher import StreamHealth, reset_stream_health

        reset_stream_health()
        health = StreamHealth("pods")
        health.connected = True
        k8s_watcher._streams["pods"] = health
        monkeypatch.setattr(service, "_engine", engine)
        monkeypatch.setattr(settings, "PREDICTIVE_DETECTION_ENABLED", flag)
        try:
            return _asyncio.run(list_findings(limit=100, since=0.0))
        finally:
            reset_stream_health()

    def _engine(self):
        return DetectorEngine(detectors=(parse_detect_block("OOMKilled", TREND),), cluster_id="t")

    def test_a_blind_engine_is_reported_as_blind(self, monkeypatch):
        eng = self._engine()
        eng.trend_blind_since = 1.0
        eng.last_trend_error = "Cannot reach Prometheus at http://x"
        payload = self._get(monkeypatch, eng)
        assert payload["sensorium"] == "active", "the watch streams are a separate claim"
        assert payload["predictive"] == "blind"
        assert "Cannot reach Prometheus" in payload["predictive_error"]

    def test_a_seeing_engine_is_reported_as_active(self, monkeypatch):
        payload = self._get(monkeypatch, self._engine())
        assert payload["predictive"] == "active" and payload["predictive_error"] is None
        assert payload["predictive_detectors"] == 1

    def test_the_flag_being_off_is_off_not_active(self, monkeypatch):
        payload = self._get(monkeypatch, self._engine(), flag=False)
        assert payload["predictive"] == "off"
