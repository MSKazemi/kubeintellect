"""context_fetcher node — pre-fetches pod + event snapshot before the coordinator runs."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import subprocess
import time

from app.agent.state import AgentState
from app.core.config import settings
from app.streaming.emitter import StatusEvent, emit
from app.utils.logger import get_logger

logger = get_logger(__name__)

_SNAPSHOT_MAX_CHARS = 8_000

# Notes appended to the snapshot when a listing was cut short or never
# arrived. Mirrors the truncation marker in tools/kubectl_tool.py: the LLM
# must be told the list it is looking at is incomplete, never left to infer
# an all-clear from partial data.
_SNAPSHOT_TRUNCATED_NOTE = (
    "[snapshot truncated: output exceeded 8 000 chars and was cut short — "
    "this listing is incomplete and may hide unhealthy pods or warning "
    "events. Narrow the scope with -n <namespace> or -l <label>.]"
)
_SNAPSHOT_UNAVAILABLE_NOTE = (
    "[snapshot unavailable: kubectl could not fetch this listing — "
    "treat the cluster state as unknown, not healthy.]"
)


@dataclass(frozen=True)
class SnapshotOutput:
    """One capped kubectl snapshot listing, plus flags that say whether an
    all-clear derived from it can be trusted.

    ``truncated``   — the output hit the 8 000-char cap, so rows beyond the
        cut are unknown.
    ``unavailable`` — kubectl could not be run at all (missing binary,
        timeout, bad kubeconfig), so nothing was fetched.
    """

    text: str
    truncated: bool = False
    unavailable: bool = False

    def with_note(self) -> str:
        """The capped text with a visible note appended when it is incomplete."""
        if self.unavailable:
            return f"{self.text}\n\n{_SNAPSHOT_UNAVAILABLE_NOTE}"
        if self.truncated:
            return f"{self.text}\n\n{_SNAPSHOT_TRUNCATED_NOTE}"
        return self.text


# Pod phases that count as "healthy" — anything else flips snapshot_has_issues.
# (Note: STATUS column from `kubectl get pods` mixes phases and reasons —
#  e.g. "CrashLoopBackOff", "ImagePullBackOff". We treat any value not in this
#  set as an issue, which is what we want.)
_HEALTHY_POD_STATUSES = frozenset({"Running", "Completed", "Succeeded"})


def _scan_snapshot(
    pods_out: str,
    events_out: str,
    *,
    pods_truncated: bool = False,
    pods_unavailable: bool = False,
    events_truncated: bool = False,
    events_unavailable: bool = False,
) -> tuple[bool, bool, int]:
    """Return (has_issues, has_warnings, pod_count) by scanning kubectl output.

    Cheap line-based parse — no extra subprocess calls. The pod table format is:
        NAMESPACE   NAME   READY   STATUS   RESTARTS   AGE
    We index the STATUS column by header position to be robust to column widths.

    The ``*_truncated`` / ``*_unavailable`` flags are conservative guards: a
    listing that was cut short (or never fetched) must not be reported as
    clean, because an unhealthy pod or warning event may sit beyond the cap.
    Callers derive them from SnapshotOutput.
    """
    has_issues = pods_truncated or pods_unavailable
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

    has_warnings = events_truncated or events_unavailable
    if events_out.strip() and "No resources found" not in events_out:
        has_warnings = True
    return has_issues, has_warnings, pod_count


def _run_kubectl_snapshot(args: list[str]) -> SnapshotOutput:
    """Run a kubectl snapshot query, capping output at _SNAPSHOT_MAX_CHARS.

    Returns a SnapshotOutput carrying the capped text plus ``truncated`` /
    ``unavailable`` flags so callers never mistake a partial or failed
    listing for an all-clear signal.
    """
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
        return SnapshotOutput(
            text=out[:_SNAPSHOT_MAX_CHARS],
            truncated=len(out) > _SNAPSHOT_MAX_CHARS,
        )
    except Exception as exc:
        logger.warning(f"context_fetcher: kubectl {' '.join(args[:2])} failed: {exc}")
        return SnapshotOutput(text=f"(unavailable: {exc})", unavailable=True)


async def context_fetcher(state: AgentState) -> dict:
    """Pre-fetch pod list and warning events in parallel before coordinator runs."""
    session_id = state.get("session_id", "-")
    await emit(session_id, StatusEvent(
        phase="snapshot",
        message="Fetching cluster snapshot…",
        session_id=session_id,
    ))

    pods, events = await asyncio.gather(
        asyncio.to_thread(_run_kubectl_snapshot, ["get", "pods", "--all-namespaces"]),
        asyncio.to_thread(_run_kubectl_snapshot, [
            "get", "events", "--all-namespaces",
            "--sort-by=.lastTimestamp",
            "--field-selector=type=Warning",
        ]),
    )

    parts = ["## Cluster Snapshot"]
    pod_block = pods.with_note().strip() or "(no pods found)"
    parts.append(f"### Live Pod State\n```\n{pod_block}\n```")

    no_events = not events.text.strip() or "No resources found" in events.text
    if events.unavailable:
        parts.append(f"### Warning Events\n{events.with_note().strip()}")
    elif no_events:
        parts.append("### Warning Events\n(none — cluster appears healthy)")
    else:
        parts.append(f"### Warning Events (most recent)\n```\n{events.with_note().strip()}\n```")

    cluster_snapshot = "\n\n".join(parts)

    # ── Snapshot health scan ──────────────────────────────────────────────────
    has_issues, has_warnings, pod_count = _scan_snapshot(
        pods.text,
        events.text,
        pods_truncated=pods.truncated,
        pods_unavailable=pods.unavailable,
        events_truncated=events.truncated,
        events_unavailable=events.unavailable,
    )

    # ── Playbook trigger matching ─────────────────────────────────────────────
    matched_playbooks: list[str] = []
    if settings.PLAYBOOKS_ENABLED:
        try:
            from app.agent.playbooks import match_playbooks
            matched_playbooks = match_playbooks(pods.text, events.text)
        except Exception as exc:
            logger.warning(f"context_fetcher: playbook matching failed: {exc}")

    # Resolve cluster identity (cached for the process lifetime).
    try:
        from app.cluster_id import get_cluster_id
        cluster_id = get_cluster_id()
    except Exception as exc:
        logger.warning(f"context_fetcher: cluster_id resolution failed: {exc}")
        cluster_id = "unknown"

    logger.info(
        "context_fetcher: snapshot_complete",
        extra={
            "session_id": session_id,
            "snapshot_chars": len(cluster_snapshot),
            "snapshot_pod_count": pod_count,
            "snapshot_has_issues": has_issues,
            "snapshot_has_warnings": has_warnings,
            "snapshot_truncated": pods.truncated or events.truncated,
            "snapshot_unavailable": pods.unavailable or events.unavailable,
            "matched_playbooks": matched_playbooks,
            "cluster_id": cluster_id,
        },
    )

    return {
        "cluster_snapshot": cluster_snapshot,
        "snapshot_has_issues": has_issues,
        "snapshot_has_warnings": has_warnings,
        "snapshot_pod_count": pod_count,
        "snapshot_built_at": time.time(),
        "matched_playbooks": matched_playbooks,
        "cluster_id": cluster_id,
    }