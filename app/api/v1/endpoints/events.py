"""GET /v1/events/replay/{session_id} — replay stored events for debugging."""
from __future__ import annotations

import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.streaming.emitter import get_history

router = APIRouter()


@router.get("/events/replay/{session_id}")
async def replay_events(session_id: str):
    """
    Stream all events recorded for *session_id* as SSE, then send [DONE].

    Useful for post-mortem debugging: re-run the event sequence for any
    session without replaying the actual LLM/kubectl calls.
    """
    history = get_history(session_id)

    async def _gen():
        for event in history:
            yield f"data: {json.dumps(event)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
