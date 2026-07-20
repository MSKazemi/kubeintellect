"""GET /v1/episodes/{episode_id}/postmortem — grounded incident postmortem (ADR-011)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.core.config import settings
from app.digest.postmortem import build_postmortem, render_markdown

router = APIRouter()


@router.get("/episodes/{episode_id}/postmortem")
async def get_postmortem(
    episode_id: str,
    format: str = Query(default="json", pattern="^(json|markdown)$"),
):
    if not settings.POSTMORTEM_ENABLED:
        raise HTTPException(status_code=404, detail="Postmortems are disabled.")
    pm = await build_postmortem(episode_id)
    if format == "markdown":
        return {"markdown": render_markdown(pm)}
    return pm
