"""Tests for `kq preference`."""
from __future__ import annotations

import os

import pytest
import respx
from httpx import Response

from kube_q.cli import preference_cmd


@pytest.fixture(autouse=True)
def _clean_kube_q_env(monkeypatch):
    for key in [k for k in os.environ if k.startswith("KUBE_Q_")]:
        monkeypatch.delenv(key, raising=False)


@respx.mock
def test_preference_set(monkeypatch, capsys):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    route = respx.put("http://test-server/v1/preferences").mock(
        return_value=Response(200, json={
            "user": "default", "key": "default_namespace",
            "value": "payments", "source": "explicit",
        })
    )
    assert preference_cmd.run(["set", "default_namespace", "payments"]) == 0
    body = route.calls[0].request.content.decode()
    assert "default_namespace" in body and "payments" in body
    assert "remembered" in capsys.readouterr().out


@respx.mock
def test_preference_set_multiword_value(monkeypatch):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    route = respx.put("http://test-server/v1/preferences").mock(
        return_value=Response(200, json={"user": "default", "key": "remediation",
                                         "value": "dry run first", "source": "explicit"})
    )
    assert preference_cmd.run(["set", "remediation", "dry", "run", "first"]) == 0
    assert "dry run first" in route.calls[0].request.content.decode()


@respx.mock
def test_preference_list(monkeypatch, capsys):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    respx.get("http://test-server/v1/preferences").mock(
        return_value=Response(200, json={"user": "default", "preferences": [
            {"key": "default_namespace", "value": "payments", "source": "inferred",
             "confidence": 0.83, "occurrence_count": 5},
        ]})
    )
    assert preference_cmd.run(["list"]) == 0
    out = capsys.readouterr().out
    assert "default_namespace" in out and "83%" in out


@respx.mock
def test_preference_forget(monkeypatch, capsys):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    respx.delete("http://test-server/v1/preferences/default_namespace").mock(
        return_value=Response(200, json={"user": "default", "key": "default_namespace",
                                         "forgotten": True})
    )
    assert preference_cmd.run(["forget", "default_namespace"]) == 0
    assert "forgotten" in capsys.readouterr().out


@respx.mock
def test_preference_user_flag(monkeypatch):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    route = respx.get("http://test-server/v1/preferences").mock(
        return_value=Response(200, json={"user": "alice", "preferences": []})
    )
    assert preference_cmd.run(["list", "--user", "alice"]) == 0
    assert route.calls[0].request.url.params["user"] == "alice"


def test_preference_usage():
    assert preference_cmd.run([]) == 2
    assert preference_cmd.run(["--help"]) == 0
