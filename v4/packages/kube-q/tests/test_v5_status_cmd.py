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


@respx.mock
def test_v5_status_surfaces_a_setting_that_does_nothing(monkeypatch, capsys):
    """The server excludes unwired flags from `active_flags`; the CLI must not let that read as silence.

    Without this row the operator turns on KI_V5_RIGHTSIZING, runs `kq v5-status`, sees
    "(none — v4 baseline)" and concludes the flag name was wrong — or worse, that it is on.
    """
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    body = {**_BODY, "active_flags": [], "set_but_unwired_flags": ["KI_V5_RIGHTSIZING"]}
    respx.get("http://test-server/v1/v5/status").mock(return_value=Response(200, json=body))
    assert v5_status_cmd.run([]) == 0
    out = capsys.readouterr().out
    assert "KI_V5_RIGHTSIZING" in out
    assert "no effect" in out


@respx.mock
def test_v5_status_stays_quiet_when_there_is_nothing_to_warn_about(monkeypatch, capsys):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    body = {**_BODY, "set_but_unwired_flags": []}
    respx.get("http://test-server/v1/v5/status").mock(return_value=Response(200, json=body))
    assert v5_status_cmd.run([]) == 0
    assert "no effect" not in capsys.readouterr().out


@respx.mock
def test_v5_status_tolerates_an_older_server(monkeypatch, capsys):
    """A kq newer than its server must not crash on the absent field — kq and the server ship apart."""
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    assert "set_but_unwired_flags" not in _BODY
    respx.get("http://test-server/v1/v5/status").mock(return_value=Response(200, json=_BODY))
    assert v5_status_cmd.run([]) == 0
    assert "no effect" not in capsys.readouterr().out


@respx.mock
def test_v5_status_surfaces_a_guard_entry_that_protects_nothing(monkeypatch, capsys):
    """The same idea one level down from `set_but_unwired_flags`.

    `KUBECTL_BLOCKED_NAMESPACES` is the outermost blast-radius control and every parser for it
    discards silently, so an entry that can never match (a glob, a slash) leaves the operator
    believing a namespace is protected when nothing is enforcing it.
    """
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    problem = ("KUBECTL_BLOCKED_NAMESPACES entry 'kube-*' is not a legal Kubernetes namespace "
               "name, so it protects nothing.")
    body = {**_BODY, "unenforceable_guard_config": [problem]}
    respx.get("http://test-server/v1/v5/status").mock(return_value=Response(200, json=body))
    assert v5_status_cmd.run([]) == 0
    out = capsys.readouterr().out
    assert "kube-*" in out
    assert "cannot match" in out


@respx.mock
def test_v5_status_says_nothing_when_the_guard_config_is_enforceable(monkeypatch, capsys):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    body = {**_BODY, "unenforceable_guard_config": []}
    respx.get("http://test-server/v1/v5/status").mock(return_value=Response(200, json=body))
    assert v5_status_cmd.run([]) == 0
    assert "cannot match" not in capsys.readouterr().out


@respx.mock
def test_v5_status_tolerates_a_server_without_the_guard_field(monkeypatch, capsys):
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    assert "unenforceable_guard_config" not in _BODY
    respx.get("http://test-server/v1/v5/status").mock(return_value=Response(200, json=_BODY))
    assert v5_status_cmd.run([]) == 0
    assert "cannot match" not in capsys.readouterr().out
