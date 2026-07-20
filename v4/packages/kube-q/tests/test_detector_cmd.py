"""Tests for `kq detector`."""
from __future__ import annotations

import os

import pytest
import respx
from httpx import Response

from kube_q.cli import detector_cmd


@pytest.fixture(autouse=True)
def _clean_kube_q_env(monkeypatch):
    for key in [k for k in os.environ if k.startswith("KUBE_Q_")]:
        monkeypatch.delenv(key, raising=False)


@respx.mock
def test_detector_new_stages_shadow(monkeypatch, capsys):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    route = respx.post("http://test-server/v1/detectors").mock(
        return_value=Response(200, json={
            "staged": True, "status": "shadow", "name": "nl:OOM",
            "compiled": {"watch_predicates": [{"kind": "Pod", "status_regex": "^OOMKilled$"}]},
            "errors": [],
        })
    )
    assert detector_cmd.run(["new", "pods getting OOM killed"]) == 0
    body = route.calls[0].request.content.decode()
    assert "OOM killed" in body
    assert "Staged shadow detector" in capsys.readouterr().out


@respx.mock
def test_detector_promote(monkeypatch, capsys):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    respx.post("http://test-server/v1/detectors/nl:OOM/promote").mock(
        return_value=Response(200, json={"name": "nl:OOM", "status": "active"})
    )
    assert detector_cmd.run(["promote", "nl:OOM"]) == 0
    assert "active" in capsys.readouterr().out


@respx.mock
def test_detector_list(monkeypatch, capsys):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    respx.get("http://test-server/v1/detectors").mock(
        return_value=Response(200, json={"detectors": [
            {"name": "nl:OOM", "source": "nl", "status": "shadow",
             "reviewed_by": None, "created_from": "oom killed pods"},
        ]})
    )
    assert detector_cmd.run(["list", "--status", "shadow"]) == 0
    assert "nl:OOM" in capsys.readouterr().out


def test_detector_usage():
    assert detector_cmd.run([]) == 2
    assert detector_cmd.run(["--help"]) == 0
