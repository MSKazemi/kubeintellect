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

from app.db.flight_recorder import fetch_episode, verify_chain

router = APIRouter()


@router.get("/episodes/{episode_id}/replay")
async def replay_episode(episode_id: str):
    rows = await fetch_episode(episode_id)
    if not rows:
        raise HTTPException(status_code=404, detail=f"no recorded episode '{episode_id}'")

    chain_valid = verify_chain(rows)
    meta = {
        "type": "replay_meta",
        "episode_id": episode_id,
        "records": len(rows),
        "chain_valid": chain_valid,
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
