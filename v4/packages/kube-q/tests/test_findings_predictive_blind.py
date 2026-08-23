"""`kq findings` must not print an all-clear while predictive detection is blind.

The sensorium (kubectl `--watch`) and the anticipatory detectors (ADR-010, which see
through Prometheus) are two independent instruments. `sensorium: active` says the watch
stream is connected; it says nothing about whether Prometheus answered. Before
2026-08-20 a Prometheus outage produced an empty findings list and the green line
"No findings · N detectors watching" — an all-clear earned by a detector that never
looked.
"""
from __future__ import annotations

import os

import pytest
import respx
from httpx import Response

from kube_q.cli import findings_cmd


@pytest.fixture(autouse=True)
def _clean_kube_q_env(monkeypatch):
    for key in [k for k in os.environ if k.startswith("KUBE_Q_")]:
        monkeypatch.delenv(key, raising=False)


def _mock(monkeypatch, payload):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    respx.get("http://test-server/v1/findings").mock(return_value=Response(200, json=payload))


BLIND = {
    "sensorium": "active",
    "detectors": 20,
    "predictive": "blind",
    "predictive_detectors": 3,
    "predictive_error": "prometheus unreachable: Connection refused",
    "findings": [],
}


@respx.mock
def test_blind_predictive_never_prints_the_green_all_clear(monkeypatch, capsys):
    _mock(monkeypatch, BLIND)
    assert findings_cmd.run([]) == 0
    out = capsys.readouterr().out
    assert "[green]" not in out  # rich markup is rendered, but colour is stripped here
    assert "No findings · 20 detectors watching\n" not in out
    assert "not an all-clear" in out


@respx.mock
def test_blind_predictive_says_it_is_blind(monkeypatch, capsys):
    _mock(monkeypatch, BLIND)
    assert findings_cmd.run([]) == 0
    out = capsys.readouterr().out
    assert "blind" in out.lower()


@respx.mock
def test_blind_predictive_reports_the_reason(monkeypatch, capsys):
    _mock(monkeypatch, BLIND)
    assert findings_cmd.run([]) == 0
    assert "Connection refused" in capsys.readouterr().out


@respx.mock
def test_blind_with_no_reason_recorded_still_warns(monkeypatch, capsys):
    payload = dict(BLIND, predictive_error=None)
    _mock(monkeypatch, payload)
    assert findings_cmd.run([]) == 0
    out = capsys.readouterr().out
    assert "blind" in out.lower()
    assert "no reason recorded" in out


@respx.mock
def test_blind_predictive_still_exits_zero(monkeypatch, capsys):
    # Blindness is a caveat on the answer, not a transport failure — scripts that
    # check the exit code must not see it as an error.
    _mock(monkeypatch, BLIND)
    assert findings_cmd.run([]) == 0


@respx.mock
def test_blind_predictive_is_reported_even_when_findings_exist(monkeypatch, capsys):
    payload = dict(
        BLIND,
        findings=[
            {
                "playbook": "CrashLoopBackOff",
                "namespace": "prod",
                "object": "web-1",
                "evidence": "pod status=CrashLoopBackOff",
                "fired_at": 1781250000.0,
            }
        ],
    )
    _mock(monkeypatch, payload)
    assert findings_cmd.run([]) == 0
    out = capsys.readouterr().out
    assert "blind" in out.lower()
    assert "CrashLoopBackOff" in out  # the table is still printed


@respx.mock
def test_active_predictive_keeps_the_green_line(monkeypatch, capsys):
    payload = dict(BLIND, predictive="active", predictive_error=None)
    _mock(monkeypatch, payload)
    assert findings_cmd.run([]) == 0
    out = capsys.readouterr().out
    assert "No findings" in out
    assert "blind" not in out.lower()


@respx.mock
def test_predictive_off_keeps_the_green_line(monkeypatch, capsys):
    # `off` is a deliberate configuration, not an outage: PREDICTIVE_DETECTION_ENABLED
    # is false by default, so the ordinary case must stay quiet.
    payload = dict(BLIND, predictive="off", predictive_detectors=0, predictive_error=None)
    _mock(monkeypatch, payload)
    assert findings_cmd.run([]) == 0
    out = capsys.readouterr().out
    assert "No findings" in out
    assert "blind" not in out.lower()


@respx.mock
def test_absent_predictive_field_keeps_the_green_line(monkeypatch, capsys):
    # An older server that does not report the field at all. kq and the server ship
    # from this one repo in lockstep, so this is the pre-2026-08-20 shape only.
    payload = {"sensorium": "active", "detectors": 20, "findings": []}
    _mock(monkeypatch, payload)
    assert findings_cmd.run([]) == 0
    assert "No findings" in capsys.readouterr().out


@respx.mock
def test_not_watching_and_blind_report_both_instruments(monkeypatch, capsys):
    payload = dict(BLIND, sensorium="degraded", streams=[])
    _mock(monkeypatch, payload)
    assert findings_cmd.run([]) == 0
    out = capsys.readouterr().out
    assert "not watching" in out
    assert "blind" in out.lower()
