"""Unit tests for the Wilson-LCB autonomy-promotion criterion (v5 ADR-102)."""

from __future__ import annotations

import pytest

from app.autonomy.promotion_stats import (
    Event,
    evaluate_promotion,
    rule_for,
    wilson_lcb,
)


# ── Wilson lower bound: known properties ──────────────────────────────────────
def test_wilson_lcb_zero_n_is_zero():
    assert wilson_lcb(0, 0) == 0.0


def test_wilson_lcb_below_point_estimate_and_emergent_n():
    # Perfect record: LCB rises with n (≈ 1 - 3/n heuristic from ADR-102).
    lcb30 = wilson_lcb(30, 30)
    lcb60 = wilson_lcb(60, 60)
    lcb300 = wilson_lcb(300, 300)
    assert lcb30 < lcb60 < lcb300 < 1.0
    assert lcb30 >= 0.85          # ~0.88 at n=30
    assert lcb60 >= 0.93          # clears 0.90 by n≈60
    assert lcb300 >= 0.98         # clears 0.99 by n≈300


def test_wilson_lcb_penalizes_failures():
    assert wilson_lcb(90, 100) < wilson_lcb(99, 100)


def _events(n, *, success=True, types=None, incidents=None, day_span=40.0, critical_at=None):
    types = types or ["t0", "t1", "t2", "t3", "t4"]
    out = []
    for i in range(n):
        out.append(Event(
            ts_days=(day_span * i / max(n - 1, 1)),
            success=success,
            incident_id=f"inc-{i}" if incidents is None else incidents[i % len(incidents)],
            incident_type=types[i % len(types)],
            critical=(critical_at is not None and i == critical_at),
        ))
    return out


# ── irreversible is a hard ceiling ────────────────────────────────────────────
def test_irreversible_never_promotes():
    d = evaluate_promotion("L3->L4:irreversible", _events(1000), now_days=100)
    assert d.promote is False
    assert "irreversible" in d.reasons[0]


# ── happy path: enough clean, diverse, aged evidence promotes ─────────────────
def test_l2_l3_promotes_with_sufficient_clean_evidence():
    d = evaluate_promotion("L2->L3", _events(40, day_span=40.0), now_days=40.0)
    assert d.reasons == []
    assert d.promote is True
    assert d.lcb >= d.theta


# ── each gate blocks independently ────────────────────────────────────────────
def test_insufficient_n_blocks():
    d = evaluate_promotion("L2->L3", _events(5, day_span=40.0), now_days=40.0)
    assert d.promote is False
    assert any("n_min" in r for r in d.reasons)


def test_insufficient_time_span_blocks():
    d = evaluate_promotion("L2->L3", _events(40, day_span=2.0), now_days=2.0)
    assert d.promote is False
    assert any("T_min" in r for r in d.reasons)


def test_low_success_rate_blocks_on_lcb():
    evs = _events(40, day_span=40.0)
    # flip half to failures → LCB well below θ
    for i in range(0, 40, 2):
        evs[i] = Event(ts_days=evs[i].ts_days, success=False, incident_id=evs[i].incident_id,
                       incident_type=evs[i].incident_type)
    d = evaluate_promotion("L2->L3", evs, now_days=40.0)
    assert d.promote is False
    assert any("θ" in r or "LCB" in r for r in d.reasons)


def test_insufficient_incident_diversity_blocks():
    # 40 events but only 2 distinct incidents and 1 type → diversity gates fail
    evs = _events(40, day_span=40.0, incidents=["a", "b"], types=["only"])
    d = evaluate_promotion("L2->L3", evs, now_days=40.0)
    assert d.promote is False
    assert any("distinct" in r for r in d.reasons)


def test_critical_event_blocks_M4():
    d = evaluate_promotion("L2->L3", _events(40, day_span=40.0, critical_at=10), now_days=40.0)
    assert d.promote is False
    assert any("critical" in r for r in d.reasons)


# ── window bounds the evidence (old events fall out) ──────────────────────────
def test_events_outside_90d_window_excluded():
    old = _events(40, day_span=40.0)  # spans days 0..40
    d = evaluate_promotion("L2->L3", old, now_days=200.0)  # all >90d old
    assert d.n == 0
    assert d.promote is False


def test_rule_table_has_all_transitions():
    for t in ["L1->L2", "L2->L3", "L3->L4:versioned-workload",
              "L3->L4:declarative-revert", "L3->L4:irreversible"]:
        assert rule_for(t) is not None


def test_unknown_transition_raises():
    with pytest.raises(KeyError):
        rule_for("L9->L10")


# ── Demotion (ADR-102 asymmetric fast-down) ───────────────────────────────────
from app.autonomy.promotion_stats import (  # noqa: E402
    DemotionDecision,
    cusum_trip,
    evaluate_demotion,
    hysteresis_breach,
)


def test_sev_attribution_at_l4_drops_two_rungs_and_freezes():
    d = evaluate_demotion("L4", 0.99, [], now_days=0.0, sev_attributed=True)
    assert d.demote is True
    assert (d.from_rung, d.to_rung) == ("L4", "L2")
    assert d.fleet_freeze_days == 14


def test_sev_attribution_below_l4_does_not_trigger_two_rung_rule():
    # sev flag only two-rung-drops at L4; at L3 it is not this trigger
    d = evaluate_demotion("L3", 0.95, [], now_days=0.0, sev_attributed=True)
    assert (d.demote, d.to_rung) != (True, "L1")  # not the 2-rung sev path


def test_m4_at_l4_immediate_demote_to_l3():
    d = evaluate_demotion("L4", 0.99, [], now_days=0.0, m4_at_l4=True)
    assert d.demote is True and d.to_rung == "L3"


def test_class_drift_flags_stale_not_demote():
    d = evaluate_demotion("L3", 0.95, [], now_days=0.0, class_drift=True)
    assert d.stale is True and d.demote is False


def test_cusum_trips_on_two_failures_within_24h():
    evs = [Event(ts_days=1.0, success=False, incident_id="a"),
           Event(ts_days=1.5, success=False, incident_id="b")]  # 0.5d apart < 1d
    assert cusum_trip(evs) is True


def test_cusum_does_not_trip_when_failures_are_spread_out():
    evs = [Event(ts_days=1.0, success=False, incident_id="a"),
           Event(ts_days=5.0, success=False, incident_id="b")]  # 4d apart
    assert cusum_trip(evs) is False


def test_cusum_trip_demotes_one_rung():
    evs = [Event(ts_days=1.0, success=False, incident_id="a"),
           Event(ts_days=1.2, success=False, incident_id="b")]
    d = evaluate_demotion("L4", 0.99, evs, now_days=2.0)
    assert d.demote is True and d.to_rung == "L3"
    assert "CUSUM" in d.reason


def test_hysteresis_breach_when_last50_lcb_falls_below_band():
    # 50 events at ~60% success → LCB well below θ-0.05 for θ=0.90
    evs = [Event(ts_days=float(i), success=(i % 5 != 0), incident_id=f"i{i}") for i in range(50)]
    assert hysteresis_breach(evs, 0.90) is True


def test_hysteresis_no_breach_when_healthy():
    evs = [Event(ts_days=float(i), success=True, incident_id=f"i{i}") for i in range(50)]
    assert hysteresis_breach(evs, 0.90) is False


def test_no_trigger_returns_no_demotion():
    evs = [Event(ts_days=float(i), success=True, incident_id=f"i{i}") for i in range(50)]
    d = evaluate_demotion("L4", 0.90, evs, now_days=50.0)
    assert d.demote is False and isinstance(d, DemotionDecision)
