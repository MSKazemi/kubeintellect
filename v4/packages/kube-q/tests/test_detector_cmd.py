"""Tests for `kq detector`."""
from __future__ import annotations

import json
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


@respx.mock
def test_a_detector_refused_with_422_is_exit_three_with_the_reasons(monkeypatch, capsys):
    """The server now refuses a detector that cannot fire with 422 (zero predicates, a promql
    query nothing evaluates). That is a refusal on the merits, not a failed request: exit 3."""
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    respx.post("http://test-server/v1/detectors").mock(
        return_value=Response(422, json={
            "staged": False, "stored": False, "compiled": {"promql": ["up == 0"]},
            "detail": "the compiled detector was refused: promql is recorded but never evaluated",
            "errors": ["promql is recorded but never evaluated"]})
    )
    assert detector_cmd.run(["new", "pods stuck pending"]) == 3
    out = capsys.readouterr().out
    assert "Not staged" in out and "never evaluated" in out


@respx.mock
def test_stored_but_not_loaded_is_not_success(monkeypatch, capsys):
    """202: the row exists, but no engine has loaded it — nothing is watching yet."""
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    respx.post("http://test-server/v1/detectors").mock(
        return_value=Response(202, json={
            "staged": False, "stored": True, "name": "nl:oom", "compiled": {},
            "staged_reason": "stored, but the detector engine is not running in this process"})
    )
    assert detector_cmd.run(["new", "pods getting OOM killed"]) == 1
    out = capsys.readouterr().out
    assert "NOT loaded" in out and "engine is not running" in out


@respx.mock
def test_recompile_and_name_reach_the_server(monkeypatch, capsys):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    route = respx.post("http://test-server/v1/detectors").mock(
        return_value=Response(200, json={"staged": True, "name": "nl:oom-v2", "compiled": {},
                                         "compilation": {"source": "fresh"}})
    )
    assert detector_cmd.run(
        ["new", "--recompile", "--name", "nl:oom-v2", "pods", "getting", "OOM", "killed"]) == 0
    body = json.loads(route.calls[0].request.content.decode())
    assert body == {"description": "pods getting OOM killed", "recompile": True,
                    "name": "nl:oom-v2"}
