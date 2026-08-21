"""
events.py — Typed client view of the KubeIntellect wire protocol.

Moved here from `kube_q/core/events.py` in the V4 monorepo merge (ADR-004);
the server emission models (the flat wire shape this module normalises) live
in `ki_protocol.wire`. Wire-format changes must update both modules together.

All backend → client messages use the envelope defined here.
The discriminated union ``Event`` covers every event type emitted by
KubeIntellect.  New types must be added by extending the union — never
overload ``data`` shapes between types.

Wire format (SSE):
    data: {"type": "status", "event_id": "...", "session_id": "...", ...}

Example usage:
    event = parse_event(raw_dict)
    match event:
        case StatusEvent(data=d): print(d.message)
        case TokenEvent(data=d):  buffer += d.content
        ...
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

# ── Per-type data payloads ────────────────────────────────────────────────────

class StatusData(BaseModel):
    phase: str = ""
    message: str = ""


class TokenData(BaseModel):
    content: str
    role: str = "assistant"


class ToolCallData(BaseModel):
    tool_name: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    call_id: str = ""
    dry_run: bool = False
    # Legacy field emitted by some backends
    tool: str = ""
    message: str = ""


class ToolResultData(BaseModel):
    call_id: str = ""
    ok: bool = True
    summary: str = ""
    truncated: bool = False


class HitlRequestData(BaseModel):
    action: str = ""
    risk: str = ""
    diff: str = ""
    approval_id: str = ""


class UsageData(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""


class FinalData(BaseModel):
    content: str = ""
    usage: UsageData | None = None
    elapsed_ms: int = 0


class ErrorData(BaseModel):
    code: str = ""
    message: str = ""
    retryable: bool = False


# ── Envelope base ─────────────────────────────────────────────────────────────

class _EventBase(BaseModel):
    event_id: str = ""
    session_id: str = ""
    conversation_id: str = ""
    timestamp: str = ""


# ── Typed event models ────────────────────────────────────────────────────────

class StatusEvent(_EventBase):
    type: Literal["status"]
    data: StatusData = Field(default_factory=StatusData)


class TokenEvent(_EventBase):
    type: Literal["token"]
    data: TokenData


class ToolCallEvent(_EventBase):
    type: Literal["tool_call"]
    data: ToolCallData = Field(default_factory=ToolCallData)


class ToolResultEvent(_EventBase):
    type: Literal["tool_result"]
    data: ToolResultData = Field(default_factory=ToolResultData)


class HitlRequestEvent(_EventBase):
    type: Literal["hitl_request"]
    data: HitlRequestData = Field(default_factory=HitlRequestData)


class UsageEvent(_EventBase):
    type: Literal["usage"]
    data: UsageData = Field(default_factory=UsageData)


class FinalEvent(_EventBase):
    type: Literal["final"]
    data: FinalData = Field(default_factory=FinalData)


class ErrorEvent(_EventBase):
    type: Literal["error"]
    data: ErrorData = Field(default_factory=ErrorData)


# ── Discriminated union ───────────────────────────────────────────────────────

Event = Annotated[
    StatusEvent
    | TokenEvent
    | ToolCallEvent
    | ToolResultEvent
    | HitlRequestEvent
    | UsageEvent
    | FinalEvent
    | ErrorEvent,
    Field(discriminator="type"),
]

# Module-level adapter — built once at import time, not per parse_event() call.
_event_adapter: TypeAdapter[Event] = TypeAdapter(Event)


# ── Parser ────────────────────────────────────────────────────────────────────

def parse_event(raw: dict[str, Any]) -> Event | None:
    """Parse a raw SSE data dict into a typed Event.

    Also handles the legacy KubeIntellect ``ki_event`` side-channel format
    where data fields are at the top level of the event dict rather than
    nested under a ``data`` key.

    Returns None for unknown/malformed events.
    """

    # Normalise legacy ki_event format: type is at top level, data fields also
    # at top level (no nested "data" key).
    event_type = raw.get("type")
    if event_type and "data" not in raw:
        raw = dict(raw)
        raw["data"] = {k: v for k, v in raw.items() if k != "type"}

    try:
        return _event_adapter.validate_python(raw)
    # `except (ValidationError, Exception)` was the same as `except Exception`:
    # ValidationError is a subclass, so listing it changed nothing and only made
    # the intent read as narrower than it is. The breadth is deliberate — this
    # decoder's contract is "return None for anything I cannot decode" — so it is
    # stated plainly rather than dressed up.
    except Exception:  # noqa: BLE001
        return None
