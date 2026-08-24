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
        # The verdict fields ride ALONGSIDE the prose, not instead of it. `format=markdown` used
        # to return `{"markdown": ...}` only, so the audit-chain verdict reached the caller as
        # four possible English banners and nothing else — `kq postmortem` could render the
        # tamper warning and still exit 0, because it had no datum to decide on. `kq replay` and
        # `kq export` both map the same verdict to exit 3/4/5; this is what lets the third
        # command follow the same documented convention. Additive: existing callers that read
        # only "markdown" are unaffected.
        return {
            "markdown": render_markdown(pm),
            # Both defaults MIRROR `render_markdown`'s, and that is the whole point: the caller
            # maps these to an exit code and prints the markdown above it, so a default that
            # disagreed with the renderer's would emit two verdicts for one episode. A stored
            # postmortem predating `chain_verified` renders "verified intact" by deliberate
            # decision (test_an_unverified_chain_is_not_a_broken_one.py) — so it must not also
            # exit 4. `chain_valid` is indexed directly by the renderer, so its default here is
            # unreachable via this endpoint; it is kept fail-closed for the day that changes.
            "chain_valid": pm.get("chain_valid", False),
            "chain_verified": pm.get("chain_verified", True),
            "events_lost": pm.get("events_lost", 0),
            "gaps": pm.get("gaps", []),
            "enrichment_failed": pm.get("enrichment_failed", []),
        }
    return pm
