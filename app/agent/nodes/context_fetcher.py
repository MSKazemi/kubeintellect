"""context_fetcher node — pre-fetches pod + event snapshot before the coordinator runs."""
from __future__ import annotations

import asyncio
import os
import subprocess

from app.agent.state import AgentState
from app.core.config import settings
from app.streaming.emitter import StatusEvent, emit
from app.utils.logger import get_logger

logger = get_logger(__name__)

_SNAPSHOT_MAX_CHARS = 8_000


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
    logger.debug(
        f"context_fetcher: snapshot built chars={len(cluster_snapshot)} session={session_id}"
    )
    return {"cluster_snapshot": cluster_snapshot}
