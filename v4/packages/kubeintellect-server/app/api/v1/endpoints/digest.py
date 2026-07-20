"""GET /v1/digest — the morning digest, rendered from the flight recorder."""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.digest.builder import build_digest, render_markdown

router = APIRouter()


@router.get("/digest")
async def get_digest(
    hours: float = Query(default=24.0, gt=0, le=168),
    format: str = Query(default="json", pattern="^(json|markdown)$"),
):
    digest = await build_digest(hours=hours)
    if format == "markdown":
        return {"markdown": render_markdown(digest)}
    return digest
