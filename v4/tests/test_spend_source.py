"""Spend usage source (v5 P3) — token→USD + episode spend aggregation, and budget composition."""
from __future__ import annotations

from app.autonomy.budget import check_spend
from app.autonomy.spend_source import episode_spend_usd, usd_from_tokens


class _FakePool:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self.row


class TestPricing:
    def test_token_pricing(self):
        # 10k input @ 0.0025/1k + 2k output @ 0.01/1k = 0.025 + 0.02 = 0.045
        assert abs(usd_from_tokens(10000, 2000, in_price_per_1k=0.0025, out_price_per_1k=0.01) - 0.045) < 1e-9

    def test_zero_tokens_zero_cost(self):
        assert usd_from_tokens(0, 0, in_price_per_1k=1, out_price_per_1k=1) == 0.0


class TestEpisodeSpend:
    async def test_aggregates_and_prices(self):
        pool = _FakePool({"in_tok": 20000, "out_tok": 5000})
        usd = await episode_spend_usd(pool, "ep-1", in_price_per_1k=0.0025, out_price_per_1k=0.01)
        assert abs(usd - (20000 / 1000 * 0.0025 + 5000 / 1000 * 0.01)) < 1e-9
        sql, args = pool.calls[0]
        assert "ki_otel_span" in args and "episode_id = $1" in sql

    async def test_no_spans_zero(self):
        pool = _FakePool({"in_tok": 0, "out_tok": 0})
        assert await episode_spend_usd(pool, "ep", in_price_per_1k=1, out_price_per_1k=1) == 0.0

    async def test_none_row_zero(self):
        assert await episode_spend_usd(_FakePool(None), "ep", in_price_per_1k=1, out_price_per_1k=1) == 0.0


class TestBudgetComposition:
    async def test_real_spend_drives_deny_before_breach(self):
        pool = _FakePool({"in_tok": 400000, "out_tok": 100000})   # 1.0 + 1.0 = $2.00 spent
        spent = await episode_spend_usd(pool, "ep", in_price_per_1k=0.0025, out_price_per_1k=0.01)
        # a $0.50 projected action would push $2.00 -> $2.50, over a $2.20 cap ⇒ deny
        assert check_spend(spent, 0.50, 2.20).allow is False
        # under a $3 cap ⇒ allow
        assert check_spend(spent, 0.50, 3.00).allow is True
