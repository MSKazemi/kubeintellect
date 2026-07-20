"""Digest builder — "what did the organism do while you were away".

Deterministic (no LLM): the digest is a *view* over durable state — the
flight recorder (decision_log) and L1 episodes — never a separate history.
Sections: detector findings, autonomous investigations (watchtower
episodes), user sessions, rollback points armed.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from app.utils.logger import get_logger

logger = get_logger(__name__)

_pool = None  # shares the memory service pool


def _get_pool():
    from app.memory import service

    return service._pool


async def build_digest(hours: float = 24.0) -> dict:
    """Structured digest for the last N hours. Empty sections when quiet."""
    pool = _get_pool()
    cutoff = datetime.fromtimestamp(time.time() - hours * 3600, tz=timezone.utc)
    digest: dict = {
        "window_hours": hours,
        "generated_at": time.time(),
        "findings": [],
        "auto_investigations": [],
        "user_sessions": 0,
        "rollback_points": [],
        "summary": "",
    }
    if pool is None:
        digest["summary"] = "Memory/recorder unavailable — no digest data."
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
            })
        elif episode.startswith("auto-"):
            sessions.add(episode)
        elif not episode.startswith("findings:"):
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

    digest["summary"] = _one_liner(digest)
    return digest


def _one_liner(digest: dict) -> str:
    findings = len(digest["findings"])
    autos = len(digest["auto_investigations"])
    fixes = sum(1 for a in digest["auto_investigations"] if a.get("outcome") == "resolved")
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
        lines += ["", "## Rollback points armed"]
        for r in digest["rollback_points"][-10:]:
            at = time.strftime("%H:%M", time.localtime(r["at"]))
            lines.append(f"- {at} `{r['rollback_id']}` — {r['command'][:80]}")
    if digest["user_sessions"]:
        lines += ["", f"_{digest['user_sessions']} user session(s) in the window._"]
    return "\n".join(lines)
