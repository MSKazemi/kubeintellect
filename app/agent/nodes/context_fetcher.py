"""context_fetcher node — pre-fetches pod + event snapshot before the coordinator runs."""
from __future__ import annotations

import asyncio
import os
import subprocess
import time

from app.agent.state import AgentState
from app.core.config import settings
from app.streaming.emitter import StatusEvent, emit
from app.utils.logger import get_logger

logger = get_logger(__name__)

_SNAPSHOT_MAX_CHARS = 8_000

# Pod phases that count as "healthy" — anything else flips snapshot_has_issues.
# (Note: STATUS column from `kubectl get pods` mixes phases and reasons —
#  e.g. "CrashLoopBackOff", "ImagePullBackOff". We treat any value not in this
#  set as an issue, which is what we want.)
_HEALTHY_POD_STATUSES = frozenset({"Running", "Completed", "Succeeded"})


def _scan_snapshot(pods_out: str, events_out: str) -> tuple[bool, bool, int]:
    """Return (has_issues, has_warnings, pod_count) by scanning kubectl output.

    Cheap line-based parse — no extra subprocess calls. The pod table format is:
        NAMESPACE   NAME   READY   STATUS   RESTARTS   AGE
    We index the STATUS column by header position to be robust to column widths.
    """
    has_issues = False
    pod_count = 0

    lines = pods_out.splitlines()
    status_idx: int | None = None
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        cols = line.split()
        if status_idx is None:
            # Header row — find STATUS column index. Fallback to 3 (default layout).
            try:
                status_idx = [c.upper() for c in cols].index("STATUS")
            except ValueError:
                status_idx = 3
            continue
        if len(cols) <= status_idx:
            continue
        pod_count += 1
        status = cols[status_idx]
        if status not in _HEALTHY_POD_STATUSES:
            has_issues = True

    has_warnings = bool(events_out.strip()) and "No resources found" not in events_out
    return has_issues, has_warnings, pod_count


def _run_kubectl_snapshot(args: list[str]) -> str:
    kubeconfig = os.path.expanduser(settings.KUBECONFIG_PATH)
    env = {**os.environ, "KUBECONFIG": kubeconfig}
    try:
        proc = subprocess.run(
            ["kubectl"] + args,
            capture_output=True,
            text=True,
            timeout=settings.KUBECTL_TIMEOUT_SECONDS,
            env=env,
            shell=False,
        )
        out = proc.stdout or proc.stderr or ""
        return out[:_SNAPSHOT_MAX_CHARS]
    except Exception as exc:
        logger.warning(f"context_fetcher: kubectl {' '.join(args[:2])} failed: {exc}")
        return f"(unavailable: {exc})"


async def context_fetcher(state: AgentState) -> dict:
    """Pre-fetch pod list and warning events in parallel before coordinator runs."""
    session_id = state.get("session_id", "-")
    await emit(session_id, StatusEvent(
        phase="snapshot",
        message="Fetching cluster snapshot…",
        session_id=session_id,
    ))

    pods_out, events_out = await asyncio.gather(
        asyncio.to_thread(_run_kubectl_snapshot, ["get", "pods", "--all-namespaces"]),
        asyncio.to_thread(_run_kubectl_snapshot, [
            "get", "events", "--all-namespaces",
            "--sort-by=.lastTimestamp",
            "--field-selector=type=Warning",
        ]),
    )

    parts = ["## Cluster Snapshot"]
    parts.append(f"### Live Pod State\n```\n{pods_out.strip()}\n```")

    no_events = not events_out.strip() or "No resources found" in events_out
    if no_events:
        parts.append("### Warning Events\n(none — cluster appears healthy)")
    else:
        parts.append(f"### Warning Events (most recent)\n```\n{events_out.strip()}\n```")

    cluster_snapshot = "\n\n".join(parts)

    # ── Snapshot health scan ──────────────────────────────────────────────────
    has_issues, has_warnings, pod_count = _scan_snapshot(pods_out, events_out)

    # ── Playbook trigger matching ─────────────────────────────────────────────
    matched_playbooks: list[str] = []
    if settings.PLAYBOOKS_ENABLED:
        try:
            from app.agent.playbooks import match_playbooks
            matched_playbooks = match_playbooks(pods_out, events_out)
        except Exception as exc:
            logger.warning(f"context_fetcher: playbook matching failed: {exc}")

    logger.debug(
        f"context_fetcher: snapshot built chars={len(cluster_snapshot)} "
        f"pods={pod_count} issues={has_issues} warnings={has_warnings} "
        f"playbooks={matched_playbooks} session={session_id}"
    )
    return {
        "cluster_snapshot": cluster_snapshot,
        "snapshot_has_issues": has_issues,
        "snapshot_has_warnings": has_warnings,
        "snapshot_pod_count": pod_count,
        "snapshot_built_at": time.time(),
        "matched_playbooks": matched_playbooks,
    }
