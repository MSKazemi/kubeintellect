"""An autonomous investigation whose model call failed ends as `error`, not as a fix that did not work.

Measured 2026-11-14 (ladder pilot, Haiku 4.5, run 2): during a DNS outage every session's model call failed at once
(an `error` event, $0 spent). The watchtower then verified the untouched workloads and recorded `not_fixed` for
each, which reads as "the agent tried and failed" and opened the A3 breaker for every namespace. An unreachable model
says nothing about whether the fix approach works: the outcome is `error`, and it does not open the breaker.
"""

from __future__ import annotations

import pytest

from app.agent import workflow
from app.autonomy import watchtower as wt
from app.detectors.models import Finding


@pytest.mark.asyncio
async def test_a_failed_model_call_is_recorded_as_error_and_does_not_trip_the_breaker(
    monkeypatch,
):
    monkeypatch.setattr(wt, "a3_allowed", lambda playbook, ns: True)
    monkeypatch.setattr(wt.settings, "AUTO_APPROVE_ENABLED", True)
    monkeypatch.setattr(wt.settings, "AUTOFIX_BREAKER_SECONDS", 3600)
    concluded = []
    real = wt._conclude
    monkeypatch.setattr(
        wt, "_conclude", lambda f, s, o, d: (concluded.append((o, d)), real(f, s, o, d))
    )

    async def failing_turn(ask, session_id, **kw):
        workflow._record_turn_error(
            session_id, "The model provider could not be reached"
        )

    monkeypatch.setattr(wt, "_run_turn", failing_turn)
    finding = Finding(
        playbook="CreateContainerConfigError",
        cluster_id="c",
        namespace="b3-c1",
        object_name="web-1",
        evidence="x",
    )
    await wt._investigate(finding, "A3")
    assert concluded and concluded[-1][0] == "error"
    assert "could not be reached" in concluded[-1][1]["error"]
    assert wt._should_auto_fix(finding, "A3") is True, (
        "an unreachable model must not demote A3"
    )


def test_the_error_is_read_once():
    workflow._record_turn_error("auto-e", "boom")
    assert workflow.pop_turn_error("auto-e") == "boom"
    assert workflow.pop_turn_error("auto-e") is None
