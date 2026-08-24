"""tests/test_a_bare_shadow_count_is_not_a_verdict.py

`kq detector shadow <name>` printed one line — `<name>: N shadow firing(s)` — and that number is
what a reviewer promotes or rejects the candidate on. It was rendered identically whether the
detector had run quietly, had never been loaded by the process answering, or the firing ring had
overflowed and dropped the older half. The server now distinguishes those; this pins that the
CLI stops collapsing them back into one line.

`0 shadow firing(s)` with no caveat has to mean "it was watched and it stayed quiet" — that is
the only reading on which promoting it is a decision rather than a guess.

`COLUMNS` is pinned because rich wraps, and a wrapped caveat splits the phrase being asserted.
"""
from __future__ import annotations

import os

import pytest
import respx
from httpx import Response

from kube_q.cli import detector_cmd

URL = "http://test-server"
EP = f"{URL}/v1/detectors/nl:disk/shadow-findings"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for key in [k for k in os.environ if k.startswith("KUBE_Q_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KUBE_Q_URL", URL)
    monkeypatch.setenv("COLUMNS", "400")


def body(*, watching=True, findings=(), held=0, capacity=500, saturated=False):
    return {"name": "nl:disk", "watching": watching, "findings": list(findings),
            "buffer": {"held": held, "capacity": capacity, "saturated": saturated},
            "durable": False}


def run(payload, status=200, capsys=None):
    respx.get(EP).mock(return_value=Response(status, json=payload))
    rc = detector_cmd.run(["shadow", "nl:disk"])
    return rc, capsys.readouterr().out


def line(out: str, needle: str) -> str:
    hits = [ln for ln in out.splitlines() if needle in ln]
    assert len(hits) == 1, f"expected exactly one line with {needle!r}, got {hits}"
    return hits[0]


FIRING = {"namespace": "default", "object": "pod-1", "evidence": "restarts climbing"}


class TestAQuietDetectorStillReadsAsQuiet:
    @respx.mock
    def test_a_watched_quiet_detector_prints_the_count_with_no_caveat(self, capsys):
        rc, out = run(body(), capsys=capsys)
        assert rc == 0
        assert "0 shadow firing(s)" in out
        assert "not a measurement" not in out

    @respx.mock
    def test_findings_are_still_listed(self, capsys):
        rc, out = run(body(findings=[FIRING], held=1), capsys=capsys)
        assert rc == 0
        assert "1 shadow firing(s)" in out
        assert "default/pod-1" in out


class TestAnUnwatchedDetectorCannotReadAsQuiet:
    @respx.mock
    def test_it_says_the_count_is_not_a_measurement(self, capsys):
        _rc, out = run(body(watching=False), capsys=capsys)
        assert "not a measurement" in out

    @respx.mock
    def test_it_says_which_fact_makes_it_one(self, capsys):
        _rc, out = run(body(watching=False), capsys=capsys)
        assert "has not loaded it as a shadow detector" in line(out, "not a measurement")

    @respx.mock
    def test_the_count_is_still_printed(self, capsys):
        """The caveat annotates the number; it must not replace it."""
        _rc, out = run(body(watching=False), capsys=capsys)
        assert "0 shadow firing(s)" in out

    @respx.mock
    def test_the_exit_code_is_unchanged(self, capsys):
        """`shadow` is documented as 0/1/2 (3 is `new`-only); this is a caveat, not a
        failure mode."""
        rc, _out = run(body(watching=False), capsys=capsys)
        assert rc == 0


class TestASaturatedRingCannotReadAsQuiet:
    @respx.mock
    def test_a_full_buffer_is_called_out(self, capsys):
        _rc, out = run(body(held=500, capacity=500, saturated=True, findings=[FIRING]),
                       capsys=capsys)
        assert "not a measurement" in out
        assert "older firings were dropped" in out

    @respx.mock
    def test_it_names_the_capacity_it_hit(self, capsys):
        _rc, out = run(body(held=4, capacity=4, saturated=True), capsys=capsys)
        assert "4-firing buffer is full" in line(out, "not a measurement")

    @respx.mock
    def test_both_caveats_appear_together_on_one_line(self, capsys):
        _rc, out = run(body(watching=False, saturated=True, capacity=4), capsys=capsys)
        got = line(out, "not a measurement")
        assert "has not loaded it" in got and "buffer is full" in got


class TestAServerThatRefusesToAnswerIsNotAZero:
    @respx.mock
    def test_a_503_does_not_print_a_firing_count(self, capsys):
        _rc, out = run({"detail": "the detector engine is not running"}, status=503,
                       capsys=capsys)
        assert "shadow firing(s)" not in out

    @respx.mock
    def test_a_503_is_a_non_zero_exit(self, capsys):
        rc, _out = run({"detail": "the detector engine is not running"}, status=503,
                       capsys=capsys)
        assert rc != 0

    @respx.mock
    def test_the_servers_own_words_reach_the_operator(self, capsys):
        _rc, out = run({"detail": "the detector engine is not running in this process"},
                       status=503, capsys=capsys)
        assert "not running in this process" in out


class TestAnOlderServerDoesNotGainACaveatItCannotJustify:
    """A server predating this change sends neither key; absent is not `false`."""

    @respx.mock
    def test_a_response_without_the_new_keys_prints_no_caveat(self, capsys):
        _rc, out = run({"name": "nl:disk", "findings": []}, capsys=capsys)
        assert "0 shadow firing(s)" in out
        assert "not a measurement" not in out
