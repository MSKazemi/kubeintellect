"""Normalised observation records produced by the sensorium watchers."""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Observation:
    """One normalised cluster signal.

    kind:
      pod_status  — fields: {"status": "<computed STATUS column value>"}
      event       — fields: {"reason", "message", "involved_kind", "event_type"}
      node_status — fields: {"status": "Ready" | "NotReady" | ...}
      metric      — fields: {"promql": "<expr>", "labels": {...}, "value": float}
    """
    kind: str
    cluster_id: str
    namespace: str
    name: str
    fields: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


def pod_display_status(pod: dict) -> str:
    """Compute the STATUS column the way `kubectl get pods` prints it.

    Mirrors the precedence kubectl uses: deletion timestamp → Terminating;
    init-container waiting/terminated reasons → "Init:<reason>"; container
    waiting reason (CrashLoopBackOff, ImagePullBackOff, ...) ; status.reason
    (Evicted lands here on Failed pods); finally the bare phase.
    """
    metadata = pod.get("metadata", {})
    status = pod.get("status", {})

    if metadata.get("deletionTimestamp"):
        return "Terminating"

    for cs in status.get("initContainerStatuses") or []:
        state = cs.get("state", {})
        waiting = state.get("waiting")
        if waiting and waiting.get("reason"):
            return f"Init:{waiting['reason']}"
        terminated = state.get("terminated")
        if terminated and terminated.get("exitCode", 0) != 0:
            reason = terminated.get("reason") or f"ExitCode:{terminated['exitCode']}"
            return f"Init:{reason}"

    for cs in status.get("containerStatuses") or []:
        waiting = cs.get("state", {}).get("waiting")
        if waiting and waiting.get("reason"):
            return waiting["reason"]

    if status.get("reason"):  # e.g. Evicted
        return status["reason"]

    return status.get("phase", "Unknown")
