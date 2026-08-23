"""Detector candidate review — the human-in-the-loop promotion gate (ADR-012).

State machine: candidate → shadow (accruing precision) → active | demoted.
Only `active` detectors reach the watchtower; promotion is always a human action.
"""
from __future__ import annotations

import json

from app.utils.logger import get_logger

logger = get_logger(__name__)


class DetectorStoreUnavailable(RuntimeError):
    """The detector store could not be read — as distinct from holding no detectors.

    These are different answers to "which detectors do I have?" and collapsing them is how an
    operator concludes their cluster has no coverage when in fact the question was never answered.
    `list_detectors` used to return `[]` for both a missing pool and a failed query, and the caller
    had no way to tell; the server logged a warning nobody reading the CLI would ever see.
    """


async def list_detectors(status: str | None = None, cluster_id: str = "global") -> list[dict]:
    """Detectors for a cluster.

    Raises `DetectorStoreUnavailable` when the store cannot be read. An empty list means exactly
    one thing: the store was read and holds nothing.
    """
    from app.memory import service

    pool = service._pool
    if pool is None:
        raise DetectorStoreUnavailable("no memory pool — the detector store is not configured")
    try:
        if status:
            rows = await pool.fetch(
                "SELECT name, source, status, predicate, precision_stats, created_from,"
                " reviewed_by, created_at FROM detectors"
                " WHERE cluster_id = $1 AND status = $2 ORDER BY created_at DESC",
                cluster_id, status,
            )
        else:
            rows = await pool.fetch(
                "SELECT name, source, status, predicate, precision_stats, created_from,"
                " reviewed_by, created_at FROM detectors"
                " WHERE cluster_id = $1 ORDER BY created_at DESC",
                cluster_id,
            )
    except Exception as exc:
        logger.warning(f"detector_review: list failed: {exc}")
        raise DetectorStoreUnavailable(f"detector store query failed: {exc}") from exc
    out = []
    for r in rows:
        d = dict(r)
        for k in ("predicate", "precision_stats"):
            if isinstance(d.get(k), str):
                try:
                    d[k] = json.loads(d[k])
                except json.JSONDecodeError:
                    pass
        if d.get("created_at") is not None:
            d["created_at"] = d["created_at"].isoformat()
        out.append(d)
    return out


async def _set_status(name: str, status: str, reviewer: str, cluster_id: str) -> bool:
    from app.memory import service

    pool = service._pool
    if pool is None:
        return False
    try:
        result = await pool.execute(
            "UPDATE detectors SET status = $1, reviewed_by = $2"
            " WHERE cluster_id = $3 AND name = $4",
            status, reviewer, cluster_id, name,
        )
        ok = bool(result and result.rsplit(" ", 1)[-1] != "0")
        if ok:
            logger.info(f"detector_review: {name} -> {status} by {reviewer}")
        return ok
    except Exception as exc:
        logger.warning(f"detector_review: set_status failed: {exc}")
        return False


async def promote_candidate(name: str, reviewer: str, cluster_id: str = "global") -> bool:
    """Promote a shadow/candidate detector to active (it now reaches the watchtower)."""
    return await _set_status(name, "active", reviewer, cluster_id)


async def demote_candidate(name: str, reviewer: str, cluster_id: str = "global") -> bool:
    """Demote/reject a detector — it stops firing entirely."""
    return await _set_status(name, "demoted", reviewer, cluster_id)
