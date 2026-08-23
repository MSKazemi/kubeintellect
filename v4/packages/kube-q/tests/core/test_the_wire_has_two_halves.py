"""`ki_protocol.wire` and `ki_protocol.events` are two halves of one contract. Nothing joined them.

`events.py`'s own docstring says *"Wire-format changes must update both modules together"* — a rule
kept by discipline and by nothing else. Before this file, **no test in either suite imported both
halves**: `packages/kube-q/tests/core/test_events.py` exercises `parse_event` against dicts written
in the client's own dialect, and the server suite exercises the emitters against theirs. Each half
proved itself consistent with itself.

Measured 2026-08-20 by serialising every server emission model and feeding it to `parse_event`::

    server sent {"type": "tool_result", "tool": "run_kubectl", "output": "NAME READY…"}
    client got  {"call_id": "", "ok": True, "summary": "", "truncated": False}

    server sent {"type": "error", "error": "boom"}
    client got  {"code": "", "message": "", "retryable": False}

    server sent {"type": "plan",  "steps": [...]}       ← emitted since V2
    client got  None                                     ← discarded entirely

A dropped payload is worse than a rejected frame: `summary=""` is the shape of a tool that returned
nothing, and `message=""` the shape of an error with no detail, so the SDK's caller cannot tell a
lost payload from a real absence.

The tests below are **generative over `ki_protocol.wire`**: a new emission model added without a
client counterpart fails here, which is what "must update both modules together" was always meant
to mean.
"""
from __future__ import annotations

import inspect
import json

import pytest
from ki_protocol import events, wire
from ki_protocol.events import parse_event

# One representative payload per server emission model. A wire model with no sample here fails
# `test_every_wire_model_has_a_sample` — that is the reminder to add it, not an excuse to skip it.
SAMPLES: dict[str, dict] = {
    "status":       dict(phase="snapshot", message="Fetching cluster snapshot…", session_id="s1"),
    "tool_call":    dict(tool="run_kubectl", command="kubectl get pods -A", session_id="s1"),
    "tool_result":  dict(tool="run_kubectl", output="NAME   READY\nweb    1/1", session_id="s1"),
    "token":        dict(content="hello", session_id="s1"),
    "final":        dict(session_id="s1"),
    "hitl_request": dict(command="kubectl delete pod x", risk_level="high", session_id="s1"),
    "plan":         dict(steps=[{"description": "check pods", "status": "pending"}], session_id="s1"),
    "error":        dict(error="boom", session_id="s1"),
}


def _wire_models() -> dict[str, type]:
    """Every emission model in `ki_protocol.wire`, keyed by the `type` it puts on the wire."""
    out = {}
    for _, cls in inspect.getmembers(wire, inspect.isclass):
        if getattr(cls, "__module__", "") != wire.__name__:
            continue
        field = getattr(cls, "model_fields", {}).get("type")
        if field is not None and isinstance(field.default, str):
            out[field.default] = cls
    return out


WIRE = _wire_models()
WIRE_TYPES = sorted(WIRE)

# `wire.FinalEvent` carries no content by design — it is an end-of-stream marker, and the answer
# arrives as `token` frames. Exempted explicitly rather than skipped silently.
CARRIES_NO_PAYLOAD = {"final"}


def _on_the_wire(event_type: str) -> dict:
    """Exactly what the server puts on the queue: `model_dump()`, JSON-round-tripped."""
    payload = WIRE[event_type](**SAMPLES[event_type]).model_dump()
    return json.loads(json.dumps(payload, default=str))


# ── the seam itself ───────────────────────────────────────────────────────────
class TestEveryEmittedEventIsParseable:
    def test_the_wire_is_not_empty(self):
        assert len(WIRE) >= 8, WIRE_TYPES

    @pytest.mark.parametrize("event_type", WIRE_TYPES)
    def test_every_wire_model_has_a_sample(self, event_type):
        assert event_type in SAMPLES, f"{event_type} is emitted but this file never exercises it"

    @pytest.mark.parametrize("event_type", WIRE_TYPES)
    def test_the_client_parses_it(self, event_type):
        assert parse_event(_on_the_wire(event_type)) is not None, (
            f"the server emits {event_type!r} and parse_event discards it")

    @pytest.mark.parametrize("event_type", WIRE_TYPES)
    def test_the_discriminator_survives(self, event_type):
        assert parse_event(_on_the_wire(event_type)).type == event_type

    @pytest.mark.parametrize("event_type", WIRE_TYPES)
    def test_the_session_id_survives(self, event_type):
        assert parse_event(_on_the_wire(event_type)).session_id == "s1"


# ── the payload, not just the envelope ────────────────────────────────────────
class TestThePayloadArrives:
    """`final` is exempt: `wire.FinalEvent` carries no content by design — it is an end-of-stream
    marker, and the answer arrives as `token` frames. That is stated, not silently skipped."""

    @pytest.mark.parametrize("event_type", [t for t in WIRE_TYPES if t not in CARRIES_NO_PAYLOAD])
    def test_no_field_the_server_sent_is_dropped(self, event_type):
        sent = _on_the_wire(event_type)
        got = parse_event(sent).data.model_dump()
        interesting = {k: v for k, v in sent.items()
                       if k not in ("type", "ts", "session_id") and v not in (None, "", [], {})}
        assert interesting, f"{event_type}: sample carries nothing to check"
        missing = [k for k, v in interesting.items() if v not in got.values()]
        assert not missing, f"{event_type}: {missing} never reached the client — sent {sent}, got {got}"

    def test_a_tool_result_carries_its_output(self):
        got = parse_event(_on_the_wire("tool_result")).data
        assert got.summary.startswith("NAME")
        assert got.tool == "run_kubectl"

    def test_an_error_carries_its_message(self):
        assert parse_event(_on_the_wire("error")).data.message == "boom"

    def test_a_hitl_request_carries_the_command_and_the_risk(self):
        got = parse_event(_on_the_wire("hitl_request")).data
        assert got.action == "kubectl delete pod x"
        assert got.risk == "high"
        assert got.approval_id

    def test_a_tool_call_carries_the_command(self):
        got = parse_event(_on_the_wire("tool_call")).data
        assert got.command == "kubectl get pods -A"
        assert got.tool_name == "run_kubectl"

    def test_a_plan_carries_its_steps(self):
        got = parse_event(_on_the_wire("plan")).data
        assert got.steps == [{"description": "check pods", "status": "pending"}]

    def test_a_tool_call_for_a_non_kubectl_tool_still_parses(self):
        # `wire.ToolCallEvent.command` is `str | None`; a client field typed `str` would reject it.
        raw = wire.ToolCallEvent(tool="query_loki", session_id="s1").model_dump()
        parsed = parse_event(json.loads(json.dumps(raw, default=str)))
        assert parsed is not None and parsed.data.tool_name == "query_loki"


# ── the client dialect still wins ─────────────────────────────────────────────
class TestTheAliasesNeverOverwrite:
    def test_a_client_shaped_payload_is_untouched(self):
        got = parse_event({"type": "tool_result",
                           "data": {"summary": "already client-shaped", "output": "wire-shaped"}})
        assert got.data.summary == "already client-shaped"

    def test_an_alias_only_fills_an_empty_field(self):
        got = parse_event({"type": "error", "data": {"message": "kept", "error": "ignored"}})
        assert got.data.message == "kept"

    def test_an_event_with_no_aliases_is_unchanged(self):
        got = parse_event({"type": "status", "data": {"phase": "p", "message": "m"}})
        assert (got.data.phase, got.data.message) == ("p", "m")

    def test_an_unknown_type_is_still_none(self):
        assert parse_event({"type": "totally_unknown", "data": {}}) is None

    def test_a_non_dict_data_does_not_raise(self):
        assert parse_event({"type": "status", "data": "not a dict"}) is None


# ── the union is reachable from the client package ────────────────────────────
class TestPlanIsAFirstClassClientEvent:
    def test_plan_event_is_exported(self):
        assert hasattr(events, "PlanEvent") and hasattr(events, "PlanData")

    def test_plan_event_reaches_the_kq_re_export(self):
        # `kube_q.core.events` exists so the client's imports keep working; a union member the
        # SDK cannot import is a member the SDK does not have.
        from kube_q.core import events as kq_events
        assert kq_events.PlanEvent is events.PlanEvent
        assert "PlanEvent" in kq_events.__all__

    def test_plan_is_in_the_union(self):
        parsed = parse_event(_on_the_wire("plan"))
        assert isinstance(parsed, events.PlanEvent)

    def test_an_empty_plan_is_valid(self):
        assert parse_event({"type": "plan", "data": {}}).data.steps == []
