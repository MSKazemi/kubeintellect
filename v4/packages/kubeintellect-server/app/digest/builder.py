"""Digest builder — "what did the organism do while you were away".

Deterministic (no LLM): the digest is a *view* over durable state — the
flight recorder (decision_log) and L1 episodes — never a separate history.
Sections: detector findings, autonomous investigations (watchtower
episodes), user sessions, rollback points armed.
"""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime

from app.utils.logger import get_logger

logger = get_logger(__name__)

_pool = None  # shares the memory service pool


def _get_pool():
    from app.memory import service

    return service._pool


async def build_digest(hours: float = 24.0) -> dict:
    """Structured digest for the last N hours. Empty sections when quiet.

    **"Quiet" and "unrecorded" are different answers and must not share a
    sentence.** The digest is the operator's morning check — *what did the agent
    do while I was away* — so an empty result is only reassuring if the sources
    were actually readable. Until 2026-08-20 four materially different states all
    produced the identical, confident line
    ``"Quiet watch: no findings in the last 24h."``: a genuinely quiet night; a
    failed ``decision_log`` query; SQLite mode, where the table does not exist at
    all; and ``FLIGHT_RECORDER_ENABLED=false``, where nothing is ever written.
    Only a missing pool was reported honestly.

    Those are all checks on the **record**. The record can be flawless and empty
    because nothing was ever looking, so the **perception** sources are checked
    too (:mod:`app.detectors.perception`) — the same classification
    ``GET /v1/findings`` returns, so the digest cannot call a window quiet that
    ``kq findings`` refuses to call clear.

    ``degraded_reasons`` names every source that could not answer. It is empty
    exactly when the digest is a real observation of the window.
    """
    from app.core.config import settings

    pool = _get_pool()
    cutoff = datetime.fromtimestamp(time.time() - hours * 3600, tz=UTC)
    digest: dict = {
        "window_hours": hours,
        "generated_at": time.time(),
        "findings": [],
        "auto_investigations": [],
        "user_sessions": 0,
        "rollback_points": [],
        "degraded": False,
        "degraded_reasons": [],
        "summary": "",
    }
    reasons: list[str] = digest["degraded_reasons"]

    # Configuration that makes an empty digest structurally guaranteed. Checked
    # before the queries, because these produce no error to catch.
    if not settings.FLIGHT_RECORDER_ENABLED:
        reasons.append(
            "the flight recorder is disabled (FLIGHT_RECORDER_ENABLED=false) — "
            "no findings, investigations or rollback points were recorded")
    elif settings.USE_SQLITE:
        reasons.append(
            "the server is in SQLite mode, which has no decision_log table — "
            "nothing was recorded to report on")
    if not settings.WATCHTOWER_ENABLED:
        reasons.append(
            "the watchtower is disabled (WATCHTOWER_ENABLED=false) — "
            "no autonomous investigation could have run")

    # The checks above are about the *record*. A perfectly healthy recorder holds
    # nothing when nothing was ever looking, so the sensing side is asked too —
    # through the same classifier `GET /v1/findings` uses, so the digest and
    # `kq findings` cannot give different answers about the same window.
    from app.detectors.perception import perception_gaps, perception_state

    try:
        reasons.extend(perception_gaps(perception_state()))
    except Exception as exc:  # perception must never break the digest
        logger.warning(f"digest: perception state unavailable: {exc}")
        reasons.append(f"the perception state could not be read ({type(exc).__name__}) — "
                       "whether anything was watching is unknown")

    if pool is None:
        reasons.append("the memory/recorder pool is unavailable")
        digest["degraded"] = True
        digest["summary"] = _one_liner(digest)
        return digest

    try:
        rows = await pool.fetch(
            """
            SELECT episode_id, kind, payload, created_at
            FROM decision_log
            WHERE created_at >= $1
            ORDER BY created_at
            """,
            cutoff,
        )
    except Exception as exc:
        logger.warning(f"digest: decision_log query failed: {exc}")
        reasons.append(f"the decision_log query failed ({type(exc).__name__}) — "
                       "findings, rollback points and session counts are unknown")
        rows = []

    sessions: set[str] = set()
    for row in rows:
        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        episode = row["episode_id"]
        kind = row["kind"]
        if kind == "finding":
            digest["findings"].append({
                "at": row["created_at"].timestamp(),
                "playbook": payload.get("playbook", "?"),
                "namespace": payload.get("namespace", ""),
                "object": payload.get("object", ""),
                "severity": payload.get("severity", "warning"),
                "eta_minutes": payload.get("eta_minutes"),
            })
        elif kind == "rollback_point":
            digest["rollback_points"].append({
                "at": row["created_at"].timestamp(),
                "rollback_id": payload.get("rollback_id", ""),
                "command": payload.get("command", ""),
                # A capture whose YAML was redacted or truncated is evidence, not a restore
                # point. Records written before this field existed cannot be judged, so they
                # are reported as unknown rather than promoted to "armed".
                "restorable": payload.get("restorable"),
                "capture_notes": payload.get("capture_notes") or [],
            })
        elif episode.startswith("auto-") or not episode.startswith("findings:"):
            sessions.add(episode)

    digest["user_sessions"] = len({s for s in sessions if not s.startswith("auto-")})

    try:
        episodes = await pool.fetch(
            """
            SELECT trigger_kind, trigger_detail, summary, outcome, verified,
                   namespace, playbooks, started_at
            FROM episodes
            WHERE started_at >= $1 AND trigger_kind <> 'backfill'
            ORDER BY started_at
            """,
            cutoff,
        )
    except Exception as exc:
        logger.warning(f"digest: episodes query failed: {exc}")
        reasons.append(f"the episodes query failed ({type(exc).__name__}) — "
                       "autonomous investigations are unknown")
        episodes = []

    for ep in episodes:
        detail = ep["trigger_detail"] or ""
        if "autonomous investigation" in detail or (ep["trigger_kind"] == "detector"):
            digest["auto_investigations"].append({
                "at": ep["started_at"].timestamp(),
                "namespace": ep["namespace"],
                "summary": (ep["summary"] or "")[:300],
                "outcome": ep["outcome"],
                "verified": ep["verified"],
                "playbooks": list(ep["playbooks"] or []),
            })

    digest["degraded"] = bool(reasons)
    digest["summary"] = _one_liner(digest)
    return digest


def _one_liner(digest: dict) -> str:
    findings = len(digest["findings"])
    autos = len(digest["auto_investigations"])
    fixes = sum(1 for a in digest["auto_investigations"] if a.get("outcome") == "resolved")
    if digest.get("degraded_reasons"):
        # Never "quiet" — an empty digest here is an absence of records, not of
        # events, and the two look identical from the outside.
        reasons = digest["degraded_reasons"]
        seen = (f"{findings} finding(s), {autos} investigation(s) readable"
                if (findings or autos) else "nothing readable")
        more = f" (+{len(reasons) - 1} more reason(s))" if len(reasons) > 1 else ""
        return (f"Digest INCOMPLETE for the last {digest['window_hours']:.0f}h — {seen}. "
                f"This is NOT a quiet watch: {reasons[0]}{more}.")
    if not findings and not autos:
        return f"Quiet watch: no findings in the last {digest['window_hours']:.0f}h."
    return (
        f"{findings} finding(s), {autos} autonomous investigation(s)"
        f"{f', {fixes} verified fix(es)' if fixes else ''}"
        f" in the last {digest['window_hours']:.0f}h."
    )


def render_markdown(digest: dict) -> str:
    lines = [f"# KubeIntellect digest — last {digest['window_hours']:.0f}h", ""]
    lines.append(digest["summary"])
    if digest.get("degraded_reasons"):
        lines += ["", "> **⚠️ This digest is incomplete — do not read it as an all-clear.**"]
        lines += [f"> - {r}" for r in digest["degraded_reasons"]]
    if digest["findings"]:
        lines += ["", "## Detector findings (zero-token)"]
        for f in digest["findings"][-20:]:
            at = time.strftime("%H:%M", time.localtime(f["at"]))
            tag = ""
            if f.get("severity") == "predicted":
                eta = f.get("eta_minutes")
                tag = f" _(predicted ~{eta:.0f}m)_" if eta is not None else " _(predicted)_"
            lines.append(f"- {at} **{f['playbook']}** {f['namespace']}/{f['object']}{tag}")
    if digest["auto_investigations"]:
        lines += ["", "## Autonomous investigations"]
        for a in digest["auto_investigations"][-10:]:
            at = time.strftime("%H:%M", time.localtime(a["at"]))
            status = a.get("outcome") or "report"
            lines.append(f"- {at} [{status}] ns={a['namespace']}: {a['summary'][:160]}")
    if digest["rollback_points"]:
        armed = sum(1 for r in digest["rollback_points"] if r.get("restorable") is True)
        lines += ["", f"## Pre-mutation state captures ({armed} of "
                      f"{len(digest['rollback_points'])} restorable)"]
        for r in digest["rollback_points"][-10:]:
            at = time.strftime("%H:%M", time.localtime(r["at"]))
            state = r.get("restorable")
            if state is True:
                mark = "restorable"
            elif state is None:
                mark = "⚠️ restorability unknown (captured before this was recorded)"
            else:
                notes = "; ".join(r.get("capture_notes") or []) or "redacted or truncated"
                mark = f"⚠️ NOT restorable — do not apply ({notes})"
            lines.append(f"- {at} `{r['rollback_id']}` [{mark}] — {r['command'][:80]}")
    if digest["user_sessions"]:
        lines += ["", f"_{digest['user_sessions']} user session(s) in the window._"]
    return "\n".join(lines)
