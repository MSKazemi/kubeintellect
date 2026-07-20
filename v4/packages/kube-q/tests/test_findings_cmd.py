"""Tests for `kq findings` (sensorium findings feed)."""
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


@respx.mock
def test_findings_table(monkeypatch, capsys):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    respx.get("http://test-server/v1/findings").mock(
        return_value=Response(
            200,
            json={
                "sensorium": "active",
                "detectors": 16,
                "findings": [
                    {
                        "playbook": "CrashLoopBackOff",
                        "namespace": "prod",
                        "object": "web-1",
                        "evidence": "pod status=CrashLoopBackOff",
                        "fired_at": 1781250000.0,
                    }
                ],
            },
        )
    )
    assert findings_cmd.run([]) == 0
    out = capsys.readouterr().out
    assert "CrashLoopBackOff" in out
    assert "web-1" in out


@respx.mock
def test_findings_empty(monkeypatch, capsys):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    respx.get("http://test-server/v1/findings").mock(
        return_value=Response(
            200, json={"sensorium": "active", "detectors": 16, "findings": []}
        )
    )
    assert findings_cmd.run([]) == 0
    assert "No findings" in capsys.readouterr().out


@respx.mock
def test_findings_sensorium_disabled(monkeypatch, capsys):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    respx.get("http://test-server/v1/findings").mock(
        return_value=Response(200, json={"sensorium": "disabled", "findings": []})
    )
    assert findings_cmd.run([]) == 0
    assert "disabled" in capsys.readouterr().out


def test_findings_usage():
    assert findings_cmd.run(["--help"]) == 0
    assert findings_cmd.run(["bogus"]) == 2
