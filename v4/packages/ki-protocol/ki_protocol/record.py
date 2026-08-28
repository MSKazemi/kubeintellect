"""One human summary per flight-recorder row, for every reader of the decision_log.

The recorder (ADR-005) is one table with more than one reader: the server builds the
incident postmortem over it, and `kq replay` streams it back to a terminal. Both have to
turn `(kind, payload)` into a line of text, and until 2026-08-20 both did — separately.
The server's version knew all eleven recorded kinds; the client's was a tuple of seven
top-level field names, so every row whose content lives anywhere else rendered as an
**empty summary cell**:

* `finding` — `Finding.to_dict()` shares no key with that tuple, and a `findings:<cluster>`
  episode contains *nothing but* findings. `kq replay findings:default` printed N rows of
  blank. The detector fired; the tool that exists to read the record back said nothing did.
* `plan` — the investigation plan is entirely in `steps`.
* `ki_otel_span` — entirely in `attributes`.

A blank cell is not "no summary available", it reads as "this event carried nothing" —
which is the one thing a tamper-evident log must never say falsely. Hence one function,
imported by both readers, so a new `kind` is handled once or not at all.
"""
from __future__ import annotations

from typing import Any

# The recorder's own kinds, mirrored here because this is the module both sides import.
# `app.db.flight_recorder.GAP_KIND` and `app.db.otel_spans.SPAN_KIND` are the definitions;
# `tests` assert they agree with these.
GAP_KIND = "recorder_gap"
SPAN_KIND = "ki_otel_span"

# Kinds that legitimately carry no content of their own. `final` is a turn-end marker and
# `token` is never recorded at all. Anything else must produce a non-empty summary — the
# test suite enumerates the wire models and fails when a new one is not handled here.
CARRIES_NO_CONTENT = frozenset({"token"})


def summarise_record(kind: str, payload: dict[str, Any]) -> str:
    """Describe one decision_log row in a single line. Never returns an empty string."""
    p = payload if isinstance(payload, dict) else {}
    if kind == "finding":
        sev = p.get("severity", "warning")
        eta = p.get("eta_minutes")
        tag = f" (predicted ~{eta:.0f}m)" if sev == "predicted" and eta is not None else ""
        return (
            f"detector {p.get('playbook', '?')} fired on "
            f"{p.get('namespace', '')}/{p.get('object', '')}{tag}"
        )
    if kind == "tool_call":
        return f"tool {p.get('tool', '?')}: {str(p.get('command', '')).strip()[:100]}"
    if kind == "tool_result":
        return f"result: {str(p.get('summary') or p.get('output', '')).strip()[:100]}"
    if kind == "rollback_point":
        state = p.get("restorable")
        mark = "" if state is True else (
            " ⚠️ NOT restorable" if state is False else " (restorability not recorded)")
        return (f"rollback point {p.get('rollback_id', '')}{mark} before: "
                f"{str(p.get('command', ''))[:80]}")
    if kind == "hitl_request":
        return f"approval requested: {str(p.get('command') or p.get('message', ''))[:80]}"
    if kind in ("answer", "final"):
        text = str(p.get("text") or p.get("answer") or p.get("final_text", "")).strip()
        return text[:200] if text else "investigation concluded"
    if kind == GAP_KIND:
        return (
            f"⚠️ {p.get('dropped', '?')} event(s) LOST here — {p.get('reason', 'unknown cause')}. "
            "The record below this point is incomplete."
        )
    if kind == "error":
        return f"error: {str(p.get('error', '')).strip()[:160]}"
    if kind == "usage":
        # Calls are named alongside tokens on purpose: `core/usage.py` keeps `llm_calls` so that
        # "called 40 times, reported no tokens" — an instrumentation gap — stays legible next to
        # a genuinely cheap request, and a summary showing only tokens erases that distinction
        # again for every reader of the record.
        return (
            f"{p.get('total_tokens', 0)} token(s) over {p.get('llm_calls', 0)} LLM call(s) "
            f"({p.get('prompt_tokens', 0)} in / {p.get('completion_tokens', 0)} out)"
        )
    if kind == "status":
        return str(p.get("message") or p.get("status") or "status update")[:120]
    if kind in ("plan", "plan_transition"):
        steps = p.get("steps")
        if isinstance(steps, list) and steps:
            done = sum(1 for s in steps if isinstance(s, dict) and s.get("status") == "done")
            return f"plan — {len(steps)} step(s), {done} done"
        return f"plan: {str(p.get('summary') or p.get('step', ''))[:100]}"
    if kind == SPAN_KIND:
        # A v5 OTel span: the operation name plus how many attributes came with it.
        attrs = p.get("attributes")
        count = len(attrs) if isinstance(attrs, dict) else 0
        return (f"span {p.get('gen_ai.operation.name', '?')} "
                f"({count} attribute(s), span_id={str(p.get('span_id', '?'))[:16]})")
    return kind or "record"
