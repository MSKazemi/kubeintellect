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


# `kubectl get pods` prints "Terminating" for a pod under deletion only while its phase is
# still non-terminal; a Succeeded/Failed pod keeps its own status even with a deletionTimestamp.
_TERMINAL_PHASES = ("Succeeded", "Failed")

# The reason the node controller writes on a pod whose node stopped answering. kubectl prints
# "Unknown" rather than "Terminating" for these, because nothing is actually terminating.
_NODE_UNREACHABLE = "NodeLost"


def _condition_is_true(status: dict, kind: str) -> bool:
    for cond in status.get("conditions") or []:
        if cond.get("type") == kind:
            return cond.get("status") == "True"
    return False


def pod_display_status(pod: dict) -> str:
    """Compute the STATUS column the way `kubectl get pods` prints it.

    This mirrors `printPod` in kubectl's `printers/internalversion`, in its order:

      1. the phase, overridden by `status.reason` (Evicted, NodeLost) — this is the *base*,
         which container statuses then override, not a fallback consulted last;
      2. the first init container that is not a clean exit — `Init:<waiting reason>`,
         `Init:<terminated reason>`, `Init:Signal:N`, `Init:ExitCode:N`, or `Init:i/n`;
      3. unless initialization is still in progress, every container status *in reverse*, so
         the first container wins: waiting reason, else terminated reason, else `Signal:N` /
         `ExitCode:N`;
      4. `Completed` alongside a running-and-ready container becomes `Running` or `NotReady`,
         by the pod's Ready condition;
      5. finally `Terminating` / `Unknown` for a pod under deletion.

    **Step 3's terminated branch was missing until 2026-08-25, and that absence was load-bearing.**
    Without it the function's range excluded every *terminated* reason — `OOMKilled`, `Error`,
    `Completed`, `ContainerStatusUnknown` — so a detector whose predicate was `^OOMKilled$` could
    not fire, ever, on any cluster. `nl:soak-oom`, authored from the prose "a container is killed
    for using too much memory (OOMKilled)", was stored, listed as `shadow`, and dead on arrival:
    the string it waits for was not in the vocabulary the observer can emit. `predicate_shape`
    could not catch it either, because `OOMKilled` is a perfectly legal, identifier-shaped
    Kubernetes reason — it just was not one of *ours*. The lesson is narrow and worth keeping:
    a predicate is only as live as the range of the function it is matched against, so that
    range has to be the real one.

    Step 4 is where `NotReady` comes from, and it is the only place it comes from. A detector
    that waits for `NotReady` meaning "the readiness probe is failing" is waiting for the wrong
    string: kubectl prints `Running` for that pod. `NotReady` appears only for a pod that has a
    completed container *and* a running, ready one, and is itself not Ready.
    """
    metadata = pod.get("metadata", {})
    status = pod.get("status", {})

    reason = status.get("phase", "Unknown")
    if status.get("reason"):
        reason = status["reason"]

    init_statuses = status.get("initContainerStatuses") or []
    initializing = False
    for i, cs in enumerate(init_statuses):
        state = cs.get("state", {})
        terminated = state.get("terminated")
        waiting = state.get("waiting")
        if terminated is not None and terminated.get("exitCode", 0) == 0:
            continue                                  # this one finished; look at the next
        if terminated is not None:
            if terminated.get("reason"):
                reason = "Init:" + terminated["reason"]
            elif terminated.get("signal"):
                reason = f"Init:Signal:{terminated['signal']}"
            else:
                reason = f"Init:ExitCode:{terminated.get('exitCode', 0)}"
        elif waiting and waiting.get("reason") and waiting["reason"] != "PodInitializing":
            reason = "Init:" + waiting["reason"]
        else:
            total = len(pod.get("spec", {}).get("initContainers") or init_statuses)
            reason = f"Init:{i}/{total}"
        initializing = True
        break

    if not initializing or _condition_is_true(status, "Initialized"):
        has_running = False
        # Reverse, and no early exit: kubectl lets the *first* container have the last word.
        for cs in reversed(status.get("containerStatuses") or []):
            state = cs.get("state", {})
            waiting = state.get("waiting")
            terminated = state.get("terminated")
            if waiting and waiting.get("reason"):
                reason = waiting["reason"]
            elif terminated and terminated.get("reason"):
                reason = terminated["reason"]
            elif terminated is not None:
                if terminated.get("signal"):
                    reason = f"Signal:{terminated['signal']}"
                else:
                    reason = f"ExitCode:{terminated.get('exitCode', 0)}"
            elif cs.get("ready") and state.get("running") is not None:
                has_running = True

        if reason == "Completed" and has_running:
            reason = "Running" if _condition_is_true(status, "Ready") else "NotReady"

    if metadata.get("deletionTimestamp"):
        if status.get("reason") == _NODE_UNREACHABLE:
            return "Unknown"
        if status.get("phase") not in _TERMINAL_PHASES:
            return "Terminating"

    return reason
