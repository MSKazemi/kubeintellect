"""
Server emission models — the canonical flat wire shape (protocol 1.0).

These models define exactly what the KubeIntellect server serialises onto the
SSE `ki_event` side-channel:

    {"type": "status", "phase": "...", "message": "...", "session_id": "...", "ts": ...}

Moved verbatim from `app/streaming/emitter.py` in the V4 monorepo merge
(ADR-004) so server and clients share one protocol definition. The client-side
typed view (envelope + `data`, with normalisation of this flat shape) lives in
`ki_protocol.events`. Wire-format changes must update both modules together.
"""
from __future__ import annotations

import time
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

PROTOCOL_VERSION = "1.0"


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
