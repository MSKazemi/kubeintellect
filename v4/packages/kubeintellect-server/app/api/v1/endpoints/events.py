"""GET /v1/events/replay/{session_id} — replay stored events for debugging."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.streaming.emitter import get_history, has_history

router = APIRouter()


@router.get("/events/replay/{session_id}")
async def replay_events(session_id: str):
    """
    Stream all events recorded for *session_id* as SSE, then send [DONE].

    Useful for post-mortem debugging: re-run the event sequence for any
    session without replaying the actual LLM/kubectl calls.

    404 when this process holds no history for the session, and the detail says why that is
    not the same as the session having produced nothing. Until 2026-08-24 all three of
    "never seen here", "lost to a restart or another replica" and "ran and emitted nothing"
    answered `200` with a lone `[DONE]` frame — so a UI reconnecting to a different pod
    rendered a real investigation as an empty one. `/v1/episodes/{id}/replay`, one file over,
    goes to some length to avoid exactly this conflation.
    """
    if not has_history(session_id):
        raise HTTPException(
            status_code=404,
            detail=(
                f"this process holds no event history for session '{session_id}'. That history "
                f"is in memory only — it does not survive a restart and is not shared between "
                f"replicas — so this is NOT evidence that the session never ran or produced "
                f"nothing. For a durable answer read GET /v1/episodes/{session_id}/replay, "
                f"which streams the hash-chained flight recorder."
            ),
        )

    history = get_history(session_id)
    # A meta frame first, as the episode replay does. Without it a zero-event replay is a bare
    # `[DONE]`, and the client cannot tell "this session emitted nothing" from "I got cut off
    # before the first frame". `durable: false` names what this stream is worth.
    meta = {
        "type": "replay_meta",
        "session_id": session_id,
        "records": len(history),
        "durable": False,
    }

    async def _gen():
        yield f"data: {json.dumps(meta)}\n\n"
        for event in history:
            yield f"data: {json.dumps(event)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
