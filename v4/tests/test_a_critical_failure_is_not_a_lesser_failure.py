"""The demotion path must never treat a critical failure more leniently than an ordinary one.

ADR §4.4 lists five demotion triggers. Two of them — the Sev-1/2 two-rung drop and the M4 rule —
are L4-scoped by the ADR's own wording. Below L4 that leaves the CUSUM fast trip and the
hysteresis band. `cusum_trip` counted `not e.success and not e.critical`, so the failures those
two L4 rules exist for were the only ones the fast trigger could not see.

Measured 2026-08-24 on an L3 class with 48 clean runs and then two failures 12 h apart:

    ordinary postcondition failures    -> CUSUM trips, L3 -> L2
    the same two, critical=True        -> "no demotion trigger", stays L3

Hysteresis cannot cover the gap: a class with an earned record keeps its last-50 LCB above
θ − 0.05 through exactly two failures, which is why the trip exists. Promotion already treats a
critical event as disqualifying (`M4 > 0`); demotion treated it as exculpatory.
"""
from __future__ import annotations

import pytest

from app.autonomy.promotion_stats import (
    CUSUM_FAILS,
    RUNGS,
    Event,
    cusum_trip,
    evaluate_demotion,
    evaluate_promotion,
    hysteresis_breach,
)


def _ev(day: float, *, ok: bool = True, critical: bool = False, i: int = 0) -> Event:
    return Event(ts_days=day, success=ok, incident_id=f"inc-{i}", incident_type="oom",
                 critical=critical)


def _earned(n: int = 48) -> list[Event]:
    """A class with a real track record — the only kind that ever reaches L3."""
    return [_ev(float(d), i=d) for d in range(n)]


def _two_failures(*, critical: bool) -> list[Event]:
    return _earned() + [_ev(50.0, ok=False, critical=critical, i=98),
                        _ev(50.5, ok=False, critical=critical, i=99)]


class TestTheFastTripSeesEveryFailure:
    def test_two_ordinary_failures_trip_cusum(self):
        """Control: the trigger works, so a miss below is about severity, not about the trip."""
        assert cusum_trip(_two_failures(critical=False)) is True

    def test_two_critical_failures_also_trip_cusum(self):
        assert cusum_trip(_two_failures(critical=True)) is True, (
            "the failures that caused critical incidents were invisible to the fast trigger")

    def test_a_critical_failure_still_demotes_an_earned_l3_class(self):
        d = evaluate_demotion("L3", 0.90, _two_failures(critical=True), now_days=51.0)
        assert d.demote is True and d.to_rung == "L2", (
            f"an L3 class that caused two Sev-1 incidents in 12 h was left at its rung: {d.reason!r}")

    def test_hysteresis_could_not_have_covered_this(self):
        """Non-vacuity: proves the CUSUM change is load-bearing, not redundant."""
        assert hysteresis_breach(_two_failures(critical=True), 0.90) is False, (
            "hysteresis now fires here, so this test no longer proves the gap it was written for")

    def test_one_failure_is_still_not_a_trip(self):
        evs = _earned() + [_ev(50.0, ok=False, critical=True, i=98)]
        assert cusum_trip(evs) is False, "the counter no longer counts, it just fires"

    def test_failures_outside_the_window_still_do_not_trip(self):
        evs = _earned() + [_ev(50.0, ok=False, critical=True, i=98),
                           _ev(52.0, ok=False, critical=True, i=99)]
        assert cusum_trip(evs) is False, "the 24 h window stopped being a window"

    def test_a_critical_event_that_succeeded_is_not_a_failure(self):
        """`critical` alone must not manufacture a postcondition failure."""
        evs = _earned() + [_ev(50.0, ok=True, critical=True, i=98),
                           _ev(50.5, ok=True, critical=True, i=99)]
        assert cusum_trip(evs) is False

    def test_the_number_of_failures_required_matches_the_constant(self):
        evs = _earned() + [_ev(50.0 + 0.1 * k, ok=False, critical=True, i=90 + k)
                           for k in range(CUSUM_FAILS)]
        assert cusum_trip(evs) is True
        assert cusum_trip(evs[:-1]) is False


class TestSeverityMonotonicity:
    """The property the bug violated, stated directly and checked at every rung."""

    @pytest.mark.parametrize("rung", ["L1", "L2", "L3", "L4"])
    def test_critical_failures_are_never_punished_more_leniently(self, rung: str):
        ordinary = evaluate_demotion(rung, 0.90, _two_failures(critical=False), now_days=51.0)
        crit = evaluate_demotion(rung, 0.90, _two_failures(critical=True), now_days=51.0)
        assert RUNGS.index(crit.to_rung) <= RUNGS.index(ordinary.to_rung), (
            f"at {rung}, making the same two failures critical moved the class from "
            f"{ordinary.to_rung} to {crit.to_rung} — a worse outcome earned a better rung")

    @pytest.mark.parametrize("rung", ["L1", "L2", "L3", "L4"])
    def test_critical_failures_never_turn_a_demotion_into_a_pass(self, rung: str):
        ordinary = evaluate_demotion(rung, 0.90, _two_failures(critical=False), now_days=51.0)
        crit = evaluate_demotion(rung, 0.90, _two_failures(critical=True), now_days=51.0)
        assert crit.demote >= ordinary.demote, f"at {rung}: {crit.reason!r}"


class TestTheAuditTrailNamesTheSeverity:
    def test_the_cusum_reason_says_a_critical_incident_occurred(self):
        d = evaluate_demotion("L3", 0.90, _two_failures(critical=True), now_days=51.0)
        assert "critical incident" in d.reason, (
            f"the audit trail records a routine trip where a Sev-1 happened: {d.reason!r}")

    def test_the_cusum_reason_does_not_say_it_when_it_did_not_happen(self):
        d = evaluate_demotion("L3", 0.90, _two_failures(critical=False), now_days=51.0)
        assert "critical" not in d.reason, (
            f"every trip now claims a critical incident: {d.reason!r}")


class TestAReportedSeveritySignalIsNeverDiscardedInSilence:
    """ADR §4.4 scopes the 2-rung drop to L4. Below it, the signal still may not vanish."""

    def test_sev_attribution_below_l4_does_not_report_a_quiet_class(self):
        d = evaluate_demotion("L3", 0.90, _earned(50), now_days=51.0, sev_attributed=True)
        assert d.reason != "no demotion trigger", (
            "a Sev-1 was attributed to this class and the decision reported nothing happened")
        assert "escalate" in d.reason

    def test_m4_below_l4_does_not_report_a_quiet_class(self):
        d = evaluate_demotion("L2", 0.90, _earned(50), now_days=51.0, m4_at_l4=True)
        assert "M4 critical" in d.reason and "escalate" in d.reason

    def test_both_signals_are_both_named(self):
        d = evaluate_demotion("L2", 0.90, _earned(50), now_days=51.0,
                              sev_attributed=True, m4_at_l4=True)
        assert "Sev-1/2 attribution" in d.reason and "M4 critical" in d.reason

    def test_the_message_never_calls_the_class_clean(self):
        d = evaluate_demotion("L3", 0.90, _earned(50), now_days=51.0, sev_attributed=True)
        assert "NOT a clean class" in d.reason

    def test_the_rung_is_still_not_changed_below_l4(self):
        """It reports honestly; it does not invent a policy the ADR does not state."""
        d = evaluate_demotion("L3", 0.90, _earned(50), now_days=51.0, sev_attributed=True)
        assert d.demote is False and d.to_rung == "L3"

    def test_a_genuinely_clean_class_still_says_no_trigger(self):
        d = evaluate_demotion("L3", 0.90, _earned(50), now_days=51.0)
        assert d.demote is False and d.reason == "no demotion trigger", (
            f"every clean class now carries an escalation notice: {d.reason!r}")


class TestTheAdrPrecedenceIsUnchanged:
    """Guard the four rules this pass did not intend to touch."""

    def test_sev_at_l4_still_drops_two_rungs_and_freezes(self):
        d = evaluate_demotion("L4", 0.95, _two_failures(critical=True), now_days=51.0,
                              sev_attributed=True)
        assert d.to_rung == "L2" and d.fleet_freeze_days == 14

    def test_m4_at_l4_still_demotes_to_l3(self):
        d = evaluate_demotion("L4", 0.95, _earned(50), now_days=51.0, m4_at_l4=True)
        assert d.to_rung == "L3" and d.demote is True

    def test_class_drift_still_outranks_the_cusum_trip(self):
        d = evaluate_demotion("L3", 0.90, _two_failures(critical=True), now_days=51.0,
                              class_drift=True)
        assert d.stale is True and d.demote is False

    def test_promotion_still_treats_a_critical_as_disqualifying(self):
        """The asymmetry runs one way only: critical blocks promotion AND forces demotion."""
        p = evaluate_promotion("L2->L3", _two_failures(critical=True), now_days=51.0)
        assert p.promote is False and any("critical" in r for r in p.reasons)
