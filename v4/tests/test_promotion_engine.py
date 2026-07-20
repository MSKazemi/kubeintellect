"""Statistical autonomy promotion engine (v5 P3, ADR-102) — promote/hold/demote decisions."""
from __future__ import annotations

import pytest

from app.autonomy.promotion_engine import (
    _empty_source,
    decide,
    outcomes_for,
    set_outcome_source,
)
from app.autonomy.promotion_stats import Event


def _successes(n: int, *, incidents: int = 6, types: int = 1, days: float = 10.0, critical: bool = False):
    """n success Events across `incidents` distinct incidents / `types` types, spread over `days`."""
    out = []
    for i in range(n):
        ts = (i / (n - 1)) * days if n > 1 else 0.0
        out.append(Event(ts_days=ts, success=True, incident_id=f"inc{i % incidents}",
                         incident_type=f"t{i % types}", critical=critical))
    return out


@pytest.fixture(autouse=True)
def reset_source():
    set_outcome_source(_empty_source)
    yield
    set_outcome_source(_empty_source)


class TestPromote:
    def test_strong_record_promotes_l1_to_l2(self):
        # 60 all-success across 6 incidents / 10 days ⇒ LCB≈0.957 ≥ θ0.95, n≥20, span≥7, incidents≥5.
        d = decide("misconfig-fix", "L1->L2", "L1", now_days=10.0, events=_successes(60))
        assert d.action == "promote" and d.to_rung == "L2"
        assert d.lcb >= 0.95 and d.n == 60


class TestHold:
    def test_insufficient_timespan_holds(self):
        # Strong LCB (no demotion) but all on one day ⇒ promotion holds on the T_min gate.
        d = decide("misconfig-fix", "L1->L2", "L1", now_days=10.0, events=_successes(60, days=0.0))
        assert d.action == "hold" and d.to_rung == "L1"
        assert any("T_min" in r or "time span" in r for r in d.reasons)

    def test_empty_holds(self):
        d = decide("misconfig-fix", "L1->L2", "L1", now_days=10.0, events=[])
        assert d.action == "hold" and d.to_rung == "L1"


class TestDemote:
    def test_sev_at_l4_demotes_two_rungs(self):
        d = decide("misconfig-fix", "L3->L4:versioned-workload", "L4", now_days=10.0,
                   events=_successes(60), sev_attributed=True)
        assert d.action == "demote" and d.to_rung == "L2"   # L4 → drop 2

    def test_cusum_failures_demote(self):
        evts = _successes(40)
        # two postcondition failures within 24h ⇒ CUSUM trip
        evts += [Event(ts_days=5.0, success=False, incident_id="f1"),
                 Event(ts_days=5.5, success=False, incident_id="f2")]
        d = decide("misconfig-fix", "L2->L3", "L3", now_days=10.0, events=evts)
        assert d.action == "demote"

    def test_class_drift_holds_stale(self):
        d = decide("misconfig-fix", "L1->L2", "L1", now_days=10.0, events=_successes(60),
                   class_drift=True)
        assert d.action == "hold" and "drift" in d.reasons[0].lower()


class TestPrecedence:
    def test_demotion_wins_over_promotion(self):
        # A promote-worthy record BUT a sev-1 at L4 ⇒ fast-down beats slow-up.
        d = decide("misconfig-fix", "L3->L4:versioned-workload", "L4", now_days=10.0,
                   events=_successes(80), sev_attributed=True)
        assert d.action == "demote"


class TestSource:
    def test_default_source_empty(self):
        assert outcomes_for("any") == []

    def test_registered_source_used(self):
        set_outcome_source(lambda ac: _successes(3) if ac == "known" else [])
        assert len(outcomes_for("known")) == 3
        assert outcomes_for("other") == []

    def test_source_exception_safe(self):
        def boom(ac):
            raise RuntimeError("recorder down")
        set_outcome_source(boom)
        assert outcomes_for("x") == []

    def test_decide_reads_source_when_events_omitted(self):
        set_outcome_source(lambda ac: _successes(60))
        d = decide("misconfig-fix", "L1->L2", "L1", now_days=10.0)   # no events kwarg
        assert d.action == "promote"
