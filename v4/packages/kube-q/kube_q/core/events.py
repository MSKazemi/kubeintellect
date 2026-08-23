"""
events.py — Typed event protocol for kube_q.

Since the V4 monorepo merge the protocol definition lives in the shared
``ki-protocol`` package (single source of truth for server and clients).
This module re-exports it so existing ``kube_q.core.events`` imports keep
working unchanged.
"""

from ki_protocol.events import (
    ErrorData,
    ErrorEvent,
    Event,
    FinalData,
    FinalEvent,
    HitlRequestData,
    HitlRequestEvent,
    PlanData,
    PlanEvent,
    StatusData,
    StatusEvent,
    TokenData,
    TokenEvent,
    ToolCallData,
    ToolCallEvent,
    ToolResultData,
    ToolResultEvent,
    UsageData,
    UsageEvent,
    parse_event,
)

__all__ = [
    "ErrorData",
    "ErrorEvent",
    "Event",
    "FinalData",
    "FinalEvent",
    "HitlRequestData",
    "HitlRequestEvent",
    "PlanData",
    "PlanEvent",
    "StatusData",
    "StatusEvent",
    "TokenData",
    "TokenEvent",
    "ToolCallData",
    "ToolCallEvent",
    "ToolResultData",
    "ToolResultEvent",
    "UsageData",
    "UsageEvent",
    "parse_event",
]
