"""Every autonomous investigation ends in a typed, recorded outcome, and bad outcomes open a circuit breaker.

Measured 2026-11-14 (`papers/v4-operator-r1/A3-LOOP-INVESTIGATION.md` §3): sessions that looped, faked a fix, or
stopped at an approval prompt nobody could answer all left the same trace — `watchtower_done` and, for the last kind,
an empty report. Now each ends as one of fixed / not_fixed / unknown / budget_exhausted / stuck / needs_approval,
written to the flight recorder; needs_approval names the command that was waiting. not_fixed, stuck and
budget_exhausted demote that (namespace, playbook) from A3 to A2 for AUTOFIX_BREAKER_SECONDS, so the next finding of
the same kind there is proposed to a human instead of retried unattended (Google SRE: bound retries; a failing
dependency gets fewer attempts, not more).
"""

from __future__ import annotations

from unittest import mock

import pytest

from app.autonomy import watchtower as wt
from app.detectors.models import Finding


def _finding(
    ns: str = "b3-c1", playbook: str = "CreateContainerConfigError"
) -> Finding:
    return Finding(
        playbook=playbook,
        cluster_id="c",
        namespace=ns,
        object_name="web-1",
        evidence="x",
    )


@pytest.fixture(autouse=True)
def _allowed(monkeypatch):
    monkeypatch.setattr(wt, "a3_allowed", lambda playbook, ns: True)
    monkeypatch.setattr(wt.settings, "AUTOFIX_BREAKER_SECONDS", 3600)
    wt.reset_breaker()
    yield
    wt.reset_breaker()


@pytest.mark.parametrize("outcome", ["not_fixed", "stuck", "budget_exhausted"])
def test_a_bad_outcome_demotes_that_pair_to_a2(outcome):
    f = _finding()
    assert wt._should_auto_fix(f, "A3") is True
    wt._conclude(f, "auto-1", outcome, {})
    assert wt._should_auto_fix(f, "A3") is False
    assert wt._should_auto_fix(_finding(ns="b3-c2"), "A3") is True, (
        "another namespace is not affected"
    )
    wt.reset_breaker()
    assert wt._should_auto_fix(f, "A3") is True


@pytest.mark.parametrize("outcome", ["fixed", "unknown", "needs_approval"])
def test_other_outcomes_do_not(outcome):
    f = _finding()
    wt._conclude(f, "auto-1", outcome, {})
    assert wt._should_auto_fix(f, "A3") is True


def test_the_breaker_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(wt.settings, "AUTOFIX_BREAKER_SECONDS", 0)
    f = _finding()
    wt._conclude(f, "auto-1", "not_fixed", {})
    assert wt._should_auto_fix(f, "A3") is True


def test_the_outcome_is_written_to_the_flight_recorder():
    with mock.patch("app.db.flight_recorder.record") as rec:
        wt._conclude(
            _finding(),
            "auto-9",
            "needs_approval",
            {"command": "kubectl set image deploy/web app=x"},
        )
    (episode, kind, payload), _ = rec.call_args
    assert episode == "auto-9" and kind == "autonomy_outcome"
    assert payload["outcome"] == "needs_approval" and "set image" in payload["command"]
    assert (
        payload["playbook"] == "CreateContainerConfigError"
        and payload["namespace"] == "b3-c1"
    )


@pytest.mark.asyncio
async def test_a_turn_waiting_at_an_approval_prompt_is_recognised(monkeypatch):
    class _Intr:
        value = {
            "type": "hitl",
            "command": "kubectl set image deployment/web app=busybox:latest",
            "risk_level": "high",
            "always_confirm": True,
        }

    class _Task:
        interrupts = (_Intr(),)

    class _Snap:
        tasks = (_Task(),)
        values: dict = {}

    class _Graph:
        async def aget_state(self, config):
            return _Snap()

    async def _get_graph():
        return _Graph()

    monkeypatch.setattr("app.agent.workflow.get_graph", _get_graph)
    pending = await wt._pending_approval("auto-x")
    assert pending == {
        "command": "kubectl set image deployment/web app=busybox:latest",
        "risk_level": "high",
        "always_confirm": True,
    }


@pytest.mark.asyncio
async def test_a_turn_stopped_at_a_prompt_is_verified_first(monkeypatch):
    """Measured 2026-11-14 (Sonnet 4.6, ladder pilot): the b3-c1 session created its own missing ConfigMap (the fault
    recovered), then tried to write a SIBLING namespace's ConfigMap, which the namespace binding stopped at a prompt.
    Concluding needs_approval hid that the session's own fault was fixed. The workload is checked first."""
    from app.autonomy import verify

    concluded = []
    monkeypatch.setattr(wt, "_conclude", lambda f, s, o, d: concluded.append((o, d)))
    pending = {
        "command": "kubectl apply -f -",
        "risk_level": "medium",
        "always_confirm": False,
    }

    async def healthy(target, timeout):
        return "fixed"

    async def broken(target, timeout):
        return "not_fixed"

    monkeypatch.setattr(verify, "verify", healthy)
    await wt._conclude_pending(_finding(), object(), "auto-p", pending)
    assert (
        concluded[-1][0] == "fixed"
        and concluded[-1][1]["pending_command"] == "kubectl apply -f -"
    )

    monkeypatch.setattr(verify, "verify", broken)
    await wt._conclude_pending(_finding(), object(), "auto-p", pending)
    assert concluded[-1] == ("needs_approval", pending)
