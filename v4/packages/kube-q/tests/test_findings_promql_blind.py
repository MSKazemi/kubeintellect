"""`kq findings` must not print an all-clear while PromQL detection is blind (#20).

The server evaluates the instant `promql:` predicates of loaded detectors on their own interval
and reports `promql: off | starting | active | blind` on `GET /v1/findings`. A query that could
not run (Prometheus unconfigured, unreachable, timing out, rejecting the query) is the error
state, so an empty findings list is not evidence that the condition is absent. Before this
change `kq findings` ignored the field and printed the green "No findings" line.
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


BASE = {"sensorium": "active", "detectors": 20, "predictive": "off", "findings": []}


@respx.mock
def test_blind_promql_never_prints_the_green_all_clear(monkeypatch, capsys):
    _mock(monkeypatch, dict(BASE, promql="blind", promql_detectors=18,
                            promql_error="1 of 21 PromQL queries could not be evaluated — "
                                         "first: OOMKilled: Prometheus query timed out (15s)."))
    assert findings_cmd.run([]) == 0
    out = capsys.readouterr().out
    assert "No findings · 20 detectors watching\n" not in out
    assert "PromQL detection is blind" in out
    assert "timed out" in out
    assert "not an all-clear" in out


@respx.mock
def test_promql_not_yet_evaluated_is_not_an_all_clear(monkeypatch, capsys):
    _mock(monkeypatch, dict(BASE, promql="starting", promql_detectors=18))
    assert findings_cmd.run([]) == 0
    out = capsys.readouterr().out
    assert "No findings · 20 detectors watching\n" not in out
    assert "first evaluation" in out


@pytest.mark.parametrize("state", ["off", "active", None])
def test_off_active_or_an_older_server_keeps_the_all_clear(monkeypatch, capsys, state):
    payload = dict(BASE) if state is None else dict(BASE, promql=state)
    with respx.mock:
        _mock(monkeypatch, payload)
        assert findings_cmd.run([]) == 0
    assert "No findings · 20 detectors watching" in capsys.readouterr().out
