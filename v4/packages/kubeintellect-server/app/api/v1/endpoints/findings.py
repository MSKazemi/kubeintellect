"""GET /v1/findings — detector firings (zero-token known-failure detection)."""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.detectors.service import get_engine

router = APIRouter()


@router.get("/findings")
async def list_findings(
    limit: int = Query(default=100, ge=1, le=500),
    since: float = Query(default=0.0, ge=0.0),
):
    """Recent findings from the detector engine (in-memory ring; the durable
    record lives in the flight recorder under episode findings:<cluster_id>)."""
    engine = get_engine()
    if engine is None:
        return {"sensorium": "disabled", "findings": []}
    return {
        "sensorium": "active",
        "detectors": len(engine.detectors),
        "findings": engine.recent_findings(limit=limit, since=since),
    }
