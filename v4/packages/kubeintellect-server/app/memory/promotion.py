"""
L3 promotion pipeline — episodic reflection → semantic rule → detector candidate (ADR-016).

The clearest learning gap the SoA study surfaced: V4 stored per-episode reflections and had a
detector *compiler*, but no explicit path turning a recurring, *verified* reflection into a
reusable *semantic rule* and then a detector. This module adds the middle abstraction:

    verified, recurring episodes  ─promote_from_episodes()─►  semantic_rules (IF→THEN)
    recurrence ≥ N  ─►  status='active'  ─►  injected into the prompt (render_rules_block)
                                          └─►  eligible to seed a detector candidate
                                               (reuses the ADR-006/012 human-review flow)

Non-parametric / training-free (Voyager/AWM/AutoGuide/ExpeL/CBR) — a fit for the closed-weight
API constraint. Runs inside the consolidation worker, gated by `MEMORY_PROMOTION` (default off).

Pool ownership: uses the memory service's pool (`app.memory.service._pool`), like consolidation.
Failure discipline: every function catches, logs, and returns a harmless value — a promotion
failure must never break a request or the consolidation loop.
"""
from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.memory import service
from app.memory import pass_health
from app.utils.logger import get_logger

logger = get_logger(__name__)

# A rule becomes 'active' (prompt-injected) once it has recurred this many times.
_DEFAULT_ACTIVATE_AT = 2


_SQL_UPSERT_RULE = """
    INSERT INTO semantic_rules (cluster_id, context, guidance, source, confidence)
    VALUES ($1, $2, $3, $4, $5)
    ON CONFLICT (cluster_id, context) DO UPDATE SET
        recurrence_count = semantic_rules.recurrence_count + 1,
        guidance         = EXCLUDED.guidance,
        confidence       = LEAST(1.0, semantic_rules.confidence + 0.1),
        last_seen_at     = now(),
        status           = CASE
            WHEN semantic_rules.status = 'demoted' THEN 'demoted'          -- respect demotion
            WHEN semantic_rules.recurrence_count + 1 >= $6 THEN 'active'
            ELSE semantic_rules.status END
    RETURNING id, status, recurrence_count
"""

# Verified episodes grouped by (cluster, playbook signature); a signature seen ≥ $1 times is a
# recurring, learnable pattern. latest_rc = the most recent verified root cause for that signature.
_SQL_RECURRING_VERIFIED = """
    SELECT cluster_id, unnest(playbooks) AS ctx, count(*) AS n,
           (array_agg(root_cause ORDER BY started_at DESC))[1] AS latest_rc
    FROM episodes
    WHERE verified = TRUE AND playbooks <> '{}' AND cluster_id <> ''
    GROUP BY cluster_id, ctx
    HAVING count(*) >= $1
"""

_SQL_ACTIVE_RULES = """
    SELECT context, guidance, recurrence_count, confidence
    FROM semantic_rules
    WHERE cluster_id = $1 AND status = 'active'
    ORDER BY confidence DESC, recurrence_count DESC
    LIMIT $2
"""


async def record_rule(
    cluster_id: str,
    context: str,
    guidance: str,
    *,
    source: str = "episode",
    confidence: float = 0.5,
    activate_at: int = _DEFAULT_ACTIVATE_AT,
) -> str | None:
    """Upsert one IF→THEN rule. A repeat sighting bumps recurrence/confidence and, once it
    reaches ``activate_at``, flips the rule to 'active' (prompt-injected). Returns its id."""
    pool = service._pool
    if pool is None or not context.strip() or not guidance.strip():
        return None
    try:
        row = await pool.fetchrow(
            _SQL_UPSERT_RULE,
            cluster_id, context[:200], guidance[:1000], source, confidence, activate_at,
        )
        return str(row["id"]) if row else None
    except Exception as exc:
        logger.warning(f"promotion: record_rule failed ({context[:40]}): {exc}")
        return None


async def promote_from_episodes(min_recurrence: int = _DEFAULT_ACTIVATE_AT) -> int:
    """Consolidation pass: promote verified, recurring episode signatures into semantic rules.

    Gated by ``MEMORY_PROMOTION``. Returns the number of rules created/reinforced.
    """
    if not settings.MEMORY_PROMOTION:
        return 0
    pool = service._pool
    if pool is None:
        return 0
    try:
        groups = await pool.fetch(_SQL_RECURRING_VERIFIED, min_recurrence)
        promoted = 0
        for g in groups:
            rid = await record_rule(
                g["cluster_id"],
                g["ctx"],
                g["latest_rc"] or "(verified fix; see linked episodes)",
                source="episode",
                activate_at=min_recurrence,
            )
            if rid:
                promoted += 1
        if promoted:
            logger.info(f"promotion: promoted/reinforced {promoted} semantic rule(s)")
        return promoted
    except Exception as exc:
        logger.warning(f"promotion: promote_from_episodes failed: {exc}")
        pass_health.record_failure("rules_promoted", exc)
        return 0


async def active_rules(cluster_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """Active (prompt-injectable) learned rules for this cluster, most-confident first."""
    pool = service._pool
    if pool is None:
        return []
    try:
        rows = await pool.fetch(_SQL_ACTIVE_RULES, cluster_id, limit)
        return [dict(row) for row in rows]
    except Exception as exc:
        logger.warning(f"promotion: active_rules failed: {exc}")
        return []


def render_rules_block(rules: list[dict]) -> str:
    """Compact prompt block of learned rules (≤ ~250 tokens for the top handful)."""
    if not rules:
        return ""
    lines = ["## Learned rules (this cluster)"]
    for rule in rules:
        guidance = str(rule.get("guidance") or "").replace("\n", " ")[:200]
        lines.append(
            f"- IF {rule['context']} THEN {guidance} (seen ×{rule.get('recurrence_count', 1)})"
        )
    return "\n".join(lines)
