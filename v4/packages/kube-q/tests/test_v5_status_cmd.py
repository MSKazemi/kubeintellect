"""Tests for `kq v5-status` (v5 trust-plane state)."""
from __future__ import annotations

import os

import pytest
import respx
from httpx import Response

from kube_q.cli import v5_status_cmd


@pytest.fixture(autouse=True)
def _clean_kube_q_env(monkeypatch):
    for key in [k for k in os.environ if k.startswith("KUBE_Q_")]:
        monkeypatch.delenv(key, raising=False)


_BODY = {
    "arm": "v4", "version": "2.1.0", "cortex_v5_enabled": True,
    "active_flags": ["CORTEX_V5_ENABLED", "KI_V5_HARNESS_FANOUT"],
    "kill_switch_engaged": True, "change_freeze": False, "spend_cap_usd": 25.0,
}


@respx.mock
def test_v5_status_table(monkeypatch, capsys):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    respx.get("http://test-server/v1/v5/status").mock(return_value=Response(200, json=_BODY))
    assert v5_status_cmd.run([]) == 0
    out = capsys.readouterr().out
    assert "2.1.0" in out and "KI_V5_HARNESS_FANOUT" in out and "25.0" in out


@respx.mock
def test_v5_status_baseline(monkeypatch, capsys):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    body = {**_BODY, "cortex_v5_enabled": False, "active_flags": [], "kill_switch_engaged": False}
    respx.get("http://test-server/v1/v5/status").mock(return_value=Response(200, json=body))
    assert v5_status_cmd.run([]) == 0
    assert "v4 baseline" in capsys.readouterr().out


@respx.mock
def test_v5_status_http_error(monkeypatch, capsys):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    respx.get("http://test-server/v1/v5/status").mock(return_value=Response(500, text="boom"))
    assert v5_status_cmd.run([]) == 1
    assert "error" in capsys.readouterr().out.lower()


def test_v5_status_help(capsys):
    assert v5_status_cmd.run(["--help"]) == 0
    assert "v5-status" in capsys.readouterr().out


def test_v5_status_rejects_args(capsys):
    assert v5_status_cmd.run(["extra"]) == 2
