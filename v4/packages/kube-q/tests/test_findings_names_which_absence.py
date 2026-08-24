"""`kq findings` must name WHICH absence, not print one word for four situations.

The server classifies a missing detector engine four ways — switched off, no compiled
detectors, a FAILED start, and a leader-election standby — and sends the sentence in
`sensorium_reason`. Before this file the CLI ignored it and printed
`Sensorium is disabled on this server.` for all four, which is a false statement for the
last two: a crashed sensorium is an outage, not a setting, and a standby replica is
behaving correctly while another replica perceives.

The property under test is *discrimination*: four different causes must produce four
different lines, and the line must not assert a cause the server did not report.
"""
from __future__ import annotations

import os
import re

import pytest
import respx
from httpx import Response

from kube_q.cli import findings_cmd

# The exact sentences `app/detectors/perception.py::_absence_phrase` emits.
FLAG = ("the sensorium is switched off (SENSORIUM_ENABLED=false) — no detector "
        "finding could have been produced")
NO_DETECTORS = ("the sensorium loaded no compiled detectors, so it did not start — no "
                "detector finding could have been produced")
START_FAILED = ("the sensorium FAILED to start (pods is forbidden: RBAC) — this replica "
                "has been perceiving nothing since, and this is an outage rather than a "
                "setting")
STANDBY = ("this replica is a leader-election standby and watches nothing by design; the "
           "replica holding the singleton lock is the one that perceives, so read its "
           "findings, not this replica's silence")

ALL_REASONS = [FLAG, NO_DETECTORS, START_FAILED, STANDBY]


@pytest.fixture(autouse=True)
def _clean_kube_q_env(monkeypatch):
    for key in [k for k in os.environ if k.startswith("KUBE_Q_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")


def _flat(text: str) -> str:
    """rich hard-wraps at the console width, so a sentence is split across lines."""
    return re.sub(r"\s+", " ", text).strip()


def _run(capsys, payload: dict) -> tuple[int, str]:
    respx.get("http://test-server/v1/findings").mock(return_value=Response(200, json=payload))
    rc = findings_cmd.run([])
    return rc, _flat(capsys.readouterr().out)


def _disabled(reason: str | None) -> dict:
    payload: dict = {"sensorium": "disabled", "detectors": 0, "findings": []}
    if reason is not None:
        payload["sensorium_reason"] = reason
    return payload


# ── the property that was broken ───────────────────────────────────────────────

@respx.mock
@pytest.mark.parametrize("reason", ALL_REASONS)
def test_the_servers_sentence_is_printed_verbatim(capsys, reason):
    rc, out = _run(capsys, _disabled(reason))
    assert rc == 0
    assert _flat(reason) in out, out


@respx.mock
def test_four_causes_print_four_different_lines(capsys):
    seen = set()
    for reason in ALL_REASONS:
        with respx.mock:
            _, out = _run(capsys, _disabled(reason))
        seen.add(out)
    assert len(seen) == len(ALL_REASONS), f"only {len(seen)} distinct outputs: {seen}"


@respx.mock
def test_a_failed_start_is_not_called_disabled(capsys):
    """The old line said "disabled". A sensorium that crashed was never disabled."""
    rc, out = _run(capsys, _disabled(START_FAILED))
    assert rc == 0
    assert "FAILED to start" in out
    assert "outage rather than a setting" in out
    assert "disabled on this server" not in out


@respx.mock
def test_a_standby_replica_is_not_blamed_on_the_flag(capsys):
    """A standby is correct behaviour: never name SENSORIUM_ENABLED, and point at the
    replica that IS perceiving."""
    _, out = _run(capsys, _disabled(STANDBY))
    assert "SENSORIUM_ENABLED" not in out
    assert "leader-election standby" in out
    assert "read its findings" in out


@respx.mock
def test_the_flag_case_still_names_the_flag(capsys):
    """The discrimination has to cut both ways — when it IS the flag, say so."""
    _, out = _run(capsys, _disabled(FLAG))
    assert "SENSORIUM_ENABLED=false" in out
    assert "outage" not in out
    assert "standby" not in out


# ── the server that cannot say ─────────────────────────────────────────────────

@respx.mock
@pytest.mark.parametrize("payload", [_disabled(None), _disabled("")])
def test_an_older_server_gets_an_honest_dont_know(capsys, payload):
    """A server predating `sensorium_reason` cannot say which of the four it is. Neither
    can the CLI — so it must say that, not pick the likeliest cause."""
    rc, out = _run(capsys, payload)
    assert rc == 0
    assert "does not report why" in out
    assert "does NOT mean the cluster is healthy" in out
    assert "SENSORIUM_ENABLED" not in out
    assert "outage" not in out


# ── nothing else moved ─────────────────────────────────────────────────────────

@respx.mock
def test_a_perceiving_sensorium_prints_no_reason_at_all(capsys):
    """Vacuity guard the other way: the reason line must not appear when nothing is
    absent, or the caveat becomes background noise operators learn to ignore."""
    rc, out = _run(capsys, {"sensorium": "active", "sensorium_reason": "",
                            "detectors": 16, "findings": []})
    assert rc == 0
    assert "not perceiving" not in out
    assert "No findings" in out


@respx.mock
def test_a_stopped_stream_still_takes_the_not_watching_path(capsys):
    """`disabled` is one of several non-active states; the others are unchanged."""
    rc, out = _run(capsys, {"sensorium": "stopped", "detectors": 16, "findings": [],
                            "streams": [{"name": "pods", "stopped": True,
                                         "last_error": "kubectl not found"}]})
    assert rc == 0
    assert "Sensorium is not watching" in out
    assert "kubectl not found" in out


@respx.mock
def test_findings_still_render_when_the_sensorium_is_absent(capsys):
    """A disabled sensorium returns early, so anything already recorded would be hidden.
    Pin today's behaviour so a future change to the early return is a deliberate one."""
    rc, out = _run(capsys, _disabled(FLAG))
    assert rc == 0
    assert "SENSORIUM_ENABLED=false" in out


# ── vacuity guards on the fixtures themselves ──────────────────────────────────

def test_the_four_fixture_sentences_are_actually_different():
    assert len(set(ALL_REASONS)) == 4


def test_the_fixture_sentences_match_the_server_source():
    """If `_absence_phrase` is reworded, this file's fixtures go stale and every
    assertion above would keep passing against text the server no longer sends."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[3] / (
        "packages/kubeintellect-server/app/detectors/perception.py")
    if not src.exists():  # kube-q is installable on its own
        pytest.skip("server source not present in this checkout")
    body = _flat(src.read_text())
    for probe in ("the sensorium is switched off (SENSORIUM_ENABLED=false)",
                  "the sensorium loaded no compiled detectors",
                  "replica has been perceiving nothing since",  # split literal in source
                  "leader-election standby and watches nothing by design"):
        assert probe in body, f"server no longer emits: {probe!r}"
