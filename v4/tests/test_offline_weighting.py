"""ADR-102 arm-3 offline-shadow weighting — offline events down-weight the LCB."""
from __future__ import annotations

from app.autonomy.promotion_stats import Event, weighted_lcb, wilson_lcb


def _events(n, *, offline, success=True):
    return [Event(ts_days=float(i), success=success, incident_id=f"i{i}", offline=offline)
            for i in range(n)]


def test_all_live_matches_plain_lcb():
    ev = _events(40, offline=False)
    assert abs(weighted_lcb(ev, offline_weight=0.5) - wilson_lcb(40, 40)) < 1e-9


def test_offline_events_lower_effective_certainty():
    live = weighted_lcb(_events(40, offline=False), offline_weight=0.5)
    offline = weighted_lcb(_events(40, offline=True), offline_weight=0.5)
    # same successes/attempts, but offline evidence ⇒ lower effective n ⇒ lower LCB
    assert offline < live


def test_weight_zero_ignores_offline_only():
    # all-offline with weight 0 ⇒ effective n=0 ⇒ LCB 0
    assert weighted_lcb(_events(30, offline=True), offline_weight=0.0) == 0.0


def test_mixed_between_live_and_offline():
    mixed = _events(20, offline=False) + _events(20, offline=True)
    val = weighted_lcb(mixed, offline_weight=0.5)
    assert weighted_lcb(_events(40, offline=True), offline_weight=0.5) < val < wilson_lcb(40, 40)


def test_weight_clamped_to_unit():
    # weight >1 is clamped ⇒ same as all-live
    hi = weighted_lcb(_events(40, offline=True), offline_weight=5.0)
    assert abs(hi - wilson_lcb(40, 40)) < 1e-9


def test_failures_reduce_lcb():
    good = weighted_lcb(_events(40, offline=False, success=True), offline_weight=0.5)
    bad = weighted_lcb(_events(20, offline=False, success=True)
                       + _events(20, offline=False, success=False), offline_weight=0.5)
    assert bad < good


class TestArm3Calibration:
    def test_perfect_agreement_hits_cap(self):
        from app.autonomy.promotion_stats import calibrate_offline_weight
        m = [(True, True), (False, False), (True, True)]   # offline always matches live
        assert calibrate_offline_weight(m, cap=0.5) == 0.5

    def test_zero_agreement_zero_weight(self):
        from app.autonomy.promotion_stats import calibrate_offline_weight
        m = [(True, False), (False, True)]                 # offline always wrong
        assert calibrate_offline_weight(m, cap=0.5) == 0.0

    def test_partial_agreement_below_cap(self):
        from app.autonomy.promotion_stats import calibrate_offline_weight
        m = [(True, True), (True, False), (False, False), (False, False)]  # 3/4 = 0.75, capped 0.5
        assert calibrate_offline_weight(m, cap=0.5) == 0.5
        m2 = [(True, True), (True, False), (False, True), (False, False)]  # 2/4 = 0.5
        assert calibrate_offline_weight(m2, cap=0.9) == 0.5

    def test_empty_zero(self):
        from app.autonomy.promotion_stats import calibrate_offline_weight
        assert calibrate_offline_weight([], cap=0.5) == 0.0
