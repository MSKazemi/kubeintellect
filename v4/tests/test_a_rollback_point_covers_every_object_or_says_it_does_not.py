"""A rollback point marked `restorable` must cover every object the command mutates.

`restorable` answers two independent questions, and before #190 it only answered the first:

1. is what we captured **faithful** — did the YAML survive redaction and the size cap intact;
2. does it **cover** every object the command will touch.

A capture can be perfectly faithful and still hold 3 of 7 objects. Nothing recorded the gap:
`restorable` stayed `True` and `capture_notes` stayed empty, so `ki_protocol/record.py` printed
no warning mark and the postmortem counted it as restorable. Someone recovering from an incident
would restore a subset believing they had restored everything -- safety invariant #1 (never
report a result you do not have), at the rollback layer.

Four independent ways an object drops out, each covered below:

* the `_ROLLBACK_MAX_TARGETS` cap, which was an unnamed `[:5]` slice;
* `kubectl get` exiting non-zero (deleted since, RBAC, wrong context);
* the fetch raising -- a 5s timeout is not generous on a loaded apiserver;
* the stdin manifest failing to parse half-way, leaving a partial object list.

`_capture_rollback_point` is best-effort **by contract and must never raise**; the last test
pins that, because everything above adds work to a function whose failure mode is silence.
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

from app.db import flight_recorder
from app.tools import kubectl_tool as kt

_OK_YAML = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {name}\ndata:\n  k: v\n"


def _manifest(n: int) -> str:
    return "\n---\n".join(
        f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cm{i}\n  namespace: prod\ndata:\n  k: v{i}\n"
        for i in range(n)
    )


def _capture(monkeypatch, manifest: str, runner) -> dict | None:
    recorded: list[dict] = []
    with patch.object(kt.subprocess, "run", runner), \
         patch.object(flight_recorder, "record", lambda sid, kind, payload: recorded.append(payload)):
        kt._capture_rollback_point("apply", ["apply", "-f", "-"], manifest, None, {})
    return recorded[0] if recorded else None


def _all_ok(cmd, **kw):
    return subprocess.CompletedProcess(cmd, 0, _OK_YAML.format(name=cmd[3]), "")


class TestAnIncompleteCaptureIsNotRestorable:
    def test_a_capture_of_every_object_is_restorable(self, monkeypatch):
        p = _capture(monkeypatch, _manifest(3), _all_ok)
        assert p["restorable"] is True
        assert (p["targets_intended"], p["targets_captured"]) == (3, 3)
        assert p["capture_notes"] == []

    def test_objects_past_the_cap_are_counted_and_named(self, monkeypatch):
        p = _capture(monkeypatch, _manifest(7), _all_ok)
        assert p["restorable"] is False, "5 of 7 objects is not a restore point"
        assert (p["targets_intended"], p["targets_captured"]) == (7, 5)
        assert any("never attempted" in n for n in p["capture_notes"])

    def test_a_failed_kubectl_get_is_counted_and_named(self, monkeypatch):
        def runner(cmd, **kw):
            if cmd[3] == "cm1":
                return subprocess.CompletedProcess(cmd, 1, "", "Error from server (NotFound)")
            return _all_ok(cmd, **kw)

        p = _capture(monkeypatch, _manifest(3), runner)
        assert p["restorable"] is False
        assert (p["targets_intended"], p["targets_captured"]) == (3, 2)
        assert any("cm1" in n and "exited 1" in n for n in p["capture_notes"])

    def test_a_fetch_that_raises_is_counted_and_named(self, monkeypatch):
        def runner(cmd, **kw):
            if cmd[3] == "cm2":
                raise subprocess.TimeoutExpired(cmd, 5)
            return _all_ok(cmd, **kw)

        p = _capture(monkeypatch, _manifest(3), runner)
        assert p["restorable"] is False
        assert (p["targets_intended"], p["targets_captured"]) == (3, 2)
        assert any("cm2" in n and "TimeoutExpired" in n for n in p["capture_notes"])

    def test_a_half_parsed_manifest_is_not_restorable(self, monkeypatch):
        """A manifest that dies mid-parse yields a partial object list, not a whole one."""
        broken = _manifest(2) + "\n---\n" + "kind: ConfigMap\n  bad: [indent\n"
        p = _capture(monkeypatch, broken, _all_ok)
        assert p is not None
        assert p["restorable"] is False
        assert any("parse failed" in n for n in p["capture_notes"])

    def test_the_error_text_of_a_failed_get_is_redacted(self, monkeypatch):
        secret = "sk-test-abcdefghijklmnopqrstuvwxyz123456"

        def runner(cmd, **kw):
            if cmd[3] == "cm1":
                return subprocess.CompletedProcess(cmd, 1, "", f"error: token={secret} rejected")
            return _all_ok(cmd, **kw)

        p = _capture(monkeypatch, _manifest(2), runner)
        assert secret not in "\n".join(p["capture_notes"])


class TestTheContractThatMustNotRegress:
    def test_redaction_damage_still_costs_restorability(self, monkeypatch):
        """The original meaning of `restorable` — fidelity — must still hold."""
        def runner(cmd, **kw):
            return subprocess.CompletedProcess(
                cmd, 0, "apiVersion: v1\nkind: Secret\nmetadata:\n  name: s\ndata:\n  password: hunter2\n", ""
            )

        p = _capture(monkeypatch, _manifest(1), runner)
        assert p["restorable"] is False
        assert p["capture_notes"], "a damaged capture must say so"

    def test_it_never_raises_even_when_the_recorder_explodes(self, monkeypatch):
        """Best-effort by contract: a capture failure must not break the mutation path."""
        def boom(*a, **kw):
            raise RuntimeError("flight recorder is down")

        with patch.object(kt.subprocess, "run", _all_ok), \
             patch.object(flight_recorder, "record", boom):
            kt._capture_rollback_point("apply", ["apply", "-f", "-"], _manifest(2), None, {})

    def test_it_never_raises_when_every_fetch_fails(self, monkeypatch):
        def runner(cmd, **kw):
            raise OSError("no kubectl")

        assert _capture(monkeypatch, _manifest(3), runner) is None, (
            "nothing captured means nothing recorded — a rollback point of zero objects "
            "would be worse than none"
        )
