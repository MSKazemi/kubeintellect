"""GET /v1/episodes/{episode_id}/replay — durable, tamper-evident replay (ADR-005).

Unlike /v1/events/replay/{session_id} (in-memory, lost on restart), this reads
the hash-chained decision_log and verifies chain integrity before streaming.
The first SSE frame is a meta record:

    {"type": "replay_meta", "episode_id": ..., "records": N, "chain_valid": bool}

followed by each recorded event payload in seq order, then [DONE].
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.db.flight_recorder import RecorderUnavailable, fetch_episode, verify_episode

router = APIRouter()


@router.get("/episodes/{episode_id}/replay")
async def replay_episode(episode_id: str):
    try:
        rows = await fetch_episode(episode_id)
    except RecorderUnavailable as exc:
        # 503, never 404. A 404 is a positive claim that the episode does not exist, and this
        # endpoint is the audit surface — telling an operator mid-incident that the episode they
        # are living through was never recorded, because the recorder happens to be off, is the
        # most expensive wrong answer this API can give.
        raise HTTPException(status_code=503, detail=f"{exc} — this is not the same as episode "
                                                    f"'{episode_id}' having no records") from exc
    verdict = await verify_episode(episode_id, rows)
    if not rows:
        # 404 says "this never existed". For an episode whose chain anchor survives but whose
        # rows do not, that is the one wrong answer this endpoint must not give: it launders a
        # total truncation into an absence. 409 says the store contradicts itself.
        if not verdict.verified:
            # And with an unreadable anchor there is no basis for *either* claim: absence and
            # total truncation look identical from here. Answering 404 would pick the more
            # comfortable one and state it as fact.
            raise HTTPException(
                status_code=503,
                detail=f"the chain anchor for episode '{episode_id}' could not be read, so an "
                       f"episode that never existed cannot be told apart from one whose records "
                       f"were all removed. This is NOT a statement that '{episode_id}' has no "
                       f"records.",
            )
        if not verdict.valid:
            raise HTTPException(
                status_code=409,
                detail=f"episode '{episode_id}' has no surviving records but its chain anchor "
                       f"says it had some — every record has been removed. This is NOT the "
                       f"same as the episode never existing.",
            )
        raise HTTPException(status_code=404, detail=f"no recorded episode '{episode_id}'")

    meta = {
        "type": "replay_meta",
        "episode_id": episode_id,
        "records": len(rows),
        "chain_valid": verdict.valid,
        # The third state. `chain_valid` alone cannot say whether anything was checked, and
        # `kq replay` has owned exit 4 — "chain NOT VERIFIED" — since before this field existed.
        "chain_verified": verdict.verified,
    }

    async def _gen():
        yield f"data: {json.dumps(meta)}\n\n"
        for row in rows:
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            if not isinstance(payload, dict):   # never happens; a raise here would truncate
                payload = {"value": payload}    # the stream mid-episode, so do not risk it
            # The row's `kind` is authoritative, and is what the client renders in the
            # `type` column. Most payloads happen to repeat it (every wire event does, and
            # `finding`/`rollback_point`/`recorder_gap` set it by hand) — but a `ki_otel_span`
            # payload never had one, so those rows replayed as type `?`. The kind is in the
            # row; there is no reason to make the client depend on the payload echoing it.
            yield f"data: {json.dumps({**payload, 'type': row['kind']}, default=str)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
