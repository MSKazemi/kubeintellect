"""GET /v1/findings — detector firings (zero-token known-failure detection)."""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.detectors.perception import DISABLED, perception_state
from app.detectors.service import get_engine
from app.sensorium.k8s_watcher import queue_stats

router = APIRouter()


@router.get("/findings")
async def list_findings(
    limit: int = Query(default=100, ge=1, le=500),
    since: float = Query(default=0.0, ge=0.0),
):
    """Recent findings from the detector engine (in-memory ring; the durable
    record lives in the flight recorder under episode findings:<cluster_id>).

    `sensorium` and `predictive` come from `detectors.perception`, which the
    morning digest reads too — the two surfaces answer for the same window and
    must not be able to disagree about whether anything was watching.
    """
    engine = get_engine()
    state = perception_state(engine)
    if state.sensorium == DISABLED:
        return {"sensorium": DISABLED, "streams": [], "queue": queue_stats(), "findings": []}

    return {
        "sensorium": state.sensorium,
        "detectors": state.detectors,
        "predictive": state.predictive,
        "predictive_detectors": state.predictive_detectors,
        "predictive_error": state.predictive_error,
        "streams": state.streams,
        # Queue depth and shed count. `shed_total > 0` means the sensorium is DROPPING
        # observations — detection is lossy and this endpoint is the only place that says so.
        # Reported next to `streams` because "connected but shedding" and "not connected" are
        # different failures with the same symptom: findings that should have fired and did not.
        "queue": queue_stats(),
        "findings": engine.recent_findings(limit=limit, since=since) if engine else [],
    }
