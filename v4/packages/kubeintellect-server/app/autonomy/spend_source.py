"""Spend usage source (v5 P3 Trust) — closes the blast-radius spend-budget loop.

The budget gate's deny-before-breach spend cap needs a real "current spend" figure; this derives it
from the OTel GenAI chat spans (specs/02) already recorded in the flight recorder — each carries
gen_ai.usage.input/output_tokens. Sum a scope's tokens, price them, and the budget gate can deny an
action whose projected cost would breach the cap. Mirrors how the promotion loop reads its outcomes
from Postgres — real recorded data, not a placeholder.

Pure token→USD math + a jsonb aggregation query; unit-testable with a fake pool.
"""

from __future__ import annotations

from typing import Any

_SPAN_KIND = "ki_otel_span"


def usd_from_tokens(
    input_tokens: float, output_tokens: float, *,
    in_price_per_1k: float, out_price_per_1k: float,
) -> float:
    """Price tokens to USD."""
    return input_tokens / 1000.0 * in_price_per_1k + output_tokens / 1000.0 * out_price_per_1k


async def episode_spend_usd(
    pool: Any, episode_id: str, *, in_price_per_1k: float, out_price_per_1k: float,
) -> float:
    """Sum an episode's recorded chat-span token usage and price it. 0.0 if no spans."""
    row = await pool.fetchrow(
        "SELECT "
        " COALESCE(SUM((payload->'attributes'->>'gen_ai.usage.input_tokens')::numeric), 0)  AS in_tok, "
        " COALESCE(SUM((payload->'attributes'->>'gen_ai.usage.output_tokens')::numeric), 0) AS out_tok "
        "FROM decision_log "
        "WHERE episode_id = $1 AND kind = $2 "
        "  AND payload->'attributes' ? 'gen_ai.usage.input_tokens'",
        episode_id, _SPAN_KIND,
    )
    if row is None:
        return 0.0
    return usd_from_tokens(float(row["in_tok"]), float(row["out_tok"]),
                           in_price_per_1k=in_price_per_1k, out_price_per_1k=out_price_per_1k)
