"""Tests for `kq postmortem`."""
from __future__ import annotations

import os

import pytest
import respx
from httpx import Response

from kube_q.cli import postmortem_cmd


@pytest.fixture(autouse=True)
def _clean_kube_q_env(monkeypatch):
    for key in [k for k in os.environ if k.startswith("KUBE_Q_")]:
        monkeypatch.delenv(key, raising=False)


@respx.mock
def test_postmortem_renders_markdown(monkeypatch, capsys):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    route = respx.get("http://test-server/v1/episodes/ep-1/postmortem").mock(
        return_value=Response(
            200,
            json={"markdown": "# Incident postmortem — `ep-1`\n\n> Audit chain verified intact"},
        )
    )
    assert postmortem_cmd.run(["ep-1"]) == 0
    assert route.calls[0].request.url.params["format"] == "markdown"
    out = capsys.readouterr().out
    assert "Incident postmortem" in out


def test_postmortem_usage():
    assert postmortem_cmd.run(["--help"]) == 0
    assert postmortem_cmd.run([]) == 2
