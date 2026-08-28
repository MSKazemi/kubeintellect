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
    # `tool` and `command` are what `ki_protocol.wire.ToolCallEvent` actually puts on the wire.
    # `tool` was already here, added by hand as a "legacy field emitted by some backends" — which
    # is what one half of a two-half contract looks like when nobody checks the other half.
    tool: str = ""
    command: str | None = None   # wire sends `None` for tools that are not run_kubectl
    message: str = ""


class ToolResultData(BaseModel):
    call_id: str = ""
    ok: bool = True
    summary: str = ""
    truncated: bool = False
    tool: str = ""          # wire.ToolResultEvent.tool


class HitlRequestData(BaseModel):
    action: str = ""
    risk: str = ""
    diff: str = ""
    approval_id: str = ""


class UsageData(BaseModel):
    """Counts for one request.

    `llm_calls` was added 2026-08-28: the server had been sending it since the meter existed and
    this model had no field for it, so it was dropped on arrival — and it is the count that keeps
    "called 40 times, reported no tokens" distinguishable from a cheap request.

    ⚠️ `model` is declared here and the server has **never** sent it, so it reads `""` for every
    caller. It is left in place rather than quietly filled: a request can span the coordinator and
    subagent tiers, so there is no single honest value to put in it, and inventing one would make
    this field lie rather than merely stay empty.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    llm_calls: int = 0
    model: str = ""


class FinalData(BaseModel):
    content: str = ""
    usage: UsageData | None = None
    elapsed_ms: int = 0


class ErrorData(BaseModel):
    code: str = ""
    message: str = ""
    retryable: bool = False


class PlanData(BaseModel):
    """The visible investigation plan (`INVESTIGATION_PLAN_ENABLED`, and every Cortex turn).

    `wire.PlanEvent` has existed since V2 and this union did not carry it, so `parse_event`
    returned `None` for every plan frame and the SDK dropped the feature silently.
    """
    steps: list[dict[str, Any]] = Field(default_factory=list)


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


class PlanEvent(_EventBase):
    type: Literal["plan"]
    data: PlanData = Field(default_factory=PlanData)


# ── Discriminated union ───────────────────────────────────────────────────────

Event = Annotated[
    StatusEvent
    | TokenEvent
    | ToolCallEvent
    | ToolResultEvent
    | HitlRequestEvent
    | UsageEvent
    | FinalEvent
    | ErrorEvent
    | PlanEvent,
    Field(discriminator="type"),
]

# Module-level adapter — built once at import time, not per parse_event() call.
_event_adapter: TypeAdapter[Event] = TypeAdapter(Event)


# ── Wire → client field names ─────────────────────────────────────────────────
#
# This module and `ki_protocol.wire` are the two halves of one contract, and its own docstring
# says "wire-format changes must update both modules together". That rule was kept by discipline
# and nothing else: no test in either suite imports both halves, so each was only ever checked
# against its own fixtures. Measured 2026-08-20 by serialising every server emission model and
# feeding it to `parse_event`, five of the eight arrived stripped:
#
#     server sent {"type": "tool_result", "tool": "run_kubectl", "output": "NAME READY…"}
#     client got  {"call_id": "", "ok": True, "summary": "", "truncated": False}
#
#     server sent {"type": "error", "error": "boom"}
#     client got  {"code": "", "message": "", "retryable": False}
#
# A dropped payload is worse than a rejected frame: `summary=""` and `message=""` are the shapes
# of *nothing went wrong*, so the SDK's caller sees an empty tool result rather than a parse
# failure it could report. The renames below are applied only where the client-side name is not
# already present, so a payload that already speaks this module's dialect is untouched.
_WIRE_ALIASES: dict[str, dict[str, str]] = {
    "tool_call":    {"tool": "tool_name"},
    "tool_result":  {"output": "summary"},
    "hitl_request": {"command": "action", "risk_level": "risk",
                     "action_id": "approval_id", "stdin_yaml": "diff"},
    "error":        {"error": "message"},
}


def _apply_wire_aliases(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """Copy `wire`-spelled fields onto their client-side names. Never overwrites."""
    aliases = _WIRE_ALIASES.get(event_type)
    if not aliases:
        return data
    out = dict(data)
    for wire_name, client_name in aliases.items():
        value = out.get(wire_name)
        if value is not None and not out.get(client_name):
            out[client_name] = value
    return out


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

    if isinstance(event_type, str) and isinstance(raw.get("data"), dict):
        raw = dict(raw)
        raw["data"] = _apply_wire_aliases(event_type, raw["data"])

    try:
        return _event_adapter.validate_python(raw)
    # `except (ValidationError, Exception)` was the same as `except Exception`:
    # ValidationError is a subclass, so listing it changed nothing and only made
    # the intent read as narrower than it is. The breadth is deliberate — this
    # decoder's contract is "return None for anything I cannot decode" — so it is
    # stated plainly rather than dressed up.
    except Exception:  # noqa: BLE001
        return None
