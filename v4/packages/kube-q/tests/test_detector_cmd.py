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


@respx.mock
def test_a_rejected_description_does_not_exit_zero(monkeypatch, capsys):
    """`kq detector new` on a description the compiler refuses must not report success.

    The server answers 200 with `staged: false` plus the compile errors — it is a valid response,
    not an HTTP failure — so `raise_for_status()` passes and the exit code is the only
    machine-readable signal that no detector was created. It used to be 0, which told
    `kq detector new … && kq detector promote …` that something existed to promote.
    """
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    respx.post("http://test-server/v1/detectors").mock(
        return_value=Response(200, json={"staged": False, "compiled": {},
                                         "errors": ["unknown field 'foo'", "no predicate"]})
    )
    assert detector_cmd.run(["new", "pods stuck terminating"]) == 3
    out = capsys.readouterr().out
    assert "Not staged" in out and "no predicate" in out


@respx.mock
def test_a_staged_detector_still_exits_zero(monkeypatch, capsys):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    respx.post("http://test-server/v1/detectors").mock(
        return_value=Response(200, json={"staged": True, "name": "stuck-terminating",
                                         "compiled": {"detect": {}}})
    )
    assert detector_cmd.run(["new", "pods stuck terminating"]) == 0
    assert "Staged shadow detector" in capsys.readouterr().out
