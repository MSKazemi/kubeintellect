"""Summary hierarchy — RAPTOR/GraphRAG-style theme summaries over episodes (spec R7, P8).

The four-tier hierarchy remembers individual incidents, but had no way to answer a *theme-level*
question — "what keeps failing in payments?", "are OOMKills trending across the fleet?" — without
scanning every episode. RAPTOR (Sarthi et al. 2024) and GraphRAG (Edge et al. 2024) both answer
this by building a tree of cluster summaries. This module adds the leaf level of that tree:

    episodes ──group by (cluster, playbook|namespace)──► memory_summaries (one theme summary each)

Two deliberate constraints from the spec:
  - **Regeneration is tied to KG change-rate (R7.1)**, not a fixed clock: a theme summary is
    rebuilt only when new episodes arrived (its `last_episode_at`/`member_count` moved) or the
    cluster's KG edge count moved (`kg_watermark`). A quiet cluster costs zero rebuilds.
  - **Deterministic, training-free** (like the promotion pass): the summary is rendered from SQL
    aggregates, no LLM call in the consolidation loop. Abstractive LLM roll-up is deferred.

Pool ownership: uses the memory service's pool (`app.memory.service._pool`). Gated by
`MEMORY_SUMMARY_TREE` (default off). Failure discipline: every function catches, logs, and
returns a harmless value — a summary failure must never break a request or the consolidation loop.
"""
from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.memory import service
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Per-cluster KG edge count — the change-rate watermark (R7.1).
_SQL_KG_WATERMARK = "SELECT cluster_id, count(*) AS n FROM kg_edges GROUP BY cluster_id"

# Theme groups: episodes bucketed by their cluster signature (first playbook, else namespace,
# else 'general'), with the aggregates a deterministic summary needs.
_SQL_THEME_GROUPS = """
    SELECT cluster_id, theme_key,
           count(*) AS n,
           count(*) FILTER (WHERE verified) AS n_verified,
           max(started_at) AS last_ep,
           (array_agg(DISTINCT outcome) FILTER (WHERE outcome IS NOT NULL)) AS outcomes,
           (array_agg(root_cause ORDER BY started_at DESC)
                FILTER (WHERE root_cause IS NOT NULL))[1:3] AS recent_rcs
    FROM (
        SELECT cluster_id,
               COALESCE(NULLIF((playbooks)[1], ''), NULLIF(namespace, ''), 'general') AS theme_key,
               verified, started_at, outcome, root_cause
        FROM episodes
        WHERE summary <> '' AND cluster_id <> ''
    ) t
    GROUP BY cluster_id, theme_key
    HAVING count(*) >= $1
"""

# Conditional upsert: only (re)write when the theme actually changed (R7.1) — new episodes
# (member_count / last_episode_at moved) or KG edge count moved. Otherwise the ON CONFLICT
# matches but the WHERE suppresses the UPDATE, so a quiet theme is never rebuilt on a clock.
_SQL_UPSERT_SUMMARY = """
    INSERT INTO memory_summaries
      (cluster_id, level, theme_key, summary, member_count, verified_count,
       last_episode_at, kg_watermark, updated_at)
    VALUES ($1, 1, $2, $3, $4, $5, $6, $7, now())
    ON CONFLICT (cluster_id, level, theme_key) DO UPDATE SET
        summary         = EXCLUDED.summary,
        member_count    = EXCLUDED.member_count,
        verified_count  = EXCLUDED.verified_count,
        last_episode_at = EXCLUDED.last_episode_at,
        kg_watermark    = EXCLUDED.kg_watermark,
        updated_at      = now()
    WHERE EXCLUDED.last_episode_at IS DISTINCT FROM memory_summaries.last_episode_at
       OR EXCLUDED.member_count <> memory_summaries.member_count
       OR EXCLUDED.kg_watermark <> memory_summaries.kg_watermark
    RETURNING id
"""

_SQL_RECALL_SUMMARIES = """
    SELECT theme_key, summary, member_count, verified_count, last_episode_at,
           similarity(theme_key || ' ' || summary, $1) AS sim
    FROM memory_summaries
    WHERE cluster_id = $2 AND level = 1
    ORDER BY sim DESC, member_count DESC
    LIMIT $3
"""


def _render_summary(theme: str, n: int, n_verified: int, outcomes, recent_rcs) -> str:
    """Deterministic theme summary from SQL aggregates (no LLM)."""
    parts = [f"Theme '{theme}': {n} episodes ({n_verified} verified)."]
    outs = [o for o in (outcomes or []) if o]
    if outs:
        parts.append("Outcomes: " + ", ".join(sorted(outs)) + ".")
    rcs = [str(rc).replace("\n", " ")[:120] for rc in (recent_rcs or []) if rc]
    if rcs:
        parts.append("Recent root causes: " + " | ".join(rcs) + ".")
    return " ".join(parts)[:1500]


async def build_summary_tree() -> int:
    """Rebuild changed theme summaries (P8, R7.1). Returns the number written/updated.

    Gated by ``MEMORY_SUMMARY_TREE`` — a no-op when off.
    """
    if not settings.MEMORY_SUMMARY_TREE:
        return 0
    pool = service._pool
    if pool is None:
        return 0
    try:
        wm_rows = await pool.fetch(_SQL_KG_WATERMARK)
        watermark = {r["cluster_id"]: int(r["n"]) for r in wm_rows}
        groups = await pool.fetch(_SQL_THEME_GROUPS, settings.MEMORY_SUMMARY_MIN_CLUSTER)
    except Exception as exc:
        logger.warning(f"summaries: fetch failed: {exc}")
        return 0

    written = 0
    for g in groups:
        summary = _render_summary(
            g["theme_key"], g["n"], g["n_verified"], g["outcomes"], g["recent_rcs"]
        )
        try:
            row = await pool.fetchrow(
                _SQL_UPSERT_SUMMARY,
                g["cluster_id"], g["theme_key"], summary, g["n"], g["n_verified"],
                g["last_ep"], watermark.get(g["cluster_id"], 0),
            )
            if row is not None:                     # None = ON CONFLICT WHERE suppressed the rewrite
                written += 1
        except Exception as exc:
            logger.warning(f"summaries: upsert failed theme={g['theme_key']}: {exc}")
    if written:
        logger.info(f"summaries: (re)built {written} theme summaries")
    return written


async def recall_theme_summaries(
    query_text: str, cluster_id: str, k: int = 3
) -> list[dict[str, Any]]:
    """Top-k theme summaries for a theme-level question. Empty on any error."""
    pool = service._pool
    if pool is None or not query_text.strip():
        return []
    try:
        rows = await pool.fetch(_SQL_RECALL_SUMMARIES, query_text[:500], cluster_id, k)
    except Exception as exc:
        logger.warning(f"summaries: recall failed: {exc}")
        return []
    return [
        dict(r) for r in rows
        if r["sim"] is None or r["sim"] > settings.MEMORY_RECALL_SIMILARITY_FLOOR
    ]


def render_summaries_block(summaries: list[dict]) -> str:
    """Compact prompt block for theme-level context injection."""
    if not summaries:
        return ""
    lines = ["## Memory themes (this cluster)"]
    for s in summaries:
        lines.append(f"- {s.get('summary', '')}")
    return "\n".join(lines)
