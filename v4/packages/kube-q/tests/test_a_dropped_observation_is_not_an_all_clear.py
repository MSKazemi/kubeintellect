"""A queue that shed observations is a third way to be blind while looking connected.

`kq findings` already refuses the reassuring green line for two failure modes: the watch stream
is not connected (`test_findings_not_watching.py`) and Prometheus could not be queried
(`test_findings_predictive_blind.py`). It missed the sibling in between. `_enqueue` sheds the
**oldest** observation rather than applying backpressure to `kubectl` — a deliberate choice, since
blocking there causes a reconnect storm and a full relist, i.e. more load exactly when the system
is behind — so the loss is silent at the point it happens and `queue.shed_total` is the only
record that it happened at all. `GET /v1/findings` is the only surface that reports it, and its
own comment says so.

Measured 2026-08-24, before the fix: a body with `sensorium: active`, a healthy stream,
`predictive: ok`, no findings and `queue: {"shed_total": 4271}` printed

    No findings · 16 detectors watching

and exited 0 — a green all-clear over 4271 observations that no detector ever saw.

The invariant this file defends is not the wording: **the green line is reachable only when all
three of watching, predicting and not-shedding hold.** It is asserted below over every
combination of the three, so a fourth blindness added later cannot quietly reuse it.
"""
from __future__ import annotations

import itertools
import os

import pytest
import respx
from httpx import Response

from kube_q.cli import findings_cmd

GREEN = "No findings · 16 detectors watching\n"
LOSSY = "Perception is lossy"


@pytest.fixture(autouse=True)
def _clean_kube_q_env(monkeypatch):
    for key in [k for k in os.environ if k.startswith("KUBE_Q_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KUBE_Q_URL", "http://test-server")
    # Rich wraps at the terminal width, which would split one logical line across several and
    # make the per-line assertions below meaningless. Two lines here carry the same numbers, so
    # whole-output assertions cannot attribute them — that is exactly what M6/M10 exploited.
    monkeypatch.setenv("COLUMNS", "400")


def _body(**over) -> dict:
    body = {"sensorium": "active", "detectors": 16, "predictive": "ok",
            "streams": [{"name": "pods", "stopped": False}],
            "queue": {"shed_total": 0, "high_water": 12},
            "findings": []}
    body.update(over)
    return body


def _line(out: str, needle: str) -> str:
    """The one output line containing `needle` — assertions must name the line, not the blob."""
    hits = [ln for ln in out.splitlines() if needle in ln]
    assert len(hits) == 1, f"expected exactly one line containing {needle!r}, got {len(hits)}"
    return hits[0]


def _run(body: dict, capsys) -> tuple[int, str]:
    with respx.mock:
        respx.get("http://test-server/v1/findings").mock(return_value=Response(200, json=body))
        rc = findings_cmd.run([])
    return rc, capsys.readouterr().out


_FINDING = {"playbook": "CrashLoopBackOff", "namespace": "prod", "object": "web-1",
            "evidence": "pod status=CrashLoopBackOff", "fired_at": 1781250000.0}


class TestADroppedObservationIsReported:
    def test_shedding_is_not_reported_as_no_findings(self, capsys):
        rc, out = _run(_body(queue={"shed_total": 4271, "high_water": 512}), capsys)
        assert rc == 0
        assert LOSSY in out, "the only surface that reports shed observations did not report them"
        assert GREEN not in out, "a green all-clear over 4271 observations nobody looked at"
        assert "4271" in _line(out, LOSSY), (
            "the loss was reported without its size — 1 dropped event and 4271 are not the "
            "same finding, and the red line is where the operator reads it")
        assert "may be missing" in _line(out, LOSSY), (
            "the line states a loss without stating its consequence for the list below")
        assert "4271" in _line(out, "not an all-clear"), (
            "the summary line named the loss without its size")

    def test_the_queue_high_water_is_shown(self, capsys):
        _, out = _run(_body(queue={"shed_total": 5, "high_water": 512}), capsys)
        assert "512" in _line(out, "high-water"), (
            "high-water is what says whether the loss is ongoing or a one-off spike")

    def test_a_non_empty_list_is_also_incomplete(self, capsys):
        """Shedding does not only invalidate an EMPTY list — it invalidates the table too."""
        _, out = _run(_body(queue={"shed_total": 9}, findings=[_FINDING]), capsys)
        assert LOSSY in out, "the table was presented as the complete set of firings"
        assert "CrashLoopBackOff" in out, "the findings themselves must still be shown"

    def test_a_healthy_queue_still_gets_the_all_clear(self, capsys):
        rc, out = _run(_body(), capsys)
        assert rc == 0
        assert GREEN in out, "the fix must not turn every quiet cluster into a warning"
        assert LOSSY not in out

    def test_the_two_blindnesses_are_named_together(self, capsys):
        """Two independent caveats must both be named, not just the first one found."""
        _, out = _run(_body(predictive="blind", queue={"shed_total": 7}), capsys)
        summary = _line(out, "not an all-clear")
        assert "blind" in summary, "the summary named only the loss"
        assert "7 observation" in summary, (
            "the summary named only the first caveat; the shed count appears elsewhere in the "
            "output, so asserting over the whole blob would have passed either way")
        assert GREEN not in out


class TestWhatTheServerDidNotSay:
    def test_a_server_that_reports_no_queue_is_not_accused_of_shedding(self, capsys):
        """Absent is not zero and it is not loss either — inventing a number would be a lie.

        A body with no `queue` key is a server that did not tell us. This command's established
        convention (see the `sensorium_reason` fallback) is to say what is known and not name a
        likeliest cause, so it must neither claim loss nor print a count it does not have.
        """
        body = _body()
        del body["queue"]
        rc, out = _run(body, capsys)
        assert rc == 0
        assert LOSSY not in out, "loss was reported for a server that never mentioned a queue"

    @pytest.mark.parametrize("shed", [None, "", 0])
    def test_a_falsy_shed_count_is_not_a_loss(self, shed, capsys):
        _, out = _run(_body(queue={"shed_total": shed}), capsys)
        assert LOSSY not in out


class TestTheGreenLineIsGuardedByEveryBlindness:
    """The property, over all 8 combinations: green iff watching AND predicting AND not shedding.

    Written as a product rather than three cases so that adding a fourth blindness to the caveat
    list cannot leave a combination untested — the failure this file exists to prevent is a
    surface that hardened two failure modes and reused the all-clear for the third.
    """

    @pytest.mark.parametrize(
        ("state", "predictive", "shed"),
        list(itertools.product(("active", "starting"), ("ok", "blind"), (0, 31))),
    )
    def test_green_only_when_nothing_is_wrong(self, state, predictive, shed, capsys):
        healthy = state == "active" and predictive != "blind" and shed == 0
        _, out = _run(_body(sensorium=state, predictive=predictive,
                            queue={"shed_total": shed, "high_water": 1}), capsys)
        if healthy:
            assert GREEN in out, "a fully healthy sensorium lost its all-clear"
        else:
            assert GREEN not in out, (
                f"all-clear printed while state={state} predictive={predictive} shed={shed}")
