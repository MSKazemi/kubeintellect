"""L1 episodic memory — investigations as recallable episodes (ADR-002).

Recall strategy (graceful degradation, no hard pgvector dependency):
  1. pgvector + an embedding function, when both are available (not wired in
     v1 — the column is only added once an embedding provider is configured).
  2. Baseline: pg_trgm text similarity over (summary + root_cause), combined
     with recency and cluster scoping. Stock-Postgres-compatible.

Failure discipline: memory must never break a request — every public function
catches, logs, and returns a safe empty value.
"""
from __future__ import annotations

import json
from datetime import datetime, UTC
from typing import Any

from app.core.config import settings
from app.utils.logger import get_logger
from app.utils.redact import redact_secrets

logger = get_logger(__name__)

_pool = None  # asyncpg.Pool — owned by app.memory.service

# Reciprocal Rank Fusion constant (Cormack et al. 2009). Memory V5 ADR-014 / P1.
_RRF_K = 60

# ── Memory V5 P6 (ADR-017): importance / surprise ────────────────────────────
# Importance derives from incident severity: a regression a fix caused is the most
# valuable thing to remember; a report-only look is the least. verified + confidence
# nudge it up. Bounded to [0,1]; modulates RANKING ONLY (R6.2), never retention.
_OUTCOME_SEVERITY = {
    "regression": 1.0,
    "partial": 0.7,
    "resolved": 0.5,
    "report_only": 0.3,
}
# A new episode this similar to something already in memory is not surprising; if it is
# also low-value (unverified + report-only) the surprise gate drops the redundant write.
_SURPRISE_FLOOR = 0.15


def _importance_score(
    outcome: str | None, verified: bool | None, confidence: float | None
) -> float:
    """Heuristic incident-severity importance in [0,1] (ADR-017 Q3: heuristic → learned)."""
    base = _OUTCOME_SEVERITY.get((outcome or "").strip().lower(), 0.4)
    if verified:
        base += 0.15
    if confidence is not None:
        base += 0.1 * max(0.0, min(1.0, float(confidence)))
    return round(max(0.0, min(1.0, base)), 4)


def _is_low_value(verified: bool | None, outcome: str | None) -> bool:
    """Low audit value = unverified AND (no outcome | report_only). Only these are
    ever gated by surprise; anything verified or actioned is always retained."""
    return not verified and (outcome or "").strip().lower() in ("", "report_only")


def _imp_weight(col: str) -> str:
    """Recall weight from importance so relevance stays dominant: weight ∈ [0.5,1.0].
    Old rows (importance NULL) COALESCE to a neutral 0.5. One place so SQL can't drift."""
    return f"(0.5 + 0.5 * COALESCE({col}, 0.5))"


def init_episodes(pool) -> None:
    global _pool
    _pool = pool


def close_episodes() -> None:
    global _pool
    _pool = None


async def write_episode(
    *,
    cluster_id: str,
    trigger_kind: str,
    trigger_detail: str = "",
    summary: str = "",
    namespace: str | None = None,
    root_cause: str | None = None,
    actions: list[dict] | None = None,
    outcome: str | None = None,
    verified: bool | None = None,
    confidence: float | None = None,
    playbooks: list[str] | None = None,
    created_by_role: str | None = None,
    request_id: str | None = None,
    started_at: float | None = None,
    ended_at: float | None = None,
) -> str | None:
    """Insert one episode (redacted). Returns the episode id or None.

    Memory V5 P6 (ADR-017): when ``MEMORY_IMPORTANCE`` is on, the episode is scored for
    importance (incident severity → recall ranking, never retention) and surprise (KG-novelty
    proxy). A near-duplicate that is also low-value (unverified + report-only) is dropped by
    the surprise gate (R6.3); anything verified or actioned is always retained (R6.2).

    Memory V5 P7 (ADR-018): when ``MEMORY_SECURITY_HARDENING`` is on, a user-derived write must
    clear the diverse write-admission guard (provenance trust, MINJA injection signature, rate
    limit); a quarantined write is dropped. A provenance ``trust`` is stamped on every episode.
    """
    if _pool is None:
        return None
    red_summary = redact_secrets(summary, max_chars=1500)
    red_root = redact_secrets(root_cause, max_chars=500) if root_cause else None
    importance: float | None = None
    surprise: float | None = None
    trust: float | None = None
    try:
        if settings.MEMORY_SECURITY_HARDENING:
            from app.memory import security
            decision = security.admit_write(
                source_kind=trigger_kind,
                requester=request_id or created_by_role,
                text=red_summary + " " + (red_root or ""),
            )
            trust = decision.trust
            if not decision.admit:
                logger.info(
                    f"episodes: write-guard quarantined a write "
                    f"(reason={decision.reason} trust={decision.trust:.2f} "
                    f"trigger={trigger_kind} cluster={cluster_id})"
                )
                # Tamper-evidence (R8.2): a rejected poison attempt is itself an audit event.
                await security.record_memory_audit(
                    _pool, cluster_id=cluster_id, kind="quarantine",
                    payload={"reason": decision.reason, "trigger": trigger_kind,
                             "trust": round(decision.trust, 3)},
                )
                return None
        if settings.MEMORY_IMPORTANCE:
            importance = _importance_score(outcome, verified, confidence)
            surprise = await _surprise_novelty(
                cluster_id, red_summary + " " + (red_root or "")
            )
            if surprise < _SURPRISE_FLOOR and _is_low_value(verified, outcome):
                logger.info(
                    f"episodes: surprise-gate dropped low-value redundant write "
                    f"(surprise={surprise:.3f} cluster={cluster_id})"
                )
                return None
        row = await _pool.fetchrow(
            """
            INSERT INTO episodes
              (cluster_id, namespace, trigger_kind, trigger_detail, summary,
               root_cause, actions, outcome, verified, confidence, playbooks,
               created_by_role, request_id, started_at, ended_at,
               importance, surprise, trust)
            VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11,$12,$13,
                    COALESCE($14, now()), $15, $16, $17, $18)
            RETURNING id
            """,
            cluster_id,
            namespace,
            trigger_kind,
            redact_secrets(trigger_detail, max_chars=500),
            red_summary,
            red_root,
            json.dumps(actions or [], default=str)[:8000],
            outcome,
            verified,
            confidence,
            playbooks or [],
            created_by_role,
            request_id,
            _ts(started_at),
            _ts(ended_at),
            importance,
            surprise,
            trust,
        )
        episode_id = str(row["id"]) if row else None
        if episode_id and settings.MEMORY_SECURITY_HARDENING:
            # Tamper-evidence (R8.2): chain each admitted write so a later silent edit is detectable.
            from app.memory import security
            await security.record_memory_audit(
                _pool, cluster_id=cluster_id, kind="episode_write", ref_id=episode_id,
                payload={"trigger": trigger_kind, "trust": round(trust, 3) if trust else None},
            )
        return episode_id
    except Exception as exc:
        logger.warning(f"episodes: write failed: {exc}")
        return None


# Surprise = KG-novelty proxy (ADR-017 Q4): how unlike recent memory this episode is.
# True per-entity KG novelty needs structured extraction (not yet wired — deferred, as in
# P4); until then the episode-level trgm distance to existing memory is a faithful,
# dependency-free stand-in. 1.0 = nothing similar seen (fully novel), 0.0 = a duplicate.
_SQL_SURPRISE = """
    SELECT max(similarity(summary || ' ' || COALESCE(root_cause, ''), $1)) AS top
    FROM episodes
    WHERE cluster_id = $2 AND summary <> ''
"""


async def _surprise_novelty(cluster_id: str, text: str) -> float:
    if _pool is None or not text.strip():
        return 1.0
    try:
        row = await _pool.fetchrow(_SQL_SURPRISE, text[:500], cluster_id)
    except Exception as exc:
        logger.warning(f"episodes: surprise scoring failed: {exc}")
        return 1.0  # fail open — never let scoring block a write
    top = float(row["top"]) if row and row["top"] is not None else 0.0
    return max(0.0, min(1.0, 1.0 - top))


# Baseline: pg_trgm similarity + recency (single channel).
_SQL_RECALL_TRGM = """
    SELECT id, summary, root_cause, outcome, verified, confidence,
           playbooks, namespace, started_at,
           similarity(summary || ' ' || COALESCE(root_cause, ''), $1) AS sim
    FROM episodes
    WHERE cluster_id = $2
      AND summary <> ''
    ORDER BY sim DESC, started_at DESC
    LIMIT $3
"""

# Memory V5 P1 (ADR-014): hybrid recall — fuse the pg_trgm channel with a
# full-text (ts_rank) lexical channel via Reciprocal Rank Fusion in one query.
# Rows matched by EITHER channel are kept; RRF ranks them together. `ts_rank` is
# not true BM25 (review F2) but is a real term-weighted channel over stock
# Postgres; a stored tsvector + GIN index (P0) makes it index-fast at scale.
_SQL_RECALL_HYBRID = """
    WITH trgm AS (
        SELECT id,
               row_number() OVER (
                   ORDER BY similarity(summary || ' ' || COALESCE(root_cause, ''), $1) DESC,
                            started_at DESC
               ) AS rnk
        FROM episodes
        WHERE cluster_id = $2
          AND summary <> ''
          AND similarity(summary || ' ' || COALESCE(root_cause, ''), $1) > $5
    ),
    fts AS (
        SELECT id,
               row_number() OVER (
                   ORDER BY ts_rank(
                       to_tsvector('english', summary || ' ' || COALESCE(root_cause, '')),
                       plainto_tsquery('english', $1)
                   ) DESC,
                   started_at DESC
               ) AS rnk
        FROM episodes
        WHERE cluster_id = $2
          AND summary <> ''
          AND to_tsvector('english', summary || ' ' || COALESCE(root_cause, ''))
              @@ plainto_tsquery('english', $1)          -- index: idx_episodes_fts
    ),
    fused AS (
        SELECT id, SUM(w) AS rrf FROM (
            SELECT id, 1.0 / ($4 + rnk) AS w FROM trgm
            UNION ALL
            SELECT id, 1.0 / ($4 + rnk) AS w FROM fts
        ) u
        GROUP BY id
    )
    SELECT e.id, e.summary, e.root_cause, e.outcome, e.verified, e.confidence,
           e.playbooks, e.namespace, e.started_at, f.rrf
    FROM fused f
    JOIN episodes e ON e.id = f.id
    ORDER BY f.rrf DESC, e.started_at DESC
    LIMIT $3
"""

# Memory V5 P6 (ADR-017): importance-weighted variants. Recall combines recency ×
# importance × relevance (R6.1) — the relevance score (trgm sim / RRF) is modulated by an
# importance weight ∈ [0.5,1.0] and recency stays the tiebreak. importance affects RANKING
# ONLY; retention is untouched (R6.2). Same shape as the flat variants so the flag only
# swaps the ORDER BY (and, for hybrid, threads e.importance through the final select).
_SQL_RECALL_TRGM_IMP = _SQL_RECALL_TRGM.replace(
    "ORDER BY sim DESC, started_at DESC",
    f"ORDER BY sim * {_imp_weight('importance')} DESC, started_at DESC",
)
_SQL_RECALL_HYBRID_IMP = _SQL_RECALL_HYBRID.replace(
    "e.playbooks, e.namespace, e.started_at, f.rrf\n",
    "e.playbooks, e.namespace, e.started_at, f.rrf, e.importance\n",
).replace(
    "ORDER BY f.rrf DESC, e.started_at DESC",
    f"ORDER BY f.rrf * {_imp_weight('e.importance')} DESC, e.started_at DESC",
)


async def recall_episodes(
    query_text: str, cluster_id: str, k: int = 3
) -> list[dict[str, Any]]:
    """Top-k similar past episodes for this cluster.

    Baseline: pg_trgm similarity + recency. When ``MEMORY_HYBRID_RETRIEVAL`` is on
    (Memory V5 P1), fuses the trgm channel with a full-text ts_rank channel via
    Reciprocal Rank Fusion (ADR-014), which the WHERE clause pre-filters to the
    channel-matched set — so no post-hoc trgm floor is applied on the hybrid path.
    When ``MEMORY_IMPORTANCE`` is on (Memory V5 P6), the relevance score is modulated by
    an importance weight (recency × importance × relevance, R6.1); the flag independently
    swaps the ORDER BY on whichever channel (trgm/hybrid) is active.
    """
    if _pool is None or not query_text.strip():
        return []
    hybrid = settings.MEMORY_HYBRID_RETRIEVAL
    importance = settings.MEMORY_IMPORTANCE
    try:
        if hybrid:
            rows = await _pool.fetch(
                _SQL_RECALL_HYBRID_IMP if importance else _SQL_RECALL_HYBRID,
                query_text[:500], cluster_id, k, _RRF_K,
                settings.MEMORY_RECALL_SIMILARITY_FLOOR,
            )
        else:
            rows = await _pool.fetch(
                _SQL_RECALL_TRGM_IMP if importance else _SQL_RECALL_TRGM,
                query_text[:500], cluster_id, k,
            )
    except Exception as exc:
        logger.warning(f"episodes: recall failed (hybrid={hybrid}): {exc}")
        # If the hybrid path errors (e.g. an unexpected FTS edge case), fall back
        # to the baseline so recall degrades gracefully instead of going empty.
        if hybrid:
            try:
                rows = await _pool.fetch(
                    _SQL_RECALL_TRGM, query_text[:500], cluster_id, k,
                )
            except Exception as exc2:
                logger.warning(f"episodes: baseline recall also failed: {exc2}")
                return []
        else:
            return []
    out = []
    for row in rows:
        # Baseline path applies the trgm noise floor here; the hybrid SQL already
        # filtered to channel-matched rows (a lex-only match has sim ≤ floor but
        # is still relevant), so it must NOT re-apply the trgm floor.
        if (
            not hybrid
            and row["sim"] is not None
            and row["sim"] <= settings.MEMORY_RECALL_SIMILARITY_FLOOR
        ):
            continue
        out.append(dict(row))
    return out


def render_recall_block(episodes: list[dict]) -> str:
    """Compact prompt block for triage injection (≤ ~400 tokens for k=3)."""
    if not episodes:
        return ""
    lines = ["## Similar past episodes (this cluster)"]
    for ep in episodes:
        verified = "verified" if ep.get("verified") else "unverified"
        outcome = ep.get("outcome") or "report_only"
        summary = (ep.get("summary") or "").replace("\n", " ")[:220]
        lines.append(f"- [{outcome}/{verified}] {summary}")
        if ep.get("root_cause"):
            lines.append(f"  root_cause: {str(ep['root_cause'])[:160]}")
    return "\n".join(lines)


async def backfill_from_rca_outcomes(cluster_id_default: str = "unknown") -> int:
    """One-shot idempotent migration: rca_outcomes → episodes (trigger_kind=backfill).

    Idempotency: an rca_outcome row is identified by (created_at, user_id);
    rows already backfilled (matching request_id or exact started_at) are skipped
    via the NOT EXISTS guard.
    """
    if _pool is None:
        return 0
    try:
        result = await _pool.execute(
            """
            INSERT INTO episodes
              (cluster_id, namespace, trigger_kind, trigger_detail, summary,
               root_cause, actions, outcome, verified, confidence, playbooks,
               created_by_role, request_id, started_at, ended_at)
            SELECT COALESCE(NULLIF(r.cluster_id, ''), $1), r.namespace, 'backfill',
                   'migrated from rca_outcomes', COALESCE(r.root_cause, ''),
                   r.root_cause,
                   COALESCE(to_jsonb(r.recommended_fix), '[]'::jsonb),
                   r.outcome_feedback, r.verified_resolved, r.confidence,
                   COALESCE(r.playbooks_matched, '{}'), r.created_by_role,
                   r.request_id, r.created_at, r.created_at
            FROM rca_outcomes r
            WHERE NOT EXISTS (
                SELECT 1 FROM episodes e
                WHERE e.trigger_kind = 'backfill' AND e.started_at = r.created_at
                  AND e.root_cause IS NOT DISTINCT FROM r.root_cause
            )
            """,
            cluster_id_default,
        )
        count = int(result.split()[-1]) if result else 0
        if count:
            logger.info(f"episodes: backfilled {count} rows from rca_outcomes")
        return count
    except Exception as exc:
        logger.warning(f"episodes: backfill failed: {exc}")
        return 0


def _ts(value: float | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=UTC)
