"""Blast-radius composite gate (v5 P3) — compose budget + staged + failure-domain."""
from __future__ import annotations

from app.autonomy.blast_radius import compose
from app.autonomy.budget import BudgetDecision
from app.autonomy.failure_domain import DomainDecision
from app.autonomy.staged_propagation import StageDecision


def _stage(batch=None, waiting=False, done=False, reason="release"):
    return StageDecision(batch=batch or [], waiting=waiting, done=done, reason=reason)


class TestCompose:
    def test_all_clear_allows_batch(self):
        v = compose(budget=BudgetDecision(True), stage=_stage(batch=["c1"]),
                    domain=DomainDecision(True))
        assert v.allow is True and v.batch == ["c1"]

    def test_budget_denial_denies(self):
        v = compose(budget=BudgetDecision(False, "kill switch engaged"), stage=_stage(batch=["c1"]))
        assert v.allow is False and "budget" in v.reasons[0] and v.batch == []

    def test_domain_denial_denies(self):
        v = compose(budget=BudgetDecision(True), stage=_stage(batch=["c1"]),
                    domain=DomainDecision(False, "would make 67% of the zone unavailable"))
        assert v.allow is False and "failure-domain" in v.reasons[0]

    def test_budget_precedes_domain(self):
        v = compose(budget=BudgetDecision(False, "freeze"), stage=_stage(batch=["c1"]),
                    domain=DomainDecision(False, "zone cap"))
        assert "budget" in v.reasons[0]   # budget denial reported first

    def test_staged_waiting_allows_empty_batch(self):
        v = compose(budget=BudgetDecision(True), stage=_stage(waiting=True, reason="120s until next stage"))
        assert v.allow is True and v.batch == [] and "staged" in v.reasons[0]

    def test_staged_done_empty_batch(self):
        v = compose(budget=BudgetDecision(True), stage=_stage(done=True))
        assert v.allow is True and v.batch == [] and "applied" in v.reasons[0]

    def test_no_domain_check_optional(self):
        v = compose(budget=BudgetDecision(True), stage=_stage(batch=["c1", "c2"]))
        assert v.allow is True and v.batch == ["c1", "c2"]
