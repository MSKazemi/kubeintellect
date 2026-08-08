"""Detector candidate review — the human-in-the-loop promotion gate (ADR-012).

State machine: candidate → shadow (accruing precision) → active | demoted.
Only `active` detectors reach the watchtower; promotion is always a human action.
"""
from __future__ import annotations

import json

from app.utils.logger import get_logger

logger = get_logger(__name__)


async def list_detectors(status: str | None = None, cluster_id: str = "global") -> list[dict]:
    from app.memory import service

    pool = service._pool
    if pool is None:
        return []
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
        return []
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
