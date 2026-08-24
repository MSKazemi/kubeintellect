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
        # Whether the chain was CHECKED at all. `chain_valid: false` on its own cannot tell
        # "the hashes disagree" from "there was nothing to hash" — and the markdown banner read
        # it as the first, so an unreadable recorder and an episode with no events both printed
        # "the recorded events may have been altered", which is a false statement about records
        # nobody read. Same reason `kq replay` has a separate exit 4 for unverified.
        "chain_verified": False,
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
        "recorder_available": True,
        # Best-effort enrichments that ERRORED. Both of them return None on failure and None is
        # also their legitimate "there was nothing to add", so the rendered document was
        # byte-identical whether the episode store had no row or refused to answer — measured
        # 2026-08-24, under a ✅ "audit chain verified intact" banner. A missing section is a
        # claim about the incident; a failed lookup is a claim about us.
        "enrichment_failed": [],
    }
    try:
        rows = await flight_recorder.fetch_episode(episode_id)
    except flight_recorder.RecorderUnavailable as exc:
        # Kept as a returned postmortem rather than an error: the caller renders it, and a
        # postmortem that says why it is empty is more use than a stack trace. But it must not
        # share a sentence with the genuinely-empty case — "or recorder unavailable" made every
        # reader guess which of the two they had.
        pm["recorder_available"] = False
        pm["summary"] = (
            f"The flight recorder could not be read ({exc}). This is NOT the same as the "
            f"episode having no events — nothing here should be read as an absence of activity."
        )
        return pm
    # Verify BEFORE the empty-episode early return. An episode with an anchor but no surviving
    # rows is not an empty episode, it is a *total* truncation — the most complete tamper there
    # is — and the early return described it as "nothing was recorded here".
    chain_verdict = await flight_recorder.verify_episode(episode_id, rows)
    if not rows:
        if not chain_verdict.verified:
            # The anchor could not be read, so "no events" and "every event removed" are
            # indistinguishable from here. Reporting the first would be picking one.
            pm["summary"] = (
                "No events survive for this episode, and the recorder's chain anchor could "
                "not be read — so this is NOT a statement that nothing was recorded. An "
                "episode whose records were all removed looks exactly like this one."
            )
            return pm
        if chain_verdict.valid:
            # Genuinely empty. `chain_verified` stays False on purpose: nothing was read, so
            # this is neither a statement that records are intact nor that they were altered.
            # Setting it True here would print the ✅ banner over an episode with no events —
            # the precise dilution the three-state banner above exists to prevent.
            pm["summary"] = "No recorded events for this episode."
            return pm
        pm["chain_verified"] = True
        pm["chain_valid"] = False
        pm["summary"] = (
            "No events survive for this episode, but the recorder's chain anchor says there "
            "were some. Every event has been removed — this is NOT an episode in which "
            "nothing happened."
        )
        return pm

    pm["chain_valid"] = chain_verdict.valid
    # Not an unconditional True. Records were read, but the anchor read can fail on its own,
    # and `chain_verified` is what suppresses the ✅ banner further down.
    pm["chain_verified"] = chain_verdict.verified
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
    try:
        meta = await _fetch_episode_meta(episode_id)
    except _EpisodeLookupFailed as exc:
        meta = None
        pm["enrichment_failed"].append(f"root cause / outcome (episode store: {exc})")
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
    try:
        pm["narrative"] = await synthesize_narrative(pm)
    except _NarrativeFailed as exc:
        pm["narrative"] = None
        pm["enrichment_failed"].append(f"narrative ({exc})")
    return pm


class _NarrativeFailed(RuntimeError):
    """The narrative call errored. Distinct from the feature being off or the timeline empty."""


class _EpisodeLookupFailed(RuntimeError):
    """The episode store could not be read — as distinct from holding no row for this episode.

    The first means the postmortem is missing a section it tried to fill; the second means the
    investigation genuinely never reached a conclusion. Returning None for both made those the
    same document.
    """


async def _fetch_episode_meta(episode_id: str) -> dict | None:
    """L1 episode lookup (summary/root_cause/outcome) by request_id.

    None means no pool or no matching row — both of which are real answers. A query that failed
    raises `_EpisodeLookupFailed` instead, so the caller can say the section is missing rather
    than let its absence read as "there was no root cause".
    """
    pool = getattr(flight_recorder, "_pool", None)
    if pool is None:
        return None
    try:
        row = await pool.fetchrow(
            "SELECT summary, root_cause, outcome, verified FROM episodes"
            " WHERE request_id = $1 ORDER BY started_at DESC LIMIT 1",
            episode_id,
        )
    except Exception as exc:
        logger.warning(f"postmortem: episode lookup failed for {episode_id}: {exc}")
        raise _EpisodeLookupFailed(str(exc)) from exc
    return dict(row) if row else None


async def synthesize_narrative(pm: dict) -> str | None:
    """One optional LLM call that narrates the timeline. Grounded + fail-open.

    Constrained to the recorded events (passed as the deterministic markdown); must cite seq
    numbers and invent nothing. The deterministic timeline is always the fallback, so a failure
    here never costs the reader a fact.

    None means the feature is off or there is nothing to narrate. A failure raises
    `_NarrativeFailed` — an operator who switched `POSTMORTEM_LLM_NARRATIVE` on and gets no
    narrative should not have to guess whether the flag took effect.
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
        raise _NarrativeFailed(str(exc)) from exc


def render_markdown(pm: dict) -> str:
    lines = [f"# Incident postmortem — `{pm['episode_id']}`", ""]
    # Three states, not two. A tamper warning is only worth printing if it is never printed
    # when nothing was tampered with — a banner that also fires for an empty episode and for an
    # unreadable recorder trains the reader to skip the one that matters.
    if not pm.get("chain_verified", True):
        lines.append(
            "> ⚠️ **AUDIT CHAIN NOT VERIFIED** — no records were read, so this is neither a "
            "statement that they are intact nor that they were altered. See the reason below."
        )
    elif pm["chain_valid"]:
        lines.append("> ✅ Audit chain verified intact — every event below is tamper-evident.")
    else:
        lines.append(
            "> ⚠️ **AUDIT CHAIN BROKEN** — the recorded events may have been altered or "
            "truncated. See the server log for which."
        )
    if pm.get("events_lost"):
        # Intact and complete are different claims. Say the second one out loud, next to the
        # first, or the ✅ above reads as "this is the whole story" when it is not.
        reasons = ", ".join(sorted({g["reason"] for g in pm["gaps"] if g.get("reason")}))
        lines.append(
            f"> ⚠️ **RECORD INCOMPLETE** — {pm['events_lost']} event(s) were never written "
            f"({reasons or 'cause not recorded'}). Absence of an event below is not evidence "
            "it did not happen."
        )
    if pm.get("enrichment_failed"):
        # Deliberately next to the chain banners: those describe the RECORDS, this describes the
        # DOCUMENT. A ✅ above and a silently missing "Root cause" below is the combination this
        # line exists to break up.
        lines.append(
            "> ⚠️ **POSTMORTEM INCOMPLETE** — could not read: "
            + "; ".join(pm["enrichment_failed"])
            + ". A section missing below is NOT evidence that it was empty."
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
