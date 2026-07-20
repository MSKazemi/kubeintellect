"""Tests for `kq digest`."""
from __future__ import annotations

import os

import pytest
import respx
from httpx import Response

from kube_q.cli import digest_cmd


@pytest.fixture(autouse=True)
def _clean_kube_q_env(monkeypatch):
    for key in [k for k in os.environ if k.startswith("KUBE_Q_")]:
        monkeypatch.delenv(key, raising=False)


@respx.mock
def test_digest_renders_markdown(monkeypatch, capsys):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    respx.get("http://test-server/v1/digest").mock(
        return_value=Response(
            200,
            json={"markdown": "# KubeIntellect digest — last 24h\n\n2 finding(s)"},
        )
    )
    assert digest_cmd.run([]) == 0
    out = capsys.readouterr().out
    assert "KubeIntellect digest" in out


@respx.mock
def test_digest_hours_param(monkeypatch, capsys):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    route = respx.get("http://test-server/v1/digest").mock(
        return_value=Response(200, json={"markdown": "# d"})
    )
    assert digest_cmd.run(["--hours", "6"]) == 0
    assert route.calls[0].request.url.params["hours"] == "6.0"


def test_digest_usage():
    assert digest_cmd.run(["--help"]) == 0
    assert digest_cmd.run(["bogus"]) == 2
