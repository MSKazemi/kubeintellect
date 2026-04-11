# app/services/failure_pattern_service.py
"""
FailurePatternService — keyword-based failure pattern matching.

Responsibilities:
- seed(patterns)      — idempotent bulk upsert of FailurePattern records
- match(query, top_k) — keyword-overlap scoring, returns top matches ≥ 0.4
- update_seen(id)     — increment times_seen + refresh last_seen on injection
- verify(id)          — mark a pattern as human-verified

Matching algorithm (v1 — no LLM, no pgvector):
    score = |query_tokens ∩ signal_tokens| / |signal_tokens|
    Return top min(top_k, 3) patterns with score ≥ MATCH_THRESHOLD.

The pool argument to every async method is the shared psycopg_pool
AsyncConnectionPool created in workflow.py (_langgraph_pool).  Callers
that do not have a pool simply get an empty / no-op result.
"""

from __future__ import annotations

import re
from typing import List

from app.models.failure_pattern import FailurePattern
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MATCH_THRESHOLD: float = 0.4
MAX_MATCHES: int = 3        # hard cap — never return more than this

# Tokens to strip when tokenizing a query (stop-words + Kubernetes noise)
_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "must",
    "i", "my", "me", "we", "our", "you", "your", "it", "its",
    "in", "on", "at", "to", "for", "of", "with", "by", "from",
    "what", "why", "how", "when", "where", "which", "who",
    "this", "that", "these", "those", "there", "here",
    "and", "or", "but", "not", "so", "if", "then",
    "pod", "pods", "node", "nodes", "cluster", "namespace", "kubernetes", "k8s",
    "get", "show", "check", "list", "describe", "tell", "look",
    "keep", "keeps", "kept", "restarting", "crashing",
})


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS failure_patterns (
    pattern_id          TEXT PRIMARY KEY,
    type                TEXT        NOT NULL,
    signals             TEXT[]      NOT NULL,
    root_cause          TEXT        NOT NULL,
    recommended_checks  TEXT[]      NOT NULL,
    remediation_steps   TEXT[]      NOT NULL,
    confidence          FLOAT       NOT NULL DEFAULT 0.8,
    times_seen          INTEGER     NOT NULL DEFAULT 0,
    last_seen           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    verified            BOOLEAN     NOT NULL DEFAULT FALSE,
    namespace_scope     TEXT,
    -- Pre-computed search text populated at INSERT time (avoids GENERATED ALWAYS AS
    -- immutability issues on some Postgres builds with array_to_string).
    signals_text        TEXT        NOT NULL DEFAULT ''
)
"""

_CREATE_SIGNAL_GIN_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_failure_patterns_signals "
    "ON failure_patterns USING GIN (signals)"
)
_CREATE_TEXT_GIN_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_failure_patterns_signals_text "
    "ON failure_patterns USING GIN (to_tsvector('english', signals_text))"
)
_CREATE_TYPE_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_failure_patterns_type "
    "ON failure_patterns (type)"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> frozenset[str]:
    """Lowercase, split on non-word chars, drop stop-words and short tokens."""
    tokens = re.findall(r"[a-zA-Z0-9][\w\-]*", text.lower())
    return frozenset(t for t in tokens if t not in _STOP_WORDS and len(t) > 1)


def _score(query_tokens: frozenset[str], signal_tokens: frozenset[str]) -> float:
    """Keyword-overlap score in [0, 1]: matched / total_signals."""
    if not signal_tokens:
        return 0.0
    return len(query_tokens & signal_tokens) / len(signal_tokens)


def _row_to_pattern(row) -> FailurePattern:
    """Convert a psycopg row (tuple or mapping) to a FailurePattern."""
    return FailurePattern(
        pattern_id=row[0],
        type=row[1],
        signals=list(row[2]),
        root_cause=row[3],
        recommended_checks=list(row[4]),
        remediation_steps=list(row[5]),
        confidence=row[6],
        times_seen=row[7],
        last_seen=row[8],
        verified=row[9],
        namespace_scope=row[10],
    )


# ---------------------------------------------------------------------------
# FailurePatternService
# ---------------------------------------------------------------------------

class FailurePatternService:
    """
    Async service for failure pattern CRUD and keyword matching.

    All methods accept *pool* as a parameter (same pool as the rest of the
    async services) so no singleton pool is held here.
    """

    # ── Schema bootstrap ────────────────────────────────────────────────────

    @staticmethod
    async def setup_schema(pool) -> None:
        """Create the failure_patterns table and indexes if they don't exist."""
        try:
            async with pool.connection() as conn:
                await conn.execute(_CREATE_TABLE_SQL)
                await conn.execute(_CREATE_SIGNAL_GIN_IDX)
                await conn.execute(_CREATE_TEXT_GIN_IDX)
                await conn.execute(_CREATE_TYPE_IDX)
            logger.info("failure_patterns schema verified/created.")
        except Exception as exc:
            logger.warning("Could not create failure_patterns schema: %s", exc)

    # ── seed ────────────────────────────────────────────────────────────────

    @staticmethod
    async def seed(patterns: List[FailurePattern], pool) -> int:
        """
        Idempotent bulk upsert of *patterns*.

        Skips any pattern whose pattern_id already exists so re-running the
        seed script is safe.  Returns the number of newly inserted rows.
        """
        if not patterns or not pool:
            return 0
        inserted = 0
        try:
            async with pool.connection() as conn:
                for p in patterns:
                    result = await conn.execute(
                        """
                        INSERT INTO failure_patterns
                            (pattern_id, type, signals, root_cause,
                             recommended_checks, remediation_steps,
                             confidence, times_seen, last_seen,
                             verified, namespace_scope, signals_text)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (pattern_id) DO NOTHING
                        """,
                        (
                            p.pattern_id,
                            p.type,
                            p.signals,
                            p.root_cause,
                            p.recommended_checks,
                            p.remediation_steps,
                            p.confidence,
                            p.times_seen,
                            p.last_seen,
                            p.verified,
                            p.namespace_scope,
                            " ".join(p.signals),
                        ),
                    )
                    if result.rowcount:
                        inserted += 1
            logger.info("seed: inserted %d / %d failure patterns.", inserted, len(patterns))
        except Exception as exc:
            logger.error("seed: failed to insert failure patterns: %s", exc)
        return inserted

    # ── match ────────────────────────────────────────────────────────────────

    @staticmethod
    async def match(
        query: str,
        pool,
        top_k: int = 3,
    ) -> List[FailurePattern]:
        """
        Return up to min(top_k, MAX_MATCHES) patterns whose signal overlap
        with *query* exceeds MATCH_THRESHOLD (0.4).

        Score = |query_tokens ∩ signal_tokens| / |signal_tokens|.
        Results are sorted by score descending.
        """
        if not query or not pool:
            return []

        effective_k = min(top_k, MAX_MATCHES)
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        try:
            async with pool.connection() as conn:
                rows = await conn.execute(
                    """
                    SELECT pattern_id, type, signals, root_cause,
                           recommended_checks, remediation_steps,
                           confidence, times_seen, last_seen,
                           verified, namespace_scope
                    FROM failure_patterns
                    """,
                )
                all_rows = await rows.fetchall()
        except Exception as exc:
            logger.debug("match: could not query failure_patterns: %s", exc)
            return []

        scored: list[tuple[float, FailurePattern]] = []
        for row in all_rows:
            pattern = _row_to_pattern(row)
            signal_tokens = _tokenize(" ".join(pattern.signals))
            score = _score(query_tokens, signal_tokens)
            if score >= MATCH_THRESHOLD:
                # Blend the keyword score with the stored confidence
                blended = score * pattern.confidence
                scored.append((blended, pattern))

        scored.sort(key=lambda t: t[0], reverse=True)

        results = [p for _, p in scored[:effective_k]]
        if results:
            logger.debug(
                "match: query=%r → %d match(es), top=%s (score=%.2f)",
                query[:60],
                len(results),
                results[0].pattern_id,
                scored[0][0],
            )
        return results

    # ── update_seen ──────────────────────────────────────────────────────────

    @staticmethod
    async def update_seen(pattern_id: str, pool) -> None:
        """
        Increment times_seen and refresh last_seen for *pattern_id*.
        Called after every injection so frequency tracking works from day one.
        Non-fatal: errors are logged at DEBUG and swallowed.
        """
        if not pattern_id or not pool:
            return
        try:
            async with pool.connection() as conn:
                await conn.execute(
                    """
                    UPDATE failure_patterns
                    SET times_seen = times_seen + 1,
                        last_seen  = NOW()
                    WHERE pattern_id = %s
                    """,
                    (pattern_id,),
                )
            logger.debug("update_seen: pattern_id=%s", pattern_id)
        except Exception as exc:
            logger.debug("update_seen: failed for %s: %s", pattern_id, exc)

    # ── verify ────────────────────────────────────────────────────────────────

    @staticmethod
    async def verify(pattern_id: str, pool) -> bool:
        """
        Mark *pattern_id* as human-verified.  Returns True on success.
        """
        if not pattern_id or not pool:
            return False
        try:
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE failure_patterns SET verified = TRUE WHERE pattern_id = %s",
                    (pattern_id,),
                )
            logger.info("verify: pattern_id=%s marked verified.", pattern_id)
            return True
        except Exception as exc:
            logger.warning("verify: failed for %s: %s", pattern_id, exc)
            return False
