"""
Typed event protocol for KubeIntellect V2 SSE streaming.

Architecture
────────────
LangGraph nodes call emit() directly for status events.
workflow.run_session() translates raw LangGraph astream_events into typed Events
and emits them here.  The FastAPI endpoint reads from stream() (an async generator)
and serialises each event as an SSE data frame.

Events are also appended to an in-memory history list so that
GET /v1/events/replay/{session_id} can replay them for debugging.

Per-session lifecycle
─────────────────────
  prepare_session(sid)   – called by the endpoint before starting a new turn;
                           resets the queue while keeping accumulated history.
  emit(sid, event)       – pushes a serialised event onto the queue + history.
  close_session(sid)     – puts the _DONE sentinel so stream() exits cleanly.
  stream(sid)            – async generator consumed by the SSE endpoint;
                           yields None on 15 s heartbeat timeout.
  get_history(sid)       – returns all events recorded for the session.
"""
from __future__ import annotations

import asyncio
import time
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

PROTOCOL_VERSION = "1.0"

# Sentinel – module-level singleton; identity compared with ``is``.
_DONE: object = object()

# ── Typed event models ────────────────────────────────────────────────────────


class StatusEvent(BaseModel):
    type: Literal["status"] = "status"
    phase: str       # loading | analyzing | investigating | dispatching | synthesizing
    message: str
    session_id: str
    ts: float = Field(default_factory=time.time)


class ToolCallEvent(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    tool: str
    command: str | None = None   # populated for run_kubectl
    session_id: str
    ts: float = Field(default_factory=time.time)


class ToolResultEvent(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool: str
    output: str      # first 500 chars of tool output
    session_id: str
    ts: float = Field(default_factory=time.time)


class TokenEvent(BaseModel):
    type: Literal["token"] = "token"
    content: str
    session_id: str
    ts: float = Field(default_factory=time.time)


class FinalEvent(BaseModel):
    type: Literal["final"] = "final"
    session_id: str
    ts: float = Field(default_factory=time.time)


class HitlRequestEvent(BaseModel):
    type: Literal["hitl_request"] = "hitl_request"
    action_id: str = Field(default_factory=lambda: str(uuid4()))
    risk_level: str
    command: str
    stdin_yaml: str | None = None
    session_id: str
    ts: float = Field(default_factory=time.time)


class PlanEvent(BaseModel):
    """Emitted by the coordinator when an investigation plan is produced."""
    type: Literal["plan"] = "plan"
    steps: list[dict]   # list of {description, status}
    session_id: str
    ts: float = Field(default_factory=time.time)


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    error: str
    session_id: str
    ts: float = Field(default_factory=time.time)


Event = (
    StatusEvent
    | ToolCallEvent
    | ToolResultEvent
    | TokenEvent
    | FinalEvent
    | HitlRequestEvent
    | PlanEvent
    | ErrorEvent
)

# ── Per-session registry ──────────────────────────────────────────────────────

# Keyed by session_id.  Both dicts are mutated only from async context so no
# additional locking is needed (asyncio is single-threaded within an event loop).
_queues: dict[str, asyncio.Queue] = {}
_histories: dict[str, list[dict]] = {}


def prepare_session(session_id: str) -> None:
    """
    Create (or reset) the queue for a new turn.

    Must be called by the FastAPI endpoint **before** starting the background
    workflow task and before iterating stream().  Preserves any existing history
    so that multi-turn sessions accumulate a full event log for replay.
    """
    _queues[session_id] = asyncio.Queue()
    if session_id not in _histories:
        _histories[session_id] = []


def _ensure(session_id: str) -> asyncio.Queue:
    """Return the queue for *session_id*, creating one if it doesn't exist."""
    if session_id not in _queues:
        prepare_session(session_id)
    return _queues[session_id]


async def emit(session_id: str, event: Event) -> None:
    """Push a typed event onto the session queue and append it to history."""
    q = _ensure(session_id)
    serialised = event.model_dump()
    _histories[session_id].append(serialised)
    await q.put(serialised)


async def close_session(session_id: str) -> None:
    """
    Signal that no more events will arrive for this session.

    Records a FinalEvent in history and puts the _DONE sentinel so that
    stream() exits cleanly.
    """
    q = _ensure(session_id)
    final = FinalEvent(session_id=session_id)
    _histories[session_id].append(final.model_dump())
    await q.put(_DONE)


def get_history(session_id: str) -> list[dict]:
    """Return all events recorded for *session_id* (for replay)."""
    return list(_histories.get(session_id, []))


async def stream(session_id: str, heartbeat_interval: float = 15.0):
    """
    Async generator that yields serialised event dicts for *session_id*.

    Yields ``None`` after *heartbeat_interval* seconds of queue silence so that
    the caller can emit an SSE keepalive comment (``": heartbeat\\n\\n"``).
    Exits when close_session() is called.
    """
    q = _ensure(session_id)
    while True:
        try:
            item = await asyncio.wait_for(q.get(), timeout=heartbeat_interval)
        except asyncio.TimeoutError:
            yield None   # caller emits ": heartbeat\n\n"
            continue
        if item is _DONE:
            break
        yield item
