"""A quiet watch is a claim about perception, not about the record.

Two surfaces answer the same question over the same window — `GET /v1/findings`
(rendered by `kq findings`) and the morning digest (`kq digest`) — and they computed
it separately, so they disagreed.

Measured 2026-08-20 with a stub Postgres pool that answers every query truthfully with
zero rows (a genuinely fully-recorded night), `FLIGHT_RECORDER_ENABLED=true`,
`USE_SQLITE=false`, `WATCHTOWER_ENABLED=true`, and **nothing watching**:

    GET /v1/findings  ->  {"sensorium": "starting", "findings": []}
    kq findings       ->  "Sensorium is not watching … an empty result here does NOT
                           mean the cluster is healthy"
    kq digest         ->  "Quiet watch: no findings in the last 24h."

The digest validated its *recording* sources — recorder flag, SQLite mode, watchtower
flag, pool, and both queries — and never asked whether anything had been looking. A
flawless empty record and an empty cluster read identically.

Both surfaces now read `app.detectors.perception`, so they cannot answer differently.
"""
from __future__ import annotations

import asyncio

import pytest
from app.api.v1.endpoints.findings import list_findings
from app.core.config import settings
from app.detectors import perception, service
from app.detectors.perception import (
    PerceptionState,
    perception_gaps,
    perception_state,
)
from app.digest import builder
from app.sensorium import k8s_watcher
from app.sensorium.k8s_watcher import StreamHealth, reset_stream_health


class _Engine:
    detectors = tuple(range(20))
    trend_blind_since = None
    last_trend_error = None

    def recent_findings(self, **_k):
        return []


class _StubPool:
    """A pool that answers truthfully with zero rows — a real, quiet, recorded night."""

    async def fetch(self, *_a, **_k):
        return []


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    reset_stream_health()
    monkeypatch.setattr(service, "_engine", _Engine())
    monkeypatch.setattr(builder, "_get_pool", lambda: _StubPool())
    monkeypatch.setattr(settings, "FLIGHT_RECORDER_ENABLED", True)
    monkeypatch.setattr(settings, "USE_SQLITE", False)
    monkeypatch.setattr(settings, "WATCHTOWER_ENABLED", True)
    monkeypatch.setattr(settings, "PREDICTIVE_DETECTION_ENABLED", False)
    yield
    reset_stream_health()


def _stream(name, *, connected=False, stopped=False, err=None):
    h = StreamHealth(name)
    h.connected, h.stopped, h.last_error = connected, stopped, err
    k8s_watcher._streams[name] = h
    return h


def _digest(hours=24.0) -> dict:
    return asyncio.run(builder.build_digest(hours))


def _findings() -> dict:
    return asyncio.run(list_findings(limit=100, since=0.0))


class TestTheMeasuredDisagreement:
    def test_nothing_watching_is_not_a_quiet_watch(self):
        # No stream has ever started: `/v1/findings` calls this "starting".
        assert _findings()["sensorium"] == "starting"
        d = _digest()
        assert "Quiet watch" not in d["summary"]
        assert d["degraded"] is True

    def test_the_reason_names_the_instrument_not_just_degraded(self):
        d = _digest()
        assert any("watch stream" in r for r in d["degraded_reasons"])

    def test_a_connected_stream_and_a_clean_record_is_a_quiet_watch(self):
        _stream("get pods -A", connected=True)
        d = _digest()
        assert d["summary"] == "Quiet watch: no findings in the last 24h."
        assert d["degraded"] is False
        assert d["degraded_reasons"] == []

    def test_the_two_surfaces_read_one_classifier(self, monkeypatch):
        """The property, not the wording: change perception once, both surfaces move."""
        _stream("get pods -A", connected=True)
        assert _digest()["degraded"] is False
        assert _findings()["sensorium"] == "active"

        blind = PerceptionState("stopped", 20, "off", 0, None,
                                [{"name": "get pods -A", "stopped": True,
                                  "last_error": "kubectl not found"}])
        monkeypatch.setattr(perception, "perception_state", lambda: blind)
        monkeypatch.setattr("app.api.v1.endpoints.findings.perception_state",
                            lambda *_a: blind)
        assert _digest()["degraded"] is True
        assert _findings()["sensorium"] == "stopped"

    def test_the_endpoint_holds_the_classifier_itself_not_a_copy_of_its_logic(self):
        """Structural, so the two cannot drift apart again: the endpoint's name is
        bound to the shared function object, not to a reimplementation of it."""
        from app.api.v1.endpoints import findings as findings_endpoint

        assert findings_endpoint.perception_state is perception.perception_state

    def test_the_digest_resolves_the_classifier_at_call_time(self, monkeypatch):
        """The digest imports it inside the function, so patching the module reaches it.
        If it ever captured a module-level alias this test goes red."""
        called: list[bool] = []

        def _spy():
            called.append(True)
            return PerceptionState("active", 20, "off", 0, None, [])

        monkeypatch.setattr(perception, "perception_state", _spy)
        _digest()
        assert called == [True]

    def test_a_stopped_stream_carries_kubectls_own_reason_into_the_digest(self):
        _stream("get pods -A", stopped=True, err="kubectl not found on the server")
        d = _digest()
        assert "kubectl not found on the server" in " ".join(d["degraded_reasons"])

    def test_the_markdown_refuses_to_read_as_an_all_clear(self):
        md = builder.render_markdown(_digest())
        assert "do not read it as an all-clear" in md
        assert "Quiet watch" not in md


class TestPerceptionStateClassifies:
    def test_no_engine_is_disabled(self, monkeypatch):
        monkeypatch.setattr(service, "_engine", None)
        assert perception_state().sensorium == "disabled"

    def test_no_streams_is_starting(self):
        assert perception_state().sensorium == "starting"

    def test_a_connected_stream_is_active_and_watching(self):
        _stream("get pods -A", connected=True)
        state = perception_state()
        assert state.sensorium == "active"
        assert state.watching is True

    def test_every_stream_stopped_is_stopped(self):
        _stream("a", stopped=True)
        _stream("b", stopped=True)
        assert perception_state().sensorium == "stopped"

    def test_one_stream_down_but_not_stopped_is_reconnecting(self):
        _stream("a", connected=False, stopped=False, err="Unauthorized")
        assert perception_state().sensorium == "reconnecting"

    def test_detector_count_comes_from_the_engine(self):
        assert perception_state().detectors == 20

    def test_predictive_is_off_when_the_flag_is_off(self, monkeypatch):
        monkeypatch.setattr(settings, "PREDICTIVE_DETECTION_ENABLED", False)
        assert perception_state().predictive == "off"


class TestPerceptionGaps:
    def test_a_watching_sensorium_has_no_gaps(self):
        _stream("get pods -A", connected=True)
        assert perception_gaps(perception_state()) == []

    def test_disabled_says_no_finding_could_have_been_produced(self, monkeypatch):
        monkeypatch.setattr(service, "_engine", None)
        gaps = perception_gaps(perception_state())
        assert len(gaps) == 1
        assert "could have been produced" in gaps[0]

    def test_predictive_off_is_a_configuration_not_a_gap(self):
        # PREDICTIVE_DETECTION_ENABLED is false by default; if `off` counted as a gap,
        # every ordinary digest would be permanently degraded and the warning worthless.
        _stream("get pods -A", connected=True)
        state = perception_state()
        assert state.predictive == "off"
        assert perception_gaps(state) == []

    def test_predictive_blind_is_a_gap_and_carries_the_reason(self):
        _stream("get pods -A", connected=True)
        state = PerceptionState("active", 20, "blind", 3,
                                "prometheus unreachable: Connection refused", [])
        gaps = perception_gaps(state)
        assert len(gaps) == 1
        assert "Connection refused" in gaps[0]

    def test_blind_predictive_alone_stops_the_quiet_watch_line(self, monkeypatch):
        _stream("get pods -A", connected=True)
        blind = PerceptionState("active", 20, "blind", 3, "Connection refused", [])
        monkeypatch.setattr(perception, "perception_state", lambda: blind)
        d = _digest()
        assert "Quiet watch" not in d["summary"]
        assert "Connection refused" in " ".join(d["degraded_reasons"])

    def test_both_instruments_blind_report_two_reasons(self, monkeypatch):
        both = PerceptionState("stopped", 20, "blind", 3, "Connection refused",
                               [{"name": "a", "stopped": True, "last_error": "kubectl missing"}])
        monkeypatch.setattr(perception, "perception_state", lambda: both)
        assert len(_digest()["degraded_reasons"]) == 2


class TestPerceptionNeverBreaksTheDigest:
    def test_a_raising_perception_layer_degrades_rather_than_500s(self, monkeypatch):
        def _boom():
            raise RuntimeError("sensorium exploded")

        monkeypatch.setattr(perception, "perception_state", _boom)
        d = _digest()
        assert d["degraded"] is True
        assert "unknown" in " ".join(d["degraded_reasons"])
        assert "Quiet watch" not in d["summary"]

    def test_the_record_side_checks_still_fire(self, monkeypatch):
        _stream("get pods -A", connected=True)
        monkeypatch.setattr(settings, "FLIGHT_RECORDER_ENABLED", False)
        assert "flight recorder is disabled" in " ".join(_digest()["degraded_reasons"])
