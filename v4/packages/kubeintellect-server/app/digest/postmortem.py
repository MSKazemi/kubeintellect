"""Incident postmortem builder — a grounded narrative over the flight recorder (ADR-011).

Deterministic-first: the postmortem is a *view* over the hash-chained decision_log
(never a separate history). The structured timeline is the source of truth and
cites every event's `seq`; the optional LLM narrative only prettifies prose over
that timeline and is constrained to it — and then checked claim by claim against it
(`apply_grounding_gate`): unsupported claims are removed, and a narrative below
`POSTMORTEM_MIN_GROUNDING` is withheld outright. Fail-open like the digest — a recorder
outage degrades the report, never the request path.
"""
from __future__ import annotations

import json
import re
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
        # Claim-level grounding of the LLM narrative (see `apply_grounding_gate`). None/0 means
        # no narrative was checked — NOT a perfect score; `grounding_rate` is only a number when
        # claims were actually counted. `narrative_withheld` is the reason the narrative was
        # dropped by the gate, so "the flag is off" and "the gate refused it" stay different.
        "grounding_rate": None,
        "claims_total": 0,
        "claims_ungrounded": 0,
        "narrative_withheld": None,
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
        narrative = await synthesize_narrative(pm)
    except _NarrativeFailed as exc:
        narrative = None
        pm["enrichment_failed"].append(f"narrative ({exc})")
    if narrative:
        try:
            apply_grounding_gate(pm, narrative)
        except Exception as exc:
            # Fail CLOSED for the narrative, open for the request: an unchecked narrative is
            # never attached, and the deterministic postmortem is still returned.
            logger.warning(f"postmortem: grounding check failed, narrative withheld: {exc}")
            pm["narrative"] = None
            pm["narrative_withheld"] = f"the grounding check could not run ({type(exc).__name__})"
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
        grounding = _evidence_text(pm)
        llm = get_synthesis_llm()
        resp = await llm.ainvoke([SystemMessage(content=system), HumanMessage(content=grounding)])
        text = getattr(resp, "content", None)
        return text.strip() if isinstance(text, str) and text.strip() else None
    except Exception as exc:
        logger.warning(f"postmortem: narrative synthesis failed (using timeline only): {exc}")
        raise _NarrativeFailed(str(exc)) from exc


# ── Claim-level grounding gate (ADR-011 × ADR-009) ──────────────────────────────────────────
# The system prompt above *asks* the model to invent nothing; a field campaign measured how far
# that holds: 0.70 and 0.61 of narrative claims supported by the incident record, with 36 and
# 54 unsupported claims across two runs, against a target of zero. A request is not a control.
# So every claim is checked, deterministically and without a second LLM call, against the very
# text the model was given — it cannot legitimately know anything else — and a claim that
# names something that text does not contain is never shown as fact.

_CITATION = re.compile(r"\[#(\d+)\]")
_BACKTICKED = re.compile(r"`([^`]+)`")
_TOKEN = re.compile(r"[A-Za-z0-9][\w.:/=%-]*")
_TIME = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
_NUMBER = re.compile(r"^\d+(?:\.\d+)?%?$")
_ABBREV = re.compile(r"^(?:[a-z]\.)+[a-z]?$", re.I)          # e.g / i.e — not identifiers
_CAMEL_OR_ACRONYM = re.compile(r"[a-z][A-Z]|^[A-Z]{2,}[a-z]*$")
_BULLET = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=\S)")
_CAUSAL = re.compile(
    r"\b(because|due to|caused|causes|causing|root cause|led to|leads to|lead to|resulted in|"
    r"results in|as a result|result of|therefore|thus|hence|triggered by|owing to|attributed to)\b",
    re.I,
)
# Lowercase hyphenated words are English ("follow-up", "read-only") as often as they are
# Kubernetes names ("payment-api"). Without a digit or a path separator they are only treated
# as a resource name when a resource kind sits next to them.
_KIND_WORDS = frozenset({
    "pod", "pods", "deployment", "deployments", "service", "svc", "namespace", "ns", "node",
    "nodes", "statefulset", "daemonset", "replicaset", "job", "cronjob", "configmap", "secret",
    "pvc", "pv", "ingress", "container", "hpa", "release", "chart", "image",
})
_STOPWORDS = frozenset({
    "this", "that", "these", "those", "with", "from", "into", "onto", "than", "then", "there",
    "their", "they", "them", "were", "was", "been", "being", "have", "has", "had", "which",
    "while", "when", "where", "what", "also", "only", "after", "before", "during", "about",
    "event", "events", "recorded", "record", "per", "the", "and", "its", "it's", "very",
    "most", "likely", "probably", "appears", "seems", "would", "could", "should", "some",
})


def _evidence_text(pm: dict) -> str:
    """Exactly what the narrative model is shown — and so the only thing it may assert."""
    return render_markdown({
        **pm, "narrative": None, "narrative_withheld": None,
        "claims_total": 0, "claims_ungrounded": 0,
    })


def _strip_md(text: str) -> str:
    return text.replace("**", "").replace("__", "").strip()


def _is_heading(line: str) -> bool:
    """Structure, not a claim: `## Timeline`, a ``` fence, or a short `**Summary:**` label."""
    s = _strip_md(line)
    if not s or s.startswith("#") or s.startswith("```"):
        return True
    return s.endswith(":") and len(s.split()) <= 4


def _present(token: str, evidence: str, *, numeric: bool) -> bool:
    """Whole-token, case-insensitive presence. A number may be followed by a unit, so `128`
    matches `128Mi`, but `5` does not match `256` and `123` does not match `abc123`; names are
    bounded by word characters and hyphens, so `web-1` matches `shop/web-1` but not `web-12`."""
    esc = re.escape(token)
    pattern = rf"(?<![\w.]){esc}(?!\.?\d)" if numeric else rf"(?<![\w-]){esc}(?![\w-])"
    return re.search(pattern, evidence, re.I) is not None


def _words(text: str) -> set[str]:
    """Content words, lightly normalised (plural `s` dropped) so `limits` meets `limit`."""
    return {
        w.rstrip("s") for w in re.findall(r"[a-z][a-z'-]+", text.lower())
        if len(w) >= 4 and w not in _STOPWORDS
    }


def _claim_is_grounded(
    claim: str, evidence: str, seqs: set[int], cited_text: dict[int, str], conclusion: str,
) -> bool:
    """One atomic claim against the evidence record. Every rule is a reason to REFUSE:

    1. it cites a `[#seq]` that is not in the timeline;
    2. a backticked span, resource-like name (digit, `/`, `.`, `:`, `_`, `=`, CamelCase or an
       acronym, or a hyphenated name beside a resource kind), number, or clock time in it does
       not appear in the evidence;
    3. it asserts causation (because / caused / led to / root cause / …) that is not carried
       by the recorded conclusion or by the events it cites — at least half its content words
       must come from that text;
    4. it has nothing to check at all — no valid citation and no named anchor. The prompt
       requires a citation on every claim; an unverifiable sentence is not a verified one.
    """
    cited = [int(m) for m in _CITATION.findall(claim)]
    if any(c not in seqs for c in cited):
        return False
    body = _CITATION.sub(" ", claim)
    anchors = 0
    for span in _BACKTICKED.findall(body):
        span = span.strip()
        if span:
            anchors += 1
            if span.lower() not in evidence.lower():
                return False
    body = _BACKTICKED.sub(" ", body)
    words = body.replace("**", " ").split()
    neighbours = [w.strip(".,;:!?()'\"").lower() for w in words]
    for i, raw in enumerate(words):
        for m in _TOKEN.finditer(raw):
            tok = m.group(0).rstrip(".:/=-_")
            if not tok or _ABBREV.match(tok):
                continue
            if _TIME.match(tok) or _NUMBER.match(tok):
                # Bare numbers are checked but are not anchors: "1" appears in nearly any record.
                if not _present(tok, evidence, numeric=True):
                    return False
                if _TIME.match(tok):
                    anchors += 1
                continue
            has_marker = any(ch.isdigit() or ch in "/.:_=" for ch in tok)
            is_named = has_marker or bool(_CAMEL_OR_ACRONYM.search(tok))
            if not is_named and "-" in tok:
                around = neighbours[max(0, i - 1):i] + neighbours[i + 1:i + 2]
                is_named = any(w in _KIND_WORDS for w in around)
            if is_named:
                anchors += 1
                if not _present(tok, evidence, numeric=False):
                    return False
    if _CAUSAL.search(body):
        support = " ".join([conclusion, *(cited_text.get(c, "") for c in cited)])
        content = _words(_CAUSAL.sub(" ", body))
        if not content or len(content & _words(support)) * 2 < len(content):
            return False
    return bool(cited) or anchors > 0


def ground_narrative(narrative: str, pm: dict) -> tuple[str, int, int]:
    """Split ``narrative`` into atomic claims and drop every one the record does not support.

    Splitting is rule-based: each non-heading line (a bullet's marker removed) is cut into
    sentences at `.`/`!`/`?` followed by whitespace; each sentence is one claim. Headings,
    fences and short `Label:` lines are structure and are neither counted nor checked.

    Returns ``(kept_text, claims_total, claims_ungrounded)``. A heading left with nothing under
    it is dropped too, so a removed section does not leave an empty title implying content.
    """
    evidence = _evidence_text(pm)
    seqs = {int(e["seq"]) for e in pm.get("timeline", [])}
    cited_text = {int(e["seq"]): str(e.get("summary", "")) for e in pm.get("timeline", [])}
    conclusion = " ".join([str(pm.get("root_cause") or ""), *map(str, pm.get("worked", []))])
    total = ungrounded = 0
    out: list[str] = []
    for line in narrative.splitlines():
        if _is_heading(line):
            out.append(line)
            continue
        m = _BULLET.match(line)
        prefix = m.group(1) if m else ""
        kept = []
        for sentence in _SENTENCE_END.split(line[len(prefix):].strip()):
            if not re.search(r"[A-Za-z0-9]", sentence):
                continue
            total += 1
            if _claim_is_grounded(sentence, evidence, seqs, cited_text, conclusion):
                kept.append(sentence)
            else:
                ungrounded += 1
        if kept:
            out.append(prefix + " ".join(kept))
    # Drop headings whose section lost every claim, then collapse blank runs.
    pruned: list[str] = []
    for i, line in enumerate(out):
        if line.strip() and _is_heading(line) and not _strip_md(line).startswith("```"):
            rest = [x for x in out[i + 1:] if x.strip()]
            if not rest or (_is_heading(rest[0]) and not _strip_md(rest[0]).startswith("```")):
                continue
        if not line.strip() and pruned and not pruned[-1].strip():
            continue
        pruned.append(line)
    return "\n".join(pruned).strip(), total, ungrounded


def apply_grounding_gate(pm: dict, narrative: str) -> None:
    """Attach ``narrative`` to ``pm`` only as far as the record supports it.

    Unsupported claims are removed, never shown marked-up: a reader skims an incident report,
    and an "(unverified)" suffix is a suffix people skip. The counts are recorded on ``pm`` so
    the rate is observable per postmortem. Below ``POSTMORTEM_MIN_GROUNDING`` the whole
    narrative is withheld — when a third of the prose was invented, the surviving two thirds
    were written by the same process — and the deterministic postmortem stands alone, with
    the reason in ``narrative_withheld``.
    """
    kept, total, ungrounded = ground_narrative(narrative, pm)
    pm["claims_total"] = total
    pm["claims_ungrounded"] = ungrounded
    pm["grounding_rate"] = (total - ungrounded) / total if total else None
    floor = settings.POSTMORTEM_MIN_GROUNDING
    reason = None
    if not total:
        reason = "the narrative contained no claims that could be checked against the record"
    elif pm["grounding_rate"] < floor:
        reason = (
            f"only {total - ungrounded} of {total} narrative claims are supported by the "
            f"recorded events (grounding rate {pm['grounding_rate']:.2f}, below "
            f"POSTMORTEM_MIN_GROUNDING={floor:.2f})"
        )
    elif not kept:
        reason = "no narrative claim survived the grounding check"
    if reason:
        # Counts only — the narrative text is model output and is not logged.
        logger.warning(f"postmortem {pm.get('episode_id')}: narrative withheld: {reason}")
        pm["narrative"] = None
        pm["narrative_withheld"] = reason
        return
    pm["narrative"] = kept


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
        if pm.get("claims_ungrounded"):
            lines += [
                f"_{pm['claims_ungrounded']} of {pm['claims_total']} narrative claim(s) were "
                "removed: the recorded events above do not support them._",
                "",
            ]
    elif pm.get("narrative_withheld"):
        # Said out loud: with the flag on, a silently absent narrative reads as "off".
        lines += [
            "## Narrative",
            f"> ⚠️ **LLM NARRATIVE WITHHELD** — {pm['narrative_withheld']}. The sections "
            "above are built from the recorded events alone and are unaffected.",
            "",
        ]
    return "\n".join(lines)
