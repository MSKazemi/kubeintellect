"""
POST /v1/chat/completions — OpenAI-compatible SSE streaming endpoint.

Wire format (unchanged — kube_q consumes this without modification):
  data: {"id":"...","object":"chat.completion.chunk","choices":[{"delta":{"content":"..."},"index":0}]}\n\n
  data: [DONE]\n\n

Phase 2 additions:
  • First frame carries a stream.start handshake with protocol_version.
  • SSE keepalive comments (": heartbeat") every 15 s of queue silence.
  • ki_event side-channel now includes tool_result events.
  • HITL interrupts remain embedded in a content chunk + hitl_data fields.

HITL side-channel fields (on the choices entry):
  "hitl_required": true, "action_id": "...", "risk_level": "...", "human_summary": "..."
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.agent.workflow import run_session
from app.api.v1.auth import get_user_role
from app.db.audit import log_request as _audit_log
from app.streaming.emitter import PROTOCOL_VERSION, prepare_session
from app.streaming.emitter import stream as emitter_stream
from app.utils.logger import get_logger, request_id_var

logger = get_logger(__name__)

router = APIRouter()


# ── Request / Response models ─────────────────────────────────────────────────


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "kubeintellect"
    messages: list[ChatMessage]
    stream: bool = True
    user: str = "default"


# ── SSE chunk builders ────────────────────────────────────────────────────────


def _make_chunk(
    completion_id: str,
    content: str,
    *,
    finish_reason: str | None = None,
    hitl_data: dict | None = None,
) -> str:
    delta: dict = {"content": content, "role": "assistant"} if content else {}
    choice: dict = {
        "index": 0,
        "delta": delta,
        "finish_reason": finish_reason,
    }
    if hitl_data:
        choice.update(hitl_data)

    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "kubeintellect",
        "choices": [choice],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _make_ki_event_chunk(completion_id: str, ki_event: dict) -> str:
    """Emit a side-channel ki_event chunk (empty choices, ki_event at top level)."""
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "kubeintellect",
        "ki_event": ki_event,
        "choices": [],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _done_chunk() -> str:
    return "data: [DONE]\n\n"


# ── Typed event → SSE frame ───────────────────────────────────────────────────


def _serialise_event(completion_id: str, event: dict) -> str | None:
    """Convert a serialised typed Event dict to an SSE frame string."""
    event_type = event.get("type")

    if event_type == "status":
        return _make_ki_event_chunk(completion_id, {
            "type": "status",
            "phase": event["phase"],
            "message": event["message"],
        })

    if event_type == "tool_call":
        command = event.get("command")
        message = f"Running: {command}" if command else f"Calling {event['tool']}"
        return _make_ki_event_chunk(completion_id, {
            "type": "tool_call",
            "tool": event["tool"],
            "message": message,
        })

    if event_type == "tool_result":
        # New in Phase 2 — kube_q ignores unknown ki_event types
        return _make_ki_event_chunk(completion_id, {
            "type": "tool_result",
            "tool": event["tool"],
            "output": event["output"],
        })

    if event_type == "token":
        return _make_chunk(completion_id, event["content"])

    if event_type == "hitl_request":
        action_id = event.get("action_id", str(uuid.uuid4()))
        risk_level = event["risk_level"]
        command = event["command"]
        stdin_yaml = event.get("stdin_yaml")

        hitl_payload = {
            "hitl_required": True,
            "action_id": action_id,
            "risk_level": risk_level,
            "human_summary": command,
        }
        risk_emoji = "🔴" if risk_level == "high" else "🟡"
        message = (
            f"\n\n---\n"
            f"{risk_emoji} **Approval Required** — risk level: `{risk_level.upper()}`\n\n"
            f"**Command:**\n```\n{command}\n```\n"
        )
        if stdin_yaml:
            preview = stdin_yaml if len(stdin_yaml) <= 3_000 else stdin_yaml[:3_000] + "\n... [truncated]"
            message += f"\n**YAML to apply:**\n```yaml\n{preview}\n```\n"
        message += "\n**Type `yes` or `/approve` to proceed, or `no` / `/deny` to cancel.**"
        return _make_chunk(completion_id, message, hitl_data=hitl_payload)

    # FinalEvent (type == "final") is handled by the stream loop exit; skip it.
    return None


# ── Main endpoint ─────────────────────────────────────────────────────────────


@router.post("/chat/completions")
async def chat_completions(request: Request, body: ChatCompletionRequest):
    req_id = str(uuid.uuid4())
    request_id_var.set(req_id)

    user_messages = [m for m in body.messages if m.role == "user"]
    if not user_messages:
        raise HTTPException(status_code=422, detail="No user message provided")

    user_message = user_messages[-1].content
    # X-Session-ID ties this request to a LangGraph thread (enables HITL resume)
    session_id = request.headers.get("X-Session-ID", str(uuid.uuid4()))
    user_id = body.user or "default"
    user_role = get_user_role(request)

    logger.info(
        f"chat_completions: session={session_id} user={user_id} role={user_role} "
        f"msg={user_message[:80]!r}"
    )

    # Fire-and-forget audit record — never blocks the response
    asyncio.create_task(_audit_log(
        request_id=req_id,
        session_id=session_id,
        user_id=user_id,
        user_role=user_role,
        path=str(request.url.path),
        method=request.method,
        status_code=200,
        duration_ms=0.0,   # streaming; duration not meaningful here
    ))

    if not body.stream:
        raise HTTPException(status_code=422, detail="Only stream=true is supported")

    return StreamingResponse(
        _stream(user_message, session_id, user_id, user_role),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _stream(
    user_message: str,
    session_id: str,
    user_id: str,
    user_role: str,
) -> AsyncIterator[str]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    # Reset the session queue for this turn (keeps accumulated history).
    prepare_session(session_id)

    # ── Protocol handshake (first frame) ──────────────────────────────────────
    yield f"data: {json.dumps({'protocol_version': PROTOCOL_VERSION, 'object': 'stream.start', 'session_id': session_id})}\n\n"

    # ── Start workflow as background task ─────────────────────────────────────
    task = asyncio.create_task(
        run_session(user_message, session_id, user_id, user_role)
    )

    try:
        async for event_dict in emitter_stream(session_id, heartbeat_interval=15.0):
            if event_dict is None:
                # Queue was silent for 15 s — emit SSE keepalive comment.
                yield ": heartbeat\n\n"
                continue

            chunk = _serialise_event(completion_id, event_dict)
            if chunk:
                yield chunk

        # Normal completion
        yield _make_chunk(completion_id, "", finish_reason="stop")
        yield _done_chunk()

    except Exception as exc:
        logger.error(f"stream error session={session_id}: {exc}", exc_info=True)
        yield _make_chunk(completion_id, f"\n\n[Error: {exc}]", finish_reason="stop")
        yield _done_chunk()

    finally:
        # Cancel the background task on client disconnect or error; await to
        # let LangGraph clean up (checkpoint writes, etc.).
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
