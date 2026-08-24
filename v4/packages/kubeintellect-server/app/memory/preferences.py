"""Operator-preference memory — remembers how each user likes to operate.

This upgrades the thin `user_prefs` key/value table into a learned preference
layer that satisfies the "remembers user preferences … and forgets outdated
information" half of the MemoryAgent contract:

  * explicit preferences  — set by the user/operator (source='explicit',
    confidence=1.0). Never decay, never overwritten by inference.
  * inferred preferences  — derived deterministically from the user's own
    behaviour (source='inferred'). Confidence grows as the behaviour repeats
    and decays when it stops; low-confidence stale ones are forgotten.

Retrieval is bounded and cluster-agnostic (preferences are per-user). Explicit
always outranks inferred. Failure discipline matches the rest of app/memory:
every public function catches, logs, and returns a safe value — preferences
must never break a request.
"""
from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.memory import pass_health
from app.utils.logger import get_logger
from app.utils.redact import redact_secrets

logger = get_logger(__name__)

_pool = None  # asyncpg.Pool — owned by app.memory.service

# Inferred confidence never reaches 1.0 (that is reserved for explicit prefs),
# so an explicit preference always outranks an inferred one of the same key.
_INFERRED_CONFIDENCE_CAP = 0.95
_INFERRED_CONFIDENCE_STEP = 0.1


def init_preferences(pool) -> None:
    global _pool
    _pool = pool


def close_preferences() -> None:
    global _pool
    _pool = None


async def set_preference(
    user_id: str,
    key: str,
    value: str,
    *,
    source: str = "explicit",
    confidence: float | None = None,
) -> bool:
    """Upsert one preference. Returns True on success.

    source='explicit' pins confidence=1.0 and can overwrite anything.
    source='inferred' bumps occurrence_count and confidence toward the cap, but
    never clobbers an existing explicit preference (value/confidence/source of
    an explicit row are preserved on conflict).
    """
    if _pool is None or not user_id or not key:
        return False
    key = key.strip()[:120]
    value = redact_secrets(value, max_chars=300) or ""
    try:
        if source == "explicit":
            await _pool.execute(
                """
                INSERT INTO user_prefs
                  (user_id, key, value, source, confidence, occurrence_count,
                   updated_at, last_seen_at)
                VALUES ($1, $2, $3, 'explicit', 1.0, 1, now(), now())
                ON CONFLICT (user_id, key) DO UPDATE
                  SET value = EXCLUDED.value,
                      source = 'explicit',
                      confidence = 1.0,
                      updated_at = now(),
                      last_seen_at = now()
                """,
                user_id, key, value,
            )
        else:
            seed = confidence if confidence is not None else 0.4
            await _pool.execute(
                """
                INSERT INTO user_prefs
                  (user_id, key, value, source, confidence, occurrence_count,
                   updated_at, last_seen_at)
                VALUES ($1, $2, $3, 'inferred', $4, 1, now(), now())
                ON CONFLICT (user_id, key) DO UPDATE
                  SET value = CASE WHEN user_prefs.source = 'explicit'
                                   THEN user_prefs.value ELSE EXCLUDED.value END,
                      source = user_prefs.source,
                      confidence = CASE WHEN user_prefs.source = 'explicit'
                                        THEN user_prefs.confidence
                                        ELSE LEAST($5, user_prefs.confidence + $6) END,
                      occurrence_count = user_prefs.occurrence_count + 1,
                      updated_at = now(),
                      last_seen_at = now()
                """,
                user_id, key, value, min(_INFERRED_CONFIDENCE_CAP, seed),
                _INFERRED_CONFIDENCE_CAP, _INFERRED_CONFIDENCE_STEP,
            )
        return True
    except Exception as exc:
        logger.warning(f"preferences: set failed for {key}: {exc}")
        return False


class PreferenceStoreUnavailable(RuntimeError):
    """Preferences could not be read — distinct from the user having none.

    Both used to return `[]`, so `kq preference list` printed "No preferences remembered" during a
    database outage. Preferences shape how the agent behaves, so an operator reading that has every
    reason to re-enter them or conclude the feature is broken. Same treatment as
    `detectors.review.DetectorStoreUnavailable` (pass 45).
    """


async def recall_preferences(user_id: str, k: int = 8) -> list[dict[str, Any]]:
    """Active preferences for this user — explicit first, then confident/recent.

    Inferred preferences below PREFERENCE_MIN_CONFIDENCE or older than
    PREFERENCE_DECAY_DAYS are excluded (they are on their way to being
    forgotten). Explicit preferences are never filtered.
    """
    if _pool is None or not user_id:
        return []
    decay_days = max(1, int(settings.PREFERENCE_DECAY_DAYS))
    min_conf = float(settings.PREFERENCE_MIN_CONFIDENCE)
    try:
        rows = await _pool.fetch(
            f"""
            SELECT key, value, source, confidence, occurrence_count
            FROM user_prefs
            WHERE user_id = $1
              AND (
                source = 'explicit'
                OR (confidence >= $2
                    AND last_seen_at > now() - INTERVAL '{decay_days} days')
              )
            ORDER BY (source = 'explicit') DESC, confidence DESC, last_seen_at DESC
            LIMIT $3
            """,
            user_id, min_conf, k,
        )
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning(f"preferences: recall failed: {exc}")
        raise PreferenceStoreUnavailable(f"preference store query failed: {exc}") from exc


def render_preferences_block(prefs: list[dict]) -> str:
    """Compact prompt block (≤ ~150 tokens for k=8). '' when there is nothing."""
    if not prefs:
        return ""
    lines = ["## Operator preferences (remembered)"]
    for p in prefs:
        val = str(p.get("value", "")).replace("\n", " ")[:120]
        if p.get("source") == "inferred":
            conf = float(p.get("confidence") or 0.0)
            lines.append(f"- {p['key']}: {val}  (inferred, confidence {conf:.0%})")
        else:
            lines.append(f"- {p['key']}: {val}")
    return "\n".join(lines)


async def forget_preference(user_id: str, key: str) -> bool:
    """Delete one preference (explicit forgetting). Idempotent."""
    if _pool is None or not user_id or not key:
        return False
    try:
        await _pool.execute(
            "DELETE FROM user_prefs WHERE user_id = $1 AND key = $2",
            user_id, key.strip()[:120],
        )
        return True
    except Exception as exc:
        logger.warning(f"preferences: forget failed: {exc}")
        return False


async def infer_from_behaviour() -> int:
    """Deterministic learning pass — derive inferred preferences from behaviour.

    Currently learns `default_namespace`: if a user's recent RCA outcomes are
    concentrated in one namespace (a clear majority), record it as an inferred
    preference with confidence = the majority share. Runs from the consolidation
    worker. Returns the number of users updated.
    """
    if _pool is None:
        return 0
    min_occurrence = max(1, int(settings.PREFERENCE_INFER_MIN_OCCURRENCE))
    try:
        rows = await _pool.fetch(
            """
            WITH ns_counts AS (
                SELECT user_id, namespace, count(*) AS c
                FROM rca_outcomes
                WHERE namespace IS NOT NULL AND namespace <> ''
                  AND created_at > now() - INTERVAL '30 days'
                GROUP BY user_id, namespace
            ),
            totals AS (
                SELECT user_id, sum(c) AS total FROM ns_counts GROUP BY user_id
            ),
            top AS (
                SELECT DISTINCT ON (nc.user_id)
                       nc.user_id, nc.namespace, nc.c, t.total
                FROM ns_counts nc JOIN totals t USING (user_id)
                ORDER BY nc.user_id, nc.c DESC
            )
            SELECT user_id, namespace, c, total,
                   (c::float / NULLIF(total, 0)) AS share
            FROM top
            WHERE c >= $1 AND (c::float / NULLIF(total, 0)) >= 0.6
            """,
            min_occurrence,
        )
    except Exception as exc:
        logger.warning(f"preferences: inference query failed: {exc}")
        pass_health.record_failure("prefs_inferred", exc)
        return 0

    updated = 0
    for r in rows:
        conf = min(_INFERRED_CONFIDENCE_CAP, float(r["share"]))
        ok = await set_preference(
            r["user_id"], "default_namespace", r["namespace"],
            source="inferred", confidence=conf,
        )
        updated += 1 if ok else 0
    if updated:
        logger.info(f"preferences: inferred default_namespace for {updated} user(s)")
    return updated


async def decay_and_forget() -> int:
    """Forgetting pass — purge stale, low-confidence inferred preferences.

    Explicit preferences are immortal. Inferred ones that have not been
    reinforced within PREFERENCE_DECAY_DAYS AND sit below
    PREFERENCE_MIN_CONFIDENCE are deleted. Returns rows forgotten.
    """
    if _pool is None:
        return 0
    decay_days = max(1, int(settings.PREFERENCE_DECAY_DAYS))
    min_conf = float(settings.PREFERENCE_MIN_CONFIDENCE)
    try:
        result = await _pool.execute(
            f"""
            DELETE FROM user_prefs
            WHERE source = 'inferred'
              AND confidence < $1
              AND last_seen_at < now() - INTERVAL '{decay_days} days'
            """,
            min_conf,
        )
        count = int(result.split()[-1]) if result else 0
        if count:
            logger.info(f"preferences: forgot {count} stale inferred preference(s)")
        return count
    except Exception as exc:
        logger.warning(f"preferences: decay failed: {exc}")
        pass_health.record_failure("prefs_forgotten", exc)
        return 0
