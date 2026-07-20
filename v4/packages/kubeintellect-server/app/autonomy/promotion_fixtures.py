"""Replay-fixture export for the ADR-102 promotion simulation (v5 specs/04).

Takes flight-recorder rows (mutation spans from specs/02 + their postcondition outcomes) and
produces a deterministic, redacted, per-action-class list of ``Event`` that the Wilson-LCB
evaluator (``promotion_stats``) replays offline. This is the substrate that lets the P3 trust
plane freeze the ADR-102 constants without touching a live cluster.

Pure functions — deterministic export, no wallclock, no LLM, no DB access here (rows are passed
in). Sensitive payload fields are dropped on export (only the labels the math needs survive).
"""

from __future__ import annotations

from typing import Any

from app.autonomy.promotion_stats import Event
from app.db import otel_spans

# Outcome kinds a postcondition check records against a mutation (specs/02 §5).
POSTCONDITION_KIND = "ki_postcondition"


def _mutation_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        r for r in rows
        if r.get("kind") == otel_spans.SPAN_KIND
        and (r.get("payload") or {}).get("gen_ai.operation.name") == otel_spans.OP_MUTATION
    ]


def _outcome_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map mutation span_id → its postcondition outcome payload."""
    idx: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("kind") != POSTCONDITION_KIND:
            continue
        p = r.get("payload") or {}
        sid = p.get("mutation_span_id")
        if sid:
            idx[sid] = p
    return idx


def export_events(rows: list[dict[str, Any]], *, day0: float = 0.0) -> dict[str, list[Event]]:
    """Export flight-recorder rows to per-action-class ``Event`` lists.

    ``rows`` are decoded decision_log rows: ``{kind, payload, episode_id, seq, created_ts?}``.
    Timestamps are normalized to monotonic day offsets from ``day0`` (deterministic; no wallclock).
    Only the fields the promotion math needs are kept — action, success, incident id/type,
    critical flag — so no sensitive telemetry leaves the recorder.
    """
    outcomes = _outcome_index(rows)
    by_class: dict[str, list[Event]] = {}
    muts = _mutation_rows(rows)
    # Deterministic ordering: by (episode_id, seq).
    muts.sort(key=lambda r: (r.get("episode_id", ""), r.get("seq", 0)))
    for i, r in enumerate(muts):
        p = r["payload"]
        attrs = p.get("attributes", {})
        action = attrs.get("ki.action", "unknown")
        span_id = p.get("span_id", f"{r.get('episode_id','')}:{r.get('seq', i)}")
        outcome = outcomes.get(span_id, {})
        # Success requires an affirmative postcondition; absent outcome ⇒ failure (conservative).
        success = bool(outcome.get("held", False))
        critical = bool(outcome.get("critical", False))
        # Deterministic day offset: use provided created_day, else the enumeration index.
        ts_days = float(r.get("created_day", i)) + day0
        ev = Event(
            ts_days=ts_days,
            success=success,
            incident_id=r.get("episode_id", f"ep-{i}"),
            incident_type=outcome.get("incident_type", "generic"),
            critical=critical,
        )
        by_class.setdefault(action, []).append(ev)
    return by_class


def redact_span_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return an export-safe copy of a span row: identity + the promotion-relevant
    attributes only; drops any free-text/telemetry payload fields."""
    p = row.get("payload") or {}
    attrs = p.get("attributes", {})
    keep_attrs = {
        k: attrs[k]
        for k in ("ki.action", "ki.provenance_incomplete")
        if k in attrs
    }
    return {
        "kind": row.get("kind"),
        "episode_id": row.get("episode_id"),
        "seq": row.get("seq"),
        "payload": {
            "span_id": p.get("span_id"),
            "gen_ai.operation.name": p.get("gen_ai.operation.name"),
            "attributes": keep_attrs,
        },
    }
