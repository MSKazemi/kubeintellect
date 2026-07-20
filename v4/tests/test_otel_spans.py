"""Unit tests for OTel GenAI spans over the flight recorder (v5 specs/02).

Pure builders + provenance reconstruction — no DB/cluster. The emit path is
verified by monkeypatching flight_recorder.record and toggling the flags.
"""

from __future__ import annotations

from app.core.config import settings
from app.db import otel_spans
from app.db.flight_recorder import compute_hash


# ── deterministic ids (replay-stable) ─────────────────────────────────────────
def test_trace_and_span_ids_are_deterministic():
    assert otel_spans.trace_id_for("ep-1") == otel_spans.trace_id_for("ep-1")
    assert otel_spans.trace_id_for("ep-1") != otel_spans.trace_id_for("ep-2")
    assert otel_spans.span_id_for("ep-1", 3) == otel_spans.span_id_for("ep-1", 3)
    assert otel_spans.span_id_for("ep-1", 3) != otel_spans.span_id_for("ep-1", 4)
    assert len(otel_spans.trace_id_for("ep-1")) == 32
    assert len(otel_spans.span_id_for("ep-1", 3)) == 16


# ── chat span carries gen_ai.usage (single spend source) ──────────────────────
def test_chat_span_usage_attributes():
    p = otel_spans.chat_span_payload("ep-1", 0, system="anthropic", model="m", input_tokens=10, output_tokens=5)
    a = p["attributes"]
    assert a["gen_ai.usage.input_tokens"] == 10
    assert a["gen_ai.usage.output_tokens"] == 5
    assert p["gen_ai.operation.name"] == otel_spans.OP_CHAT


def test_tool_and_mcp_span_operation_names():
    t = otel_spans.tool_span_payload("ep-1", 1, tool_name="inspect", ok=True)
    m = otel_spans.tool_span_payload("ep-1", 2, tool_name="pagerduty", ok=True, mcp=True)
    assert t["gen_ai.operation.name"] == otel_spans.OP_TOOL
    assert m["gen_ai.operation.name"] == otel_spans.OP_MCP


# ── provenance enforced at build time (R-otel provenance) ─────────────────────
def test_mutation_with_full_provenance_is_complete():
    p = otel_spans.mutation_span_payload(
        "ep-1", 3, action="scale", hypothesis_span_ids=["h1"], evidence_span_ids=["e1"],
    )
    assert p["attributes"]["ki.provenance_incomplete"] is False


def test_mutation_without_evidence_is_flagged_not_dropped(caplog):
    p = otel_spans.mutation_span_payload(
        "ep-1", 3, action="scale", hypothesis_span_ids=["h1"], evidence_span_ids=[],
    )
    assert p["attributes"]["ki.provenance_incomplete"] is True  # fail-loud, still recorded


# ── deterministic provenance chain reconstruction ─────────────────────────────
def test_build_provenance_chain_links_hypothesis_and_evidence():
    h = otel_spans.tool_span_payload("ep-1", 0, tool_name="hypo", ok=True)
    h["gen_ai.operation.name"] = otel_spans.OP_HYPOTHESIS
    e = otel_spans.tool_span_payload("ep-1", 1, tool_name="ev", ok=True)
    e["gen_ai.operation.name"] = otel_spans.OP_EVIDENCE
    mut = otel_spans.mutation_span_payload(
        "ep-1", 2, action="restart",
        hypothesis_span_ids=[h["span_id"]], evidence_span_ids=[e["span_id"]],
    )
    # rows given out of order → reconstruction is order-independent
    out = otel_spans.build_provenance_chain([mut, e, h])
    assert out["total_mutations"] == 1
    chain = out["chains"][0]
    assert chain["action"] == "restart"
    assert chain["complete"] is True
    assert len(chain["hypotheses"]) == 1 and len(chain["evidence"]) == 1


def test_provenance_chain_incomplete_when_link_missing():
    mut = otel_spans.mutation_span_payload(
        "ep-1", 2, action="restart", hypothesis_span_ids=["missing"], evidence_span_ids=["gone"],
    )
    out = otel_spans.build_provenance_chain([mut])
    assert out["chains"][0]["complete"] is False


# ── emit is flag-gated (byte-identical to V4 when off) ────────────────────────
def test_emit_noop_when_flags_off(monkeypatch):
    calls = []
    monkeypatch.setattr(otel_spans.flight_recorder, "record", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(settings, "CORTEX_V5_ENABLED", False)
    monkeypatch.setattr(settings, "KI_V5_OTEL_SPANS_ENABLED", True)
    otel_spans.emit("ep-1", otel_spans.chat_span_payload("ep-1", 0, system="x", model="m", input_tokens=1, output_tokens=1))
    assert calls == []


def test_emit_records_span_when_flags_on(monkeypatch):
    calls = []
    monkeypatch.setattr(otel_spans.flight_recorder, "record", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(settings, "CORTEX_V5_ENABLED", True)
    monkeypatch.setattr(settings, "KI_V5_OTEL_SPANS_ENABLED", True)
    otel_spans.emit("ep-1", otel_spans.chat_span_payload("ep-1", 0, system="x", model="m", input_tokens=1, output_tokens=1))
    assert len(calls) == 1
    episode_id, kind, _payload = calls[0]
    assert (episode_id, kind) == ("ep-1", otel_spans.SPAN_KIND)


# ── span identity is hash-covered (payload is part of the canonical form) ─────
def test_span_payload_participates_in_hash_chain():
    payload = otel_spans.chat_span_payload("ep-1", 0, system="x", model="m", input_tokens=1, output_tokens=1)
    h1 = compute_hash("", "ep-1", 0, otel_spans.SPAN_KIND, payload)
    tampered = {**payload, "attributes": {**payload["attributes"], "gen_ai.usage.input_tokens": 999}}
    h2 = compute_hash("", "ep-1", 0, otel_spans.SPAN_KIND, tampered)
    assert h1 != h2  # tampering the span changes the chain hash
