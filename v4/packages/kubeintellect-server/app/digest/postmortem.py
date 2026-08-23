"""Incident postmortem builder — a grounded narrative over the flight recorder (ADR-011).

Deterministic-first: the postmortem is a *view* over the hash-chained decision_log
(never a separate history). The structured timeline is the source of truth and
cites every event's `seq`; the optional LLM narrative only prettifies prose over
that timeline and is constrained to it. Fail-open like the digest — a recorder
outage degrades the report, never the request path.
"""
from __future__ import annotations

import json
import time
from datetime import datetime

from app.core.config import settings
from app.db import flight_recorder
from app.utils.logger import get_logger
from ki_protocol.record import summarise_record

logger = get_logger(__name__)

# Which event kinds count as each narrative bucket.
_MUTATION_KINDS = {"rollback_point", "hitl_request"}
_INVESTIGATE_KINDS = {"tool_call", "tool_result"}
_CONCLUSION_KINDS = {"final", "answer"}   # "final" is the recorded turn-end marker


def _payload(row: dict) -> dict:
    p = row.get("payload")
    if isinstance(p, str):
        try:
            return json.loads(p)
        except json.JSONDecodeError:
            return {}
    return p or {}


def _ts_of(row: dict) -> float:
    at = row.get("created_at")
    if isinstance(at, datetime):
        return at.timestamp()
    return float(at or 0.0)


def _summarize(kind: str, p: dict) -> str:
    """One line for one decision_log row.

    The implementation lives in `ki_protocol.record` because `kq replay` reads the same
    rows and used to summarise them from its own seven-field list — see that module.
    """
    return summarise_record(kind, p)


async def build_postmortem(episode_id: str) -> dict:
    """Reconstruct a grounded postmortem for one episode from the decision_log."""
    pm: dict = {
        "episode_id": episode_id,
        "generated_at": time.time(),
        "chain_valid": False,
        "timeline": [],
        "what_fired": [],
        "investigated": [],
        "tried": [],
        "worked": [],
        "errors": [],
        "root_cause": None,
        "follow_ups": [],
        "narrative": None,
        # A verified chain says nothing was altered. It does not say nothing is missing —
        # the recorder is fire-and-forget and records its own losses as GAP_KIND rows.
        "events_lost": 0,
        "gaps": [],
    }
    rows = await flight_recorder.fetch_episode(episode_id)
    if not rows:
        pm["summary"] = "No recorded events for this episode (or recorder unavailable)."
        return pm

    pm["chain_valid"] = flight_recorder.verify_chain(rows)
    for row in rows:
        kind = row["kind"]
        p = _payload(row)
        seq = row["seq"]
        summary = _summarize(kind, p)
        pm["timeline"].append({"seq": seq, "at": _ts_of(row), "kind": kind, "summary": summary})
        if kind == flight_recorder.GAP_KIND:
            lost = int(p.get("dropped") or 0)
            pm["events_lost"] += lost
            pm["gaps"].append({"seq": seq, "dropped": lost, "reason": p.get("reason", "")})
        if kind == "finding":
            pm["what_fired"].append({
                "seq": seq, "playbook": p.get("playbook", "?"),
                "namespace": p.get("namespace", ""), "object": p.get("object", ""),
                "severity": p.get("severity", "warning"),
            })
        elif kind in _INVESTIGATE_KINDS:
            pm["investigated"].append(f"[#{seq}] {summary}")
        elif kind in _MUTATION_KINDS:
            pm["tried"].append(f"[#{seq}] {summary}")
        elif kind == "error":
            pm["errors"].append(f"[#{seq}] {summary}")
        elif kind in _CONCLUSION_KINDS:
            pm["worked"].append(f"[#{seq}] {summary}")
            text = str(p.get("text") or p.get("answer") or p.get("final_text", "")).strip()
            if text:
                pm["root_cause"] = text[:300]

    # Root cause / outcome come from the L1 episode summary (the decision log
    # records *events*, not the final narrative). Best-effort, fail-open.
    meta = await _fetch_episode_meta(episode_id)
    if meta:
        if meta.get("root_cause"):
            pm["root_cause"] = str(meta["root_cause"])[:300]
        if meta.get("outcome"):
            verdict = "verified" if meta.get("verified") else meta["outcome"]
            pm["worked"].append(f"outcome: {meta['outcome']} ({verdict})")

    n = len(pm["timeline"])
    chain = "intact" if pm["chain_valid"] else "BROKEN"
    pm["summary"] = (
        f"{n} recorded events · {len(pm['what_fired'])} detector firing(s) · "
        f"{len(pm['investigated'])} investigation step(s) · {len(pm['tried'])} mutation "
        f"point(s) · {len(pm['errors'])} error(s) · audit chain {chain}."
    )
    pm["narrative"] = await synthesize_narrative(pm)
    return pm


async def _fetch_episode_meta(episode_id: str) -> dict | None:
    """Best-effort L1 episode lookup (summary/root_cause/outcome) by request_id."""
    pool = getattr(flight_recorder, "_pool", None)
    if pool is None:
        return None
    try:
        row = await pool.fetchrow(
            "SELECT summary, root_cause, outcome, verified FROM episodes"
            " WHERE request_id = $1 ORDER BY started_at DESC LIMIT 1",
            episode_id,
        )
    except Exception:
        return None
    return dict(row) if row else None


async def synthesize_narrative(pm: dict) -> str | None:
    """One optional LLM call that narrates the timeline. Grounded + fail-open.

    Constrained to the recorded events (passed as the deterministic markdown);
    must cite seq numbers and invent nothing. Returns None when disabled or on
    any failure, so the deterministic timeline always remains the fallback.
    """
    if not settings.POSTMORTEM_LLM_NARRATIVE:
        return None
    if not pm.get("timeline"):
        return None
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.cortex.models import get_synthesis_llm

        system = (
            "You are writing a Kubernetes incident postmortem. Use ONLY the recorded "
            "events below. Every claim MUST reference the event it came from using its "
            "[#seq] tag. Do not invent any fact not present in the timeline. If the audit "
            "chain is broken, say so explicitly. Be concise."
        )
        grounding = render_markdown({**pm, "narrative": None})
        llm = get_synthesis_llm()
        resp = await llm.ainvoke([SystemMessage(content=system), HumanMessage(content=grounding)])
        text = getattr(resp, "content", None)
        return text.strip() if isinstance(text, str) and text.strip() else None
    except Exception as exc:
        logger.warning(f"postmortem: narrative synthesis failed (using timeline only): {exc}")
        return None


def render_markdown(pm: dict) -> str:
    lines = [f"# Incident postmortem — `{pm['episode_id']}`", ""]
    if pm["chain_valid"]:
        lines.append("> ✅ Audit chain verified intact — every event below is tamper-evident.")
    else:
        lines.append("> ⚠️ **AUDIT CHAIN BROKEN** — the recorded events may have been altered.")
    if pm.get("events_lost"):
        # Intact and complete are different claims. Say the second one out loud, next to the
        # first, or the ✅ above reads as "this is the whole story" when it is not.
        reasons = ", ".join(sorted({g["reason"] for g in pm["gaps"] if g.get("reason")}))
        lines.append(
            f"> ⚠️ **RECORD INCOMPLETE** — {pm['events_lost']} event(s) were never written "
            f"({reasons or 'cause not recorded'}). Absence of an event below is not evidence "
            "it did not happen."
        )
    lines += ["", pm.get("summary", ""), ""]

    if not pm["timeline"]:
        return "\n".join(lines)

    if pm["root_cause"]:
        lines += ["## Root cause", pm["root_cause"], ""]

    lines += ["## Timeline"]
    for e in pm["timeline"]:
        at = time.strftime("%H:%M:%S", time.localtime(e["at"])) if e["at"] else "--:--:--"
        lines.append(f"- `[#{e['seq']}]` {at} **{e['kind']}** — {e['summary']}")
    lines.append("")

    if pm["what_fired"]:
        lines += ["## What fired"]
        for f in pm["what_fired"]:
            lines.append(
                f"- `[#{f['seq']}]` {f['playbook']} ({f['severity']}) "
                f"on {f['namespace']}/{f['object']}"
            )
        lines.append("")
    if pm["investigated"]:
        lines += ["## What was investigated", *(f"- {x}" for x in pm["investigated"]), ""]
    if pm["tried"]:
        lines += ["## What was tried (mutations)", *(f"- {x}" for x in pm["tried"]), ""]
    if pm.get("errors"):
        lines += ["## Errors encountered", *(f"- {x}" for x in pm["errors"]), ""]
    if pm["worked"]:
        lines += ["## Outcome", *(f"- {x}" for x in pm["worked"]), ""]
    if pm["follow_ups"]:
        lines += ["## Follow-ups", *(f"- {x}" for x in pm["follow_ups"]), ""]
    if pm.get("narrative"):
        lines += ["## Narrative", pm["narrative"], ""]
    return "\n".join(lines)
