"""OTel GenAI spans over the flight recorder (v5 specs/02, ADR-005 reuse).

Spans are emitted as additive ``decision_log`` rows (``kind="ki_otel_span"``) through the
existing ``record()`` path, so they enter the same per-episode SHA-256 hash chain and become
tamper-evident for free — no new store. Span identity + attributes live in the hash-covered
``payload``; the DB projection columns (trace_id/span_id/parent_span_id, added idempotently in
schema.sql) are for fast queries only and are excluded from the canonical hash form.

Pure builders + a deterministic, LLM-free provenance reconstructor keep this unit-testable
without a live DB or cluster.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

from app.core.config import settings
from app.db import flight_recorder

logger = logging.getLogger(__name__)

SPAN_KIND = "ki_otel_span"

# OTel GenAI operation names we emit (semconv-aligned) + KI extensions.
OP_CHAT = "chat"          # an LLM call
OP_TOOL = "execute_tool"  # a tool/ACI/kubectl call
OP_MCP = "mcp_call"       # an MCP server call
OP_MUTATION = "ki.mutation"  # a cluster-mutating action (KI extension)
OP_HYPOTHESIS = "ki.hypothesis"
OP_EVIDENCE = "ki.evidence"


def trace_id_for(episode_id: str) -> str:
    """Replay-stable trace id derived from the episode id (no wallclock/random)."""
    return hashlib.sha256(episode_id.encode()).hexdigest()[:32]


def span_id_for(episode_id: str, seq: int) -> str:
    """Deterministic span id from (episode, monotonic seq) — replay-stable."""
    return hashlib.sha256(f"{episode_id}:{seq}".encode()).hexdigest()[:16]


def _base(
    episode_id: str,
    seq: int,
    operation: str,
    parent_span_id: Optional[str],
    attributes: dict[str, Any],
) -> dict[str, Any]:
    return {
        "trace_id": trace_id_for(episode_id),
        "span_id": span_id_for(episode_id, seq),
        "parent_span_id": parent_span_id,
        "gen_ai.operation.name": operation,
        "attributes": attributes,
    }


def chat_span_payload(
    episode_id: str, seq: int, *, system: str, model: str,
    input_tokens: int, output_tokens: int, parent_span_id: Optional[str] = None,
) -> dict[str, Any]:
    """A GenAI ``chat`` span; its ``gen_ai.usage.*`` is the single spend source."""
    return _base(episode_id, seq, OP_CHAT, parent_span_id, {
        "gen_ai.system": system,
        "gen_ai.request.model": model,
        "gen_ai.usage.input_tokens": int(input_tokens),
        "gen_ai.usage.output_tokens": int(output_tokens),
    })


def tool_span_payload(
    episode_id: str, seq: int, *, tool_name: str, ok: bool,
    parent_span_id: Optional[str] = None, mcp: bool = False,
) -> dict[str, Any]:
    return _base(episode_id, seq, OP_MCP if mcp else OP_TOOL, parent_span_id, {
        "gen_ai.tool.name": tool_name,
        "ki.tool.ok": bool(ok),
    })


def mutation_span_payload(
    episode_id: str, seq: int, *, action: str,
    hypothesis_span_ids: list[str], evidence_span_ids: list[str],
    parent_span_id: Optional[str] = None,
) -> dict[str, Any]:
    """A cluster-mutating span. Provenance is enforced at build time: it MUST link
    ≥1 hypothesis and ≥1 evidence span, else ``ki.provenance_incomplete=true`` +
    a WARNING (fail-loud in the record, never dropped — R-otel-provenance)."""
    incomplete = not hypothesis_span_ids or not evidence_span_ids
    if incomplete:
        logger.warning(
            "otel: mutation span for %r has incomplete provenance "
            "(hypotheses=%d, evidence=%d)",
            action, len(hypothesis_span_ids), len(evidence_span_ids),
        )
    return _base(episode_id, seq, OP_MUTATION, parent_span_id, {
        "ki.action": action,
        "ki.links.hypothesis": list(hypothesis_span_ids),
        "ki.links.evidence": list(evidence_span_ids),
        "ki.provenance_incomplete": incomplete,
    })


def emit(episode_id: str, payload: dict[str, Any]) -> None:
    """Emit a span as a hash-chained flight-recorder row (fire-and-forget).

    No-op unless both CORTEX_V5_ENABLED and KI_V5_OTEL_SPANS_ENABLED are set,
    so with flags off the recorder byte-stream is identical to V4.
    """
    if not (settings.CORTEX_V5_ENABLED and settings.KI_V5_OTEL_SPANS_ENABLED):
        return
    flight_recorder.record(episode_id, SPAN_KIND, payload)


def build_provenance_chain(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic, LLM-free reconstruction of hypothesis→evidence→mutation.

    ``rows`` are decoded span payloads (from decision_log where kind=ki_otel_span),
    in any order. Returns, for each mutation span, the linked hypothesis + evidence
    spans, and whether the provenance is complete.
    """
    by_id = {r["span_id"]: r for r in rows if "span_id" in r}
    chains = []
    for r in rows:
        if r.get("gen_ai.operation.name") != OP_MUTATION:
            continue
        attrs = r.get("attributes", {})
        h_ids = attrs.get("ki.links.hypothesis", [])
        e_ids = attrs.get("ki.links.evidence", [])
        chains.append({
            "mutation_span_id": r["span_id"],
            "action": attrs.get("ki.action"),
            "hypotheses": [by_id[h] for h in h_ids if h in by_id],
            "evidence": [by_id[e] for e in e_ids if e in by_id],
            "complete": bool(h_ids) and bool(e_ids)
            and all(h in by_id for h in h_ids) and all(e in by_id for e in e_ids),
        })
    return {"chains": chains, "total_mutations": len(chains)}
