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
            # A current server sends the verdict as data alongside the prose. Without those
            # fields the command exits 4 by design — an absent verdict is not a passing one —
            # and that case is asserted in
            # tests/test_postmortem_exit_code_matches_its_own_verdict.py.
            json={"markdown": "# Incident postmortem — `ep-1`\n\n> Audit chain verified intact",
                  "chain_valid": True, "chain_verified": True, "events_lost": 0, "gaps": []},
        )
    )
    assert postmortem_cmd.run(["ep-1"]) == 0
    assert route.calls[0].request.url.params["format"] == "markdown"
    out = capsys.readouterr().out
    assert "Incident postmortem" in out


def test_postmortem_usage():
    assert postmortem_cmd.run(["--help"]) == 0
    assert postmortem_cmd.run([]) == 2
