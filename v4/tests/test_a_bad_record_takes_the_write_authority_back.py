"""The recorded record can close the A3 gate — and cannot open it.

Pass 269 gave `promotion_outcomes` a writer; the loop still had no reader, so a class whose
agreement had collapsed kept writing to the cluster forever. This wires the read side, in the one
direction these samples honestly support.

Two properties, and the second is the load-bearing one:

  * **revocation works** — CUSUM (two postcondition failures within 24 h) closes the A3 gate for
    `watchtower-autofix`, at any sample size, behind `KI_V5_STATISTICAL_PROMOTION`;
  * **promotion does not** — a clean record never *opens* a gate the allowlist left shut. Every
    sample comes from a fix the watchtower was already permitted to make, so earning authority
    from them would be circular, and gating the write on them would deadlock a class with no
    samples out of ever producing one.

Plus the small-n defect that had to be fixed before any of this could be trusted: the anti-flap
band demoted a class with a flawless record for the crime of being young, while a class with no
record at all was safe.
"""
from __future__ import annotations

import pytest

from app.autonomy import promotion_source, watchtower
from app.autonomy.promotion_stats import (
    Event,
    evaluate_demotion,
    hysteresis_breach,
    wilson_lcb,
)
from app.memory import service


def clean(n: int, *, start: float = 0.0, step: float = 1.0) -> list[Event]:
    return [Event(ts_days=start + i * step, success=True, incident_id=f"inc-{i}",
                  incident_type="CrashLoopBackOff") for i in range(n)]


class TestTheBandOnlyTripsOnFailures:
    """`hysteresis_breach` measured sample size as if it were failure. Measured 2026-08-28."""

    @pytest.mark.parametrize("n", [1, 5, 10, 15])
    def test_a_flawless_short_record_is_not_a_breach(self, n):
        assert wilson_lcb(n, n) < 0.95 - 0.05          # the bound really is under the band …
        assert hysteresis_breach(clean(n), 0.95) is False   # … and that is no longer a demotion

    def test_a_flawless_record_is_never_demoted_at_any_length(self):
        for n in range(0, 60):
            decision = evaluate_demotion("L4", 0.95, clean(n), 100.0)
            assert decision.demote is False, f"{n} consecutive successes demoted the class"

    def test_evidence_of_competence_is_no_longer_worse_than_no_evidence(self):
        """The inverted ordering, stated as a test: an empty record was safe, a perfect one was not."""
        assert evaluate_demotion("L4", 0.95, [], 100.0).demote is False
        assert evaluate_demotion("L4", 0.95, clean(15), 100.0).demote is False

    def test_a_real_failure_rate_still_breaches_once_the_band_is_reachable(self):
        events = clean(40) + [Event(ts_days=41.0 + i, success=False, incident_id=f"bad-{i}")
                              for i in range(10)]
        assert hysteresis_breach(events, 0.95) is True

    def test_the_size_independent_fast_trigger_is_untouched(self):
        """CUSUM is what catches a genuinely bad young class; abstaining must not blunt it."""
        events = [Event(ts_days=1.0, success=False, incident_id="a"),
                  Event(ts_days=1.4, success=False, incident_id="b")]
        assert evaluate_demotion("L4", 0.95, events, 2.0).demote is True


class FakePool:
    def __init__(self, events: list[Event] | None = None, raises: Exception | None = None):
        self._events = events or []
        self._raises = raises

    async def fetch(self, sql, *args):
        if self._raises:
            raise self._raises
        return [{"ts_days": e.ts_days, "success": e.success, "incident_id": e.incident_id,
                 "incident_type": e.incident_type, "critical": e.critical} for e in self._events]


class TestAutofixRevocation:
    async def test_a_clean_record_keeps_the_authority(self):
        assert await promotion_source.autofix_revocation(FakePool(clean(40)), 100.0) is None

    async def test_an_empty_store_does_not_revoke(self):
        """No evidence is not evidence of collapse; the allowlist grant stands."""
        assert await promotion_source.autofix_revocation(FakePool([]), 100.0) is None

    async def test_two_failures_in_a_day_revoke(self):
        events = clean(30) + [Event(ts_days=31.0, success=False, incident_id="x"),
                              Event(ts_days=31.5, success=False, incident_id="y")]
        reason = await promotion_source.autofix_revocation(FakePool(events), 32.0)
        assert reason is not None
        assert "watchtower-autofix demoted L4→L3" in reason
        assert "CUSUM" in reason

    async def test_it_reads_the_class_the_writer_writes(self):
        """A revocation keyed to a class nothing records would never fire."""
        assert promotion_source.WATCHTOWER_AUTOFIX == "watchtower-autofix"

    async def test_the_transition_is_a_real_reversible_one(self):
        """`irreversible` has θ=None, which `decide` substitutes with 1.0 — not a measured
        threshold. The band must be measured against one somebody set."""
        from app.autonomy.promotion_stats import rule_for

        rule = rule_for(promotion_source.AUTOFIX_TRANSITION)
        assert rule.theta is not None and rule.to == promotion_source.AUTOFIX_GRANTED_RUNG


class TestTheGateIsRevocationOnly:
    """The asymmetry, asserted rather than described."""

    async def test_a_perfect_record_never_returns_a_grant(self):
        for n in (0, 25, 60, 120, 400):
            assert await promotion_source.autofix_revocation(FakePool(clean(n)), 500.0) is None

    def test_the_watchtower_never_widens_auto_fix(self):
        """`_autofix_revoked` can only ever set `auto_fix` False — never True."""
        import inspect

        source = inspect.getsource(watchtower._investigate)
        assert "auto_fix = False" in source
        assert "auto_fix = True" not in source


class TestTheWatchtowerBrake:
    @pytest.fixture
    def flag_on(self, mocker):
        mocker.patch.object(watchtower.settings, "KI_V5_STATISTICAL_PROMOTION", True)

    async def test_flag_off_never_touches_the_store(self, mocker):
        mocker.patch.object(watchtower.settings, "KI_V5_STATISTICAL_PROMOTION", False)
        called = mocker.patch.object(promotion_source, "autofix_revocation")
        assert await watchtower._autofix_revoked() is None
        called.assert_not_called()

    async def test_no_store_abstains_and_says_the_brake_is_not_operating(
            self, mocker, flag_on, caplog):
        mocker.patch.object(service, "_pool", None)
        with caplog.at_level("WARNING"):
            assert await watchtower._autofix_revoked() is None
        assert "brake is NOT operating" in caplog.text

    async def test_an_unreadable_store_revokes_rather_than_reads_clean(self, mocker, flag_on):
        mocker.patch.object(service, "_pool", FakePool(raises=RuntimeError("db down")))
        reason = await watchtower._autofix_revoked()
        assert reason is not None and "unreadable" in reason

    async def test_a_collapsed_record_revokes(self, mocker, flag_on):
        events = clean(30) + [Event(ts_days=31.0, success=False, incident_id="x"),
                              Event(ts_days=31.5, success=False, incident_id="y")]
        mocker.patch.object(service, "_pool", FakePool(events))
        mocker.patch.object(watchtower.time, "time", lambda: 32.0 * 86400.0)
        assert await watchtower._autofix_revoked() is not None

    async def test_a_clean_record_leaves_a3_alone(self, mocker, flag_on):
        mocker.patch.object(service, "_pool", FakePool(clean(40)))
        mocker.patch.object(watchtower.time, "time", lambda: 100.0 * 86400.0)
        assert await watchtower._autofix_revoked() is None


class TestTheDocsMatch:
    def test_the_write_gate_paragraph_records_the_one_way_direction(self):
        from pathlib import Path

        doc = (Path(__file__).resolve().parents[1] / "docs" / "how-it-works.md").read_text(encoding="utf-8")
        assert "it can revoke, never grant" in doc

    def test_the_chokepoint_no_longer_claims_the_store_has_no_writer(self):
        from app.tools.aci import mutating

        assert mutating.__doc__ and "has no production writer either" not in mutating.__doc__
