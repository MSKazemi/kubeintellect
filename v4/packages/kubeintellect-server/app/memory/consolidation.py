"""Consolidation worker — the hippocampus "sleep" phase (ADR-002).

v1 passes (all deterministic — no LLM calls; CodeAD-style learned-predicate
synthesis is deferred until enough verified patterns accumulate in production):

1. Backfill episodes from legacy rca_outcomes (startup only, idempotent).
2. Graph maintenance: close KG edges whose pod entity has not been observed
   recently (the watch DELETED path normally closes them; this catches
   watcher downtime).
3. Detector-candidate proposal: verified failure_patterns with
   occurrence_count >= 2 whose matched playbooks all carry compiled detect
   blocks get a `detectors` candidate row (status=candidate, human review
   required before activation — ADR-006).

Failure discipline: each pass is independently guarded; one failing pass
never stops the loop.
"""
from __future__ import annotations

import asyncio
import json
import time

from app.memory import pass_health
from app.utils.logger import get_logger

logger = get_logger(__name__)

_INTERVAL_SECONDS = 600       # between consolidation passes
_STALE_EDGE_HOURS = 24        # open edges older than this with no fresh pod attr
_last_watchdog_sweep = 0.0    # wallclock of the last change-watchdog sweep (dedup by timestamp)


async def _sweep_change_watchdogs() -> int:
    """P4: arm read-only watchdog investigations for changes recorded since the last sweep.

    Periodic + timestamp-deduped, so a change is watched once. Only changes newer than the last
    sweep are considered (no re-investigation). Fires the real fan-out dispatch. Returns count."""
    global _last_watchdog_sweep
    from app.cluster_id import get_cluster_id
    from app.core.config import settings
    from app.cortex.change_rca import recent_changes
    from app.sensorium import change_watchdog, watchdog_dispatch
    watchdog_dispatch.install()
    try:
        cid = get_cluster_id()
    except Exception:
        cid = "unknown"
    now = time.time()
    tasks = change_watchdog.plan_watchdogs(
        recent_changes(cid), now_epoch=now, since_epoch=_last_watchdog_sweep,
        ttl_seconds=settings.KI_V5_WATCHDOG_TTL_SECONDS, max_active=settings.KI_V5_WATCHDOG_MAX_ACTIVE,
    )
    _last_watchdog_sweep = now
    return change_watchdog.fire(tasks)


async def run_consolidation_once(startup: bool = False) -> dict:
    """Run all passes once; returns per-pass counters (for tests/digest)."""
    from app.memory import episodes, service

    stats: dict[str, int] = {}
    if not service.memory_active():
        return stats  # memory is off or down; `memory_status()` is the authority on which

    if startup:
        try:
            from app.cluster_id import get_cluster_id
            default_cluster = get_cluster_id()
        except Exception:
            default_cluster = "unknown"
        stats["backfilled"] = await episodes.backfill_from_rca_outcomes(default_cluster)

    stats["stale_edges_closed"] = await _close_stale_edges()
    stats["detector_candidates"] = await _propose_detector_candidates()

    # Preference memory: learn from behaviour, then forget stale inferred prefs.
    from app.core.config import settings
    if settings.PREFERENCE_MEMORY_ENABLED:
        from app.memory import preferences
        stats["prefs_inferred"] = await preferences.infer_from_behaviour()
        stats["prefs_forgotten"] = await preferences.decay_and_forget()

    # Memory V5 P5 (ADR-016): promote verified, recurring episodes into semantic rules.
    if settings.MEMORY_PROMOTION:
        from app.memory import promotion
        stats["rules_promoted"] = await promotion.promote_from_episodes()

    # Memory V5 P6 (ADR-017): fire due prospective re-checks ("did the fix hold?").
    if settings.MEMORY_PROSPECTIVE:
        from app.memory import prospective
        stats["prospective_fired"] = await prospective.run_prospective_once()

    # Memory V5 P8 (spec R7): (re)build the theme summary tree, tied to KG change-rate.
    if settings.MEMORY_SUMMARY_TREE:
        from app.memory import summaries
        stats["summaries_built"] = await summaries.build_summary_tree()

    # v5 P2 L0 file plane: regenerate CLUSTER.md + MEMORY.md projections (files = projection).
    if settings.CORTEX_V5_ENABLED and settings.KI_V5_FILE_PLANE:
        from app.memory import file_plane
        try:
            from app.cluster_id import get_cluster_id
            cid = get_cluster_id()
        except Exception:
            cid = "unknown"
        written = await file_plane.regenerate_file_plane(
            cid, settings.KI_V5_FILE_PLANE_DIR, max_bytes=settings.KI_V5_FILE_PLANE_MAX_BYTES,
        )
        stats["file_plane_bytes"] = written["cluster_md_bytes"] + written["memory_md_bytes"]

    # v5 P4 change-watchdog: arm read-only investigations for changes since the last sweep.
    if settings.CORTEX_V5_ENABLED and settings.KI_V5_CHANGE_WATCHDOG:
        stats["watchdogs_fired"] = await _sweep_change_watchdogs()

    # Retention (enterprise A10): the only pass that DELETES. Bounded per table per pass, and
    # it refuses the hash-chained ledgers outright — see `retention.REFUSED`.
    if settings.MEMORY_RETENTION_DAYS > 0:
        from app.memory import retention
        stats["rows_pruned"] = await retention.prune_once()

    # Every pass above returns 0 for "nothing to do" AND for "it raised and I caught it", so the
    # counters alone cannot tell a healthy idle cluster from a dead subsystem — measured, they are
    # byte-identical. The register is what separates them; see `app.memory.pass_health`.
    # Read "did anything happen?" off the counters BEFORE the failure count joins them, so the
    # test does not have to exclude a key from itself.
    did_work = any(stats.values())
    failures = pass_health.drain()
    stats["failed_passes"] = len(failures)
    if failures:
        detail = "; ".join(f"{name}: {why}" for name, why in failures)
        logger.warning(
            f"consolidation_pass INCOMPLETE — {len(failures)} of {len(stats) - 1} passes failed "
            f"[{detail}] · counters {json.dumps(stats)}"
        )
    elif did_work:
        logger.info(f"consolidation_pass {json.dumps(stats)}")
    return stats


async def consolidation_loop() -> None:
    while True:
        try:
            await asyncio.sleep(_INTERVAL_SECONDS)
            await run_consolidation_once()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning(f"consolidation: pass failed: {exc}")


async def _close_stale_edges() -> int:
    from app.memory import service

    pool = service._pool
    if pool is None:
        return 0
    try:
        result = await pool.execute(
            f"""
            UPDATE kg_edges SET valid_to = now()
            WHERE valid_to IS NULL
              AND valid_from < now() - INTERVAL '{_STALE_EDGE_HOURS} hours'
              AND src IN (
                  SELECT id FROM kg_entities e
                  WHERE e.kind = 'Pod'
                    AND COALESCE((e.attrs->>'last_seen')::float, 0) <
                        EXTRACT(EPOCH FROM now() - INTERVAL '{_STALE_EDGE_HOURS} hours')
              )
            """
        )
        return int(result.split()[-1]) if result else 0
    except Exception as exc:
        logger.warning(f"consolidation: stale-edge pass failed: {exc}")
        pass_health.record_failure("stale_edges_closed", exc)
        return 0


async def _propose_detector_candidates() -> int:
    """Verified, recurring patterns whose playbooks have compiled detect blocks
    become reviewable detector candidates (deterministic derivation)."""
    from app.agent.playbooks import get_playbook
    from app.memory import service

    pool = service._pool
    if pool is None:
        return 0
    try:
        patterns = await pool.fetch(
            """
            SELECT pattern_name, cluster_id, description
            FROM failure_patterns
            WHERE occurrence_count >= 2 AND confidence >= 0.9
              AND COALESCE(demoted, FALSE) = FALSE
            """
        )
    except Exception as exc:
        logger.warning(f"consolidation: pattern fetch failed: {exc}")
        pass_health.record_failure("detector_candidates", exc)
        return 0

    created = 0
    for pattern in patterns:
        name = pattern["pattern_name"]
        playbook_names = _playbooks_from_key(name)
        blocks: list[dict] = []
        for pb_name in playbook_names:
            pb = get_playbook(pb_name)
            det = getattr(pb, "detect", None) if pb else None
            if det is None:
                blocks = []
                break
            blocks.append(
                {
                    "playbook": det.playbook,
                    "promql": list(det.promql),
                    "debounce_seconds": det.debounce_seconds,
                }
            )
        if not blocks:
            continue
        predicate = {"derived_from_playbooks": blocks, "pattern": name}
        try:
            result = await pool.execute(
                """
                INSERT INTO detectors (cluster_id, name, source, predicate, created_from)
                VALUES ($1, $2, 'learned', $3::jsonb, $4)
                ON CONFLICT (cluster_id, name) DO NOTHING
                """,
                pattern["cluster_id"] or "global",
                f"learned:{name[:80]}",
                json.dumps(predicate),
                name,
            )
            if result and result.endswith("1"):
                created += 1
        except Exception as exc:
            logger.warning(f"consolidation: candidate insert failed: {exc}")
    return created


def _playbooks_from_key(pattern_name: str) -> list[str]:
    """Parse 'playbook=A+B | ns=... | cluster=...' structured keys (reflexion R2)."""
    if not pattern_name.startswith("playbook="):
        return []
    head = pattern_name.split("|", 1)[0].strip()
    return [p for p in head.removeprefix("playbook=").split("+") if p]
