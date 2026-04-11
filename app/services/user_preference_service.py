# app/services/user_preference_service.py
"""
UserPreferenceService — per-user preference detection, storage, and retrieval.

Preference keys (use the named constants below — never bare strings):
    PREF_VERBOSITY           "verbosity"         concise | verbose | default
    PREF_FORMAT              "format"            root-cause-first | step-by-step | default
    PREF_DEFAULT_NAMESPACE   "default_namespace" any string
    PREF_DEFAULT_CLUSTER     "default_cluster"   any string
    PREF_REMEDIATION_STYLE   "remediation_style" conservative | aggressive | default

Storage rule: one row per (user_id, preference_key).
Upsert rule:  only overwrite when incoming confidence >= stored confidence.
              This means a manually-set confidence-1.0 preference can never be
              clobbered by a low-confidence heuristic.

Hidden keys (prefix "_") are used internally for heuristic counting and are
never returned by load() or included in the injected SystemMessage.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Dict, Optional

from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")

# ---------------------------------------------------------------------------
# Preference key constants
# ---------------------------------------------------------------------------

PREF_VERBOSITY          = "verbosity"
PREF_FORMAT             = "format"
PREF_DEFAULT_NAMESPACE  = "default_namespace"
PREF_DEFAULT_CLUSTER    = "default_cluster"
PREF_REMEDIATION_STYLE  = "remediation_style"

# Sentinel value meaning "user has not expressed a preference"
DEFAULT_VALUE = "default"

# Returned by load() for any key not yet stored
PREF_DEFAULTS: Dict[str, Optional[str]] = {
    PREF_VERBOSITY:         DEFAULT_VALUE,
    PREF_FORMAT:            DEFAULT_VALUE,
    PREF_DEFAULT_NAMESPACE: None,
    PREF_DEFAULT_CLUSTER:   None,
    PREF_REMEDIATION_STYLE: DEFAULT_VALUE,
}

# Keys that are surfaced to the injection layer
_PUBLIC_KEYS = frozenset(PREF_DEFAULTS.keys())

# ---------------------------------------------------------------------------
# Heuristic patterns for explicit instruction detection
# ---------------------------------------------------------------------------

_HEURISTIC_PATTERNS: list[tuple[re.Pattern, str, str, float]] = [
    # (compiled pattern, pref_key, pref_value, confidence)
    (re.compile(r"keep.{0,10}short|be.{0,10}concise|\bbrief\b|\bsummariz", re.I),
     PREF_VERBOSITY, "concise", 1.0),
    (re.compile(r"more detail|verbose|explain.{0,10}fully|step.{0,5}by.{0,5}step", re.I),
     PREF_VERBOSITY, "verbose", 1.0),
    (re.compile(r"root.{0,5}cause.{0,10}first|start.{0,10}with.{0,10}cause|why.{0,10}first", re.I),
     PREF_FORMAT, "root-cause-first", 1.0),
    (re.compile(r"don.{0,3}t.{0,10}change|read.?only|\bsafe\b|no.{0,10}modif", re.I),
     PREF_REMEDIATION_STYLE, "conservative", 1.0),
]

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id          TEXT        NOT NULL,
    preference_key   TEXT        NOT NULL,
    preference_value TEXT        NOT NULL,
    confidence       FLOAT       NOT NULL DEFAULT 0.5,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, preference_key)
)
"""
_CREATE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_user_preferences_user_id "
    "ON user_preferences (user_id)"
)


# ---------------------------------------------------------------------------
# UserPreferenceService
# ---------------------------------------------------------------------------

class UserPreferenceService:
    """
    Async service for user preference CRUD and heuristic detection.

    All methods accept *pool* (the shared psycopg_pool AsyncConnectionPool).
    """

    # ── Schema bootstrap ────────────────────────────────────────────────────

    @staticmethod
    async def setup_schema(pool) -> None:
        """Create the user_preferences table and index if they don't exist."""
        try:
            async with pool.connection() as conn:
                await conn.execute(_CREATE_TABLE_SQL)
                await conn.execute(_CREATE_INDEX_SQL)
            logger.info("user_preferences schema verified/created.")
        except Exception as exc:
            logger.warning("Could not create user_preferences schema: %s", exc)

    # ── load ────────────────────────────────────────────────────────────────

    @staticmethod
    async def load(user_id: str, pool) -> Dict[str, Optional[str]]:
        """
        Return current public preferences for *user_id*.

        Falls back to PREF_DEFAULTS for any key not yet stored.
        Hidden keys (starting with '_') are never returned.
        """
        result = dict(PREF_DEFAULTS)
        if not user_id or not pool:
            return result
        try:
            async with pool.connection() as conn:
                rows = await conn.execute(
                    "SELECT preference_key, preference_value "
                    "FROM user_preferences "
                    "WHERE user_id = %s AND preference_key NOT LIKE '\\%%_'",
                    (user_id,),
                )
                for row in await rows.fetchall():
                    key, val = row[0], row[1]
                    if key in _PUBLIC_KEYS:
                        result[key] = val
        except Exception as exc:
            logger.error(
                "preference_load_failed user_id=%s error=%s — falling back to defaults",
                user_id, exc,
            )
        return result

    # ── upsert ──────────────────────────────────────────────────────────────

    @staticmethod
    async def upsert(
        user_id: str,
        key: str,
        value: str,
        confidence: float,
        pool,
    ) -> None:
        """
        Insert or update a single preference.

        Only updates if *confidence* >= the currently stored confidence.
        This ensures high-confidence explicit instructions are never clobbered
        by low-confidence heuristics.
        """
        if not user_id or not key or not pool:
            return
        try:
            async with pool.connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO user_preferences
                        (user_id, preference_key, preference_value, confidence, updated_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (user_id, preference_key) DO UPDATE SET
                        preference_value = EXCLUDED.preference_value,
                        confidence       = EXCLUDED.confidence,
                        updated_at       = NOW()
                    WHERE user_preferences.confidence <= EXCLUDED.confidence
                    """,
                    (user_id, key, value, confidence),
                )
            logger.debug(
                "upsert: user=%s key=%s value=%s confidence=%.2f",
                user_id, key, value, confidence,
            )
        except Exception as exc:
            logger.debug("upsert: failed for user %s key %s: %s", user_id, key, exc)

    # ── detect_and_save ──────────────────────────────────────────────────────

    @classmethod
    async def detect_and_save(
        cls,
        user_id: str,
        conversation_history: list,
        current_namespace: Optional[str],
        current_cluster: Optional[str],
        pool,
    ) -> None:
        """
        Run detection heuristics and persist any discovered preferences.

        Never raises — all errors are swallowed and logged at DEBUG.
        Intended to be fire-and-forget via asyncio.create_task().

        Heuristics:
        H1 — repeated namespace: last 5 conversation_context rows for this user.
             If same namespace appears 3+ times → upsert default_namespace @ 0.7.
        H2 — explicit instruction: scan last user message for pattern matches.
             Matched patterns upsert at confidence 1.0.
        H3 — repeated cluster: same as H1 but tracked via a hidden counting key
             since cluster is not stored in conversation_context.
        """
        if not user_id or not pool:
            return
        try:
            await asyncio.gather(
                cls._heuristic_namespace(user_id, current_namespace, pool),
                cls._heuristic_explicit(user_id, conversation_history, pool),
                cls._heuristic_cluster(user_id, current_cluster, pool),
                return_exceptions=True,
            )
        except Exception as exc:
            logger.debug("detect_and_save: unexpected error for user %s: %s", user_id, exc)

    # ── private heuristic helpers ────────────────────────────────────────────

    @classmethod
    async def _heuristic_namespace(
        cls, user_id: str, current_namespace: Optional[str], pool
    ) -> None:
        """H1: if the same namespace appears in ≥ 3 of the last 5 conversations, set it."""
        try:
            async with pool.connection() as conn:
                rows = await conn.execute(
                    """
                    SELECT context_json->>'namespace'
                    FROM   conversation_context
                    WHERE  user_id = %s
                      AND  context_json->>'namespace' IS NOT NULL
                    ORDER  BY updated_at DESC
                    LIMIT  5
                    """,
                    (user_id,),
                )
                namespaces = [r[0] for r in await rows.fetchall() if r[0]]

            if not namespaces:
                return

            # Include the current request's namespace in the tally
            if current_namespace:
                namespaces.append(current_namespace)

            counts: Dict[str, int] = {}
            for ns in namespaces:
                counts[ns] = counts.get(ns, 0) + 1

            top_ns, top_count = max(counts.items(), key=lambda kv: kv[1])
            if top_count >= 3:
                await cls.upsert(user_id, PREF_DEFAULT_NAMESPACE, top_ns, 0.7, pool)
        except Exception as exc:
            logger.debug("_heuristic_namespace: user=%s: %s", user_id, exc)

    @classmethod
    async def _heuristic_explicit(
        cls, user_id: str, conversation_history: list, pool
    ) -> None:
        """H2: scan last user message for explicit preference instructions."""
        try:
            last_user_msg = ""
            for msg in reversed(conversation_history):
                # Support both LangChain message objects and {"role": ..., "content": ...} dicts
                role = getattr(msg, "type", None) or (msg.get("role") if isinstance(msg, dict) else None)
                content = getattr(msg, "content", None) or (msg.get("content") if isinstance(msg, dict) else None)
                if role in ("human", "user") and content:
                    last_user_msg = content
                    break

            if not last_user_msg:
                return

            for pattern, key, value, confidence in _HEURISTIC_PATTERNS:
                if pattern.search(last_user_msg):
                    await cls.upsert(user_id, key, value, confidence, pool)
        except Exception as exc:
            logger.debug("_heuristic_explicit: user=%s: %s", user_id, exc)

    @classmethod
    async def _heuristic_cluster(
        cls, user_id: str, current_cluster: Optional[str], pool
    ) -> None:
        """
        H3: track cluster sightings via a hidden counting key.

        Stores _cluster_sightings as a JSON dict {cluster_name: count} in
        user_preferences.  When a count reaches 3, upserts default_cluster @ 0.7.
        """
        if not current_cluster:
            return
        try:
            _KEY = "_cluster_sightings"
            sightings: Dict[str, int] = {}

            async with pool.connection() as conn:
                row_result = await conn.execute(
                    "SELECT preference_value FROM user_preferences "
                    "WHERE user_id = %s AND preference_key = %s",
                    (user_id, _KEY),
                )
                row = await row_result.fetchone()
                if row:
                    try:
                        sightings = json.loads(row[0])
                    except (json.JSONDecodeError, TypeError):
                        sightings = {}

            sightings[current_cluster] = sightings.get(current_cluster, 0) + 1

            # Persist updated sightings (confidence 0 — never surfaces in load())
            await cls.upsert(user_id, _KEY, json.dumps(sightings), 0.0, pool)

            if sightings[current_cluster] >= 3:
                await cls.upsert(user_id, PREF_DEFAULT_CLUSTER, current_cluster, 0.7, pool)
        except Exception as exc:
            logger.debug("_heuristic_cluster: user=%s: %s", user_id, exc)
