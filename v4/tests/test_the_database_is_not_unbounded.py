"""A10 — retention exists, is bounded, and refuses the tables it must not touch.

Three separate claims, and the second two are the ones worth a test file:

1. There is a prune at all. Before 2026-08-28 there was none — twenty append-only tables and
   no clock-driven DELETE anywhere in the tree.
2. It never prunes a hash-chained ledger. `decision_log` and `memory_audit` are
   tamper-evidence; deleting their newest rows breaks no hash link and so is invisible to
   `verify_chain`, while contradicting the head anchors that exist to catch exactly that. A
   retention pass that touched them would ship a tamper-evidence bypass as housekeeping.
3. A short retention window cannot blind the A3 statistical brake. `promotion_outcomes` feeds
   the only thing that can revoke unattended-write authority, and it reads a rolling
   WINDOW_MAX_DAYS window — so pruning inside that window would delete the failures the brake
   demotes on, quietly widening what the watchtower may do without a human.
"""
from __future__ import annotations

import inspect

import pytest

from app.autonomy.promotion_stats import WINDOW_MAX_DAYS
from app.core.config import settings
from app.memory import consolidation, pass_health, retention, service


class FakePool:
    """Records every statement; fails for the tables named in ``fail_on``."""

    def __init__(self, deleted: int = 0, fail_on: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.deleted = deleted
        self.fail_on = set(fail_on)

    async def execute(self, sql: str, *args):
        self.calls.append((sql, args))
        for table in self.fail_on:
            if f"DELETE FROM {table} " in sql:
                raise RuntimeError(f"boom {table}")
        return f"DELETE {self.deleted}"

    def tables(self) -> list[str]:
        return [sql.split("DELETE FROM ")[1].split()[0] for sql, _ in self.calls]

    def days_for(self, table: str) -> int:
        for sql, args in self.calls:
            if f"DELETE FROM {table} " in sql:
                return args[0]
        raise AssertionError(f"{table} was never pruned")


@pytest.fixture
def pool(monkeypatch):
    p = FakePool(deleted=3)
    monkeypatch.setattr(service, "_pool", p)
    return p


@pytest.fixture(autouse=True)
def _clean_health():
    pass_health.reset()
    yield
    pass_health.reset()


class TestThereIsAPruneAtAll:
    def test_it_is_off_by_default(self):
        assert settings.MEMORY_RETENTION_DAYS == 0, (
            "a data-deleting default would discard an operator's history on upgrade"
        )

    @pytest.mark.asyncio
    async def test_off_means_not_one_statement_is_issued(self, pool, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_RETENTION_DAYS", 0)
        assert await retention.prune_once() == 0
        assert pool.calls == []

    @pytest.mark.asyncio
    async def test_a_negative_setting_is_off_too(self, pool, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_RETENTION_DAYS", -30)
        assert await retention.prune_once() == 0
        assert pool.calls == []

    @pytest.mark.asyncio
    async def test_no_pool_is_a_no_op_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_RETENTION_DAYS", 30)
        monkeypatch.setattr(service, "_pool", None)
        assert await retention.prune_once() == 0

    @pytest.mark.asyncio
    async def test_on_it_prunes_every_rule_and_counts_them(self, pool, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_RETENTION_DAYS", 30)
        total = await retention.prune_once()
        assert pool.tables() == [r.table for r in retention._RULES]
        assert total == 3 * len(retention._RULES)

    @pytest.mark.asyncio
    async def test_every_statement_is_bounded(self, pool, monkeypatch):
        """One pass must never issue an unbounded DELETE — it shares the request pool."""
        monkeypatch.setattr(settings, "MEMORY_RETENTION_DAYS", 30)
        await retention.prune_once()
        for sql, _ in pool.calls:
            assert f"LIMIT {retention._PRUNE_BATCH}" in sql

    @pytest.mark.asyncio
    async def test_only_terminal_recheck_rows_age_out(self, pool, monkeypatch):
        """A pending 'did the fix hold?' intent is live work, not history."""
        monkeypatch.setattr(settings, "MEMORY_RETENTION_DAYS", 30)
        await retention.prune_once()
        sql = next(s for s, _ in pool.calls if "prospective_memory" in s)
        assert "status IN ('done', 'cancelled')" in sql


class TestTheHashChainsAreRefused:
    def test_the_ledgers_and_their_anchors_are_all_listed(self):
        for table in ("decision_log", "memory_audit", "decision_log_head", "memory_chain_head"):
            assert table in retention.REFUSED

    def test_episodes_are_refused_too(self):
        """L1 memory is the product's recall, and a live chain points at it."""
        assert "episodes" in retention.REFUSED

    def test_every_refusal_carries_a_dated_reason(self):
        for table, why in retention.REFUSED.items():
            assert why.startswith("2026-"), f"{table} refusal is undated"
            assert len(why) > 80, f"{table} refusal is not a reason, it is a label"

    def test_no_rule_names_a_refused_table(self):
        for rule in retention._RULES:
            assert rule.table not in retention.REFUSED

    @pytest.mark.asyncio
    async def test_a_whole_pass_never_mentions_a_refused_table(self, pool, monkeypatch):
        """The end-to-end version of the claim: read the SQL that actually went out."""
        monkeypatch.setattr(settings, "MEMORY_RETENTION_DAYS", 1)
        await retention.prune_once()
        for sql, _ in pool.calls:
            for table in retention.REFUSED:
                assert f"DELETE FROM {table} " not in sql

    def test_the_module_says_why_rather_than_just_not_doing_it(self):
        doc = " ".join((retention.__doc__ or "").split())
        assert "tamper-EVIDENCE" in doc or "tamper-evidence" in doc
        assert "decision_log" in doc and "memory_audit" in doc


class TestAShortWindowCannotBlindTheBrake:
    def test_promotion_outcomes_carries_the_adr102_floor(self):
        rule = next(r for r in retention._RULES if r.table == "promotion_outcomes")
        assert rule.floor_days == WINDOW_MAX_DAYS
        assert rule.floor_why

    def test_a_shorter_setting_is_clamped_up(self):
        rule = next(r for r in retention._RULES if r.table == "promotion_outcomes")
        assert retention.effective_days(rule, 1) == WINDOW_MAX_DAYS
        assert retention.effective_days(rule, 7) == WINDOW_MAX_DAYS

    def test_a_longer_setting_is_honoured(self):
        rule = next(r for r in retention._RULES if r.table == "promotion_outcomes")
        assert retention.effective_days(rule, 365) == 365

    def test_a_floorless_rule_uses_the_setting_as_given(self):
        rule = next(r for r in retention._RULES if r.table == "request_log")
        assert retention.effective_days(rule, 1) == 1

    @pytest.mark.asyncio
    async def test_the_clamp_reaches_the_statement_not_just_the_helper(
        self, pool, monkeypatch,
    ):
        monkeypatch.setattr(settings, "MEMORY_RETENTION_DAYS", 1)
        await retention.prune_once()
        assert pool.days_for("promotion_outcomes") == WINDOW_MAX_DAYS
        assert pool.days_for("request_log") == 1


class TestOneBadTableDoesNotStopThePass:
    @pytest.mark.asyncio
    async def test_the_rest_still_prune(self, monkeypatch):
        p = FakePool(deleted=2, fail_on=("request_log",))
        monkeypatch.setattr(service, "_pool", p)
        monkeypatch.setattr(settings, "MEMORY_RETENTION_DAYS", 30)
        total = await retention.prune_once()
        assert p.tables() == [r.table for r in retention._RULES]
        assert total == 2 * (len(retention._RULES) - 1)

    @pytest.mark.asyncio
    async def test_the_failure_is_registered_not_swallowed(self, monkeypatch):
        """Counters cannot tell 'nothing to prune' from 'it raised' — the register can."""
        p = FakePool(deleted=0, fail_on=("request_log",))
        monkeypatch.setattr(service, "_pool", p)
        monkeypatch.setattr(settings, "MEMORY_RETENTION_DAYS", 30)
        await retention.prune_once()
        assert [name for name, _ in pass_health.drain()] == ["retention_pruned"]


class TestTheConsolidationLoopRunsIt:
    def test_it_is_wired_and_gated(self):
        src = inspect.getsource(consolidation.run_consolidation_once)
        assert "settings.MEMORY_RETENTION_DAYS > 0" in src
        assert "retention.prune_once()" in src
        assert 'stats["rows_pruned"]' in src

    @pytest.mark.asyncio
    async def test_a_pass_reports_what_it_pruned(self, pool, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_RETENTION_DAYS", 30)
        monkeypatch.setattr(service, "memory_active", lambda: True)
        for flag in ("PREFERENCE_MEMORY_ENABLED", "MEMORY_PROMOTION", "MEMORY_PROSPECTIVE",
                     "MEMORY_SUMMARY_TREE", "CORTEX_V5_ENABLED"):
            monkeypatch.setattr(settings, flag, False)
        monkeypatch.setattr(consolidation, "_close_stale_edges", _zero)
        monkeypatch.setattr(consolidation, "_propose_detector_candidates", _zero)
        stats = await consolidation.run_consolidation_once()
        assert stats["rows_pruned"] == 3 * len(retention._RULES)


async def _zero() -> int:
    return 0
