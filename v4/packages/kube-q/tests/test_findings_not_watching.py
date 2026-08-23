"""`kq findings` must not print a green all-clear when nothing is watching.

The server reports `sensorium` as one of `disabled | starting | reconnecting | stopped | active`.
Only `active` means a `kubectl --watch` stream is connected. The old client tested
`!= "active"` and printed *"Sensorium is disabled on this server."* for every non-active state, and
otherwise printed the green **"No findings · N detectors watching"** — so a sensorium whose watch
tasks had permanently exited (measured: kubectl absent ⇒ `FileNotFoundError` ⇒ the loop `return`s)
rendered as a healthy cluster with N detectors on watch.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from kube_q.cli import findings_cmd


def _run(body: dict, capsys) -> tuple[int, str]:
    resp = MagicMock()
    resp.json.return_value = body
    resp.raise_for_status.return_value = None
    client = MagicMock()
    client.get.return_value = resp
    client.__enter__ = lambda self: client
    client.__exit__ = lambda *a: False
    cfg = type("C", (), {"url": "http://x", "api_key": "k", "timeout": 5})()
    with patch.object(findings_cmd, "make_client", return_value=client), \
         patch.object(findings_cmd, "load_config", return_value=cfg):
        rc = findings_cmd.run([])
    return rc, capsys.readouterr().out


_STOPPED = {
    "sensorium": "stopped", "detectors": 20, "findings": [],
    "streams": [{"name": "get pods -A", "connected": False, "stopped": True,
                 "consecutive_failures": 0, "last_error": "kubectl not found on the server"}],
}
_RECONNECTING = {
    "sensorium": "reconnecting", "detectors": 20, "findings": [],
    "streams": [{"name": "get pods -A", "connected": False, "stopped": False,
                 "consecutive_failures": 9,
                 "last_error": 'pods is forbidden: User "sa" cannot watch'}],
}
_STARTING = {"sensorium": "starting", "detectors": 20, "findings": [], "streams": []}


class TestANotWatchingSensoriumNeverReadsAsAllClear:
    @pytest.mark.parametrize("body", [_STOPPED, _RECONNECTING, _STARTING],
                             ids=["stopped", "reconnecting", "starting"])
    def test_the_green_line_is_not_printed(self, body, capsys):
        _, out = _run(body, capsys)
        assert "No findings" not in out, out
        assert "detectors watching" not in out, out

    @pytest.mark.parametrize("body", [_STOPPED, _RECONNECTING, _STARTING],
                             ids=["stopped", "reconnecting", "starting"])
    def test_it_says_an_empty_result_is_not_health(self, body, capsys):
        _, out = _run(body, capsys)
        assert "not watching" in out
        assert "does NOT mean the cluster is healthy" in out

    def test_it_names_the_reason_per_stream(self, capsys):
        _, out = _run(_RECONNECTING, capsys)
        assert "forbidden" in out
        assert "get pods -A" in out

    def test_a_missing_kubectl_is_named(self, capsys):
        _, out = _run(_STOPPED, capsys)
        assert "kubectl not found" in out

    def test_no_streams_yet_is_said_plainly(self, capsys):
        _, out = _run(_STARTING, capsys)
        assert "no watch streams have started yet" in out


class TestTheHonestStatesAreUnchanged:
    def test_disabled_still_says_disabled(self, capsys):
        rc, out = _run({"sensorium": "disabled", "streams": [], "findings": []}, capsys)
        assert rc == 0 and "disabled on this server" in out
        assert "not watching" not in out

    def test_active_and_empty_still_gets_the_green_line(self, capsys):
        rc, out = _run({"sensorium": "active", "detectors": 20, "streams": [
            {"name": "get pods -A", "connected": True, "stopped": False,
             "consecutive_failures": 0, "last_error": None}], "findings": []}, capsys)
        assert rc == 0
        assert "No findings" in out and "20 detectors watching" in out
        assert "not watching" not in out

    def test_active_with_findings_still_renders_the_table(self, capsys):
        body = {"sensorium": "active", "detectors": 20, "streams": [], "findings": [
            {"playbook": "CrashLoopBackOff", "namespace": "prod", "object": "web-1",
             "fired_at": 1765500000.0, "evidence": "restarts=7"}]}
        rc, out = _run(body, capsys)
        assert rc == 0 and "CrashLoopBackOff" in out
