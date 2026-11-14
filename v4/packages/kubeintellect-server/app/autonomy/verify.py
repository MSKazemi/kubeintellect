"""Decide, without the model, whether an autonomous fix fixed the object the finding was about.

Measured 2026-11-14 (`papers/v4-operator-r1/A3-LOOP-INVESTIGATION.md` §3): in 2 of 4 unattended re-runs a session
created a stub Job that printed SUCCESS, verified the stub, and reported "The job has been fixed" while the Job the
detector fired on stayed Failed. Nothing checked: the watchtower took the turn's end as the outcome, and the
"fix held" re-check is off by default (`MEMORY_PROSPECTIVE`).

So the watchtower now pins the finding to its **workload** before the fix turn — kind, name and uid (a pod is
followed through its ReplicaSet to the Deployment, since a repair replaces pods) — and afterwards polls that same
object. Outcomes:

* ``fixed``     — the same object (same uid) is healthy.
* ``not_fixed`` — it is still unhealthy at the deadline, is gone, or was replaced by another object of the same
  name (a different uid). Deleting or substituting the object is not a repair.
* ``unknown``   — it could not be pinned or read (no cluster access, a kind this module does not judge). Never
  reported as fixed.

Reads go straight to kubectl with the deployment's kubeconfig, never through the model's tool.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from dataclasses import dataclass

from app.core.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

_POLL_INTERVAL_SECONDS = 10.0
_JOB_PLAYBOOKS = frozenset({"JobBackoffLimitExceeded"})


@dataclass(frozen=True)
class Target:
    kind: str  # deployment | statefulset | daemonset | job | pod
    name: str
    namespace: str
    uid: str


# Returned by `_kubectl_json` when the API server said the object does not exist. Any OTHER failed read (timeout,
# apiserver restarting) is None: no information, which must never be read as "gone" (second review, N3).
NOT_FOUND: dict = {"__not_found__": True}


def _kubectl_json(args: list[str]) -> dict | None:
    """`kubectl <args> -o json` with the deployment's kubeconfig; None on any failure (NotFound included)."""
    try:
        proc = subprocess.run(
            ["kubectl", *args, "-o", "json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            shell=False,
            env={
                **os.environ,
                "KUBECONFIG": os.path.expanduser(settings.KUBECONFIG_PATH),
            },
        )
        if proc.returncode != 0:
            err = proc.stderr or ""
            # Only the API server's NotFound about the object; a kubeconfig error ('context "x" not found') is not it.
            return NOT_FOUND if "(NotFound)" in err else None
        obj = json.loads(proc.stdout)
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001 - an unreadable object is "unknown", never "fixed"
        return None


def _get(kind: str, name: str, namespace: str) -> dict | None:
    """The object, `NOT_FOUND`, or None (the read failed: no information)."""
    return _kubectl_json(["get", kind, name, "-n", namespace])


def _owner(obj: dict) -> tuple[str, str] | None:
    for ref in (obj.get("metadata") or {}).get("ownerReferences") or []:
        if ref.get("controller"):
            return str(ref.get("kind", "")).lower(), str(ref.get("name", ""))
    return None


def resolve_target(playbook: str, namespace: str, object_name: str) -> Target | None:
    """The workload a finding is about, pinned by uid; None when it cannot be read."""
    if playbook in _JOB_PLAYBOOKS:
        job = _get("job", object_name, namespace)
        uid = (
            (job.get("metadata") or {}).get("uid")
            if job is not None and job is not NOT_FOUND
            else None
        )
        return Target("job", object_name, namespace, uid) if uid else None
    obj = _get("pod", object_name, namespace)
    if obj is None or obj is NOT_FOUND:
        return None
    kind, name = "pod", object_name
    for _ in range(
        3
    ):  # pod -> ReplicaSet -> Deployment (or StatefulSet / DaemonSet / Job)
        owner = _owner(obj)
        if owner is None:
            break
        owner_kind, owner_name = owner
        parent = _get(owner_kind, owner_name, namespace)
        if parent is None or parent is NOT_FOUND:
            break
        obj, kind, name = parent, owner_kind, owner_name
        if kind != "replicaset":
            break
    uid = (obj.get("metadata") or {}).get("uid")
    if not uid or kind == "replicaset":
        return None
    return Target(kind, name, namespace, uid)


_JUDGED_KINDS = frozenset({"job", "deployment", "statefulset", "daemonset", "pod"})


def _n(status: dict, key: str) -> int:
    return int(status.get(key, 0) or 0)


def _want(spec: dict) -> int:
    return int(spec["replicas"]) if spec.get("replicas") is not None else 1


def _healthy(target: Target, obj: dict) -> bool | None:
    """True healthy, False terminally failed, None not yet (keep polling).

    A rollout is healthy only when it is COMPLETE — the conditions `kubectl rollout status` waits for. Counting ready
    replicas alone passed a stalled rollout whose old pod was still Ready while the new one crashlooped (independent
    review F3).
    """
    spec, status, meta = (
        obj.get("spec") or {},
        obj.get("status") or {},
        obj.get("metadata") or {},
    )
    settled = _n(status, "observedGeneration") >= int(meta.get("generation", 0) or 0)
    if target.kind == "job":
        conditions = {
            c.get("type"): c.get("status") for c in status.get("conditions") or []
        }
        if conditions.get("Complete") == "True":
            return True
        if conditions.get("Failed") == "True":
            return False
        return None
    if target.kind == "deployment":
        want = _want(spec)
        complete = (
            want > 0
            and settled
            and _n(status, "updatedReplicas") >= want
            and _n(status, "replicas") == _n(status, "updatedReplicas")
            and _n(status, "readyReplicas") >= want
            and _n(status, "availableReplicas") >= want
            and _n(status, "unavailableReplicas") == 0
        )
        return True if complete else None
    if target.kind == "statefulset":
        want = _want(spec)
        complete = (
            want > 0
            and settled
            and _n(status, "readyReplicas") >= want
            and _n(status, "updatedReplicas") >= want
            and status.get("currentRevision") == status.get("updateRevision")
        )
        return True if complete else None
    if target.kind == "daemonset":
        want = _n(status, "desiredNumberScheduled")
        complete = (
            want > 0
            and settled
            and _n(status, "updatedNumberScheduled") >= want
            and _n(status, "numberAvailable") >= want
            and _n(status, "numberUnavailable") == 0
        )
        return True if complete else None
    if target.kind == "pod":
        ready = any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in status.get("conditions") or []
        )
        return True if ready else None
    return None


def _workload_restarts(target: Target, obj: dict) -> int | None:
    """Total container restarts of a workload's pods (by its matchLabels selector), or None when they cannot be read.

    A crashlooping pod behind a Deployment can be Ready at both samples; the Deployment's own status does not show the
    restarts (second review, F3 part 2).
    """
    labels = ((obj.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
    if not labels:
        return None
    selector = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    pods = _kubectl_json(["get", "pods", "-n", target.namespace, "-l", selector])
    if pods is None or pods is NOT_FOUND:
        return None
    return sum(_restarts(p) for p in pods.get("items") or [])


def _restarts(obj: dict) -> int:
    return sum(
        int(c.get("restartCount", 0) or 0)
        for c in (obj.get("status") or {}).get("containerStatuses") or []
    )


async def verify(
    target: Target | None,
    timeout_seconds: float,
    interval: float = _POLL_INTERVAL_SECONDS,
) -> str:
    """``fixed`` | ``not_fixed`` | ``unknown`` for the pinned object, polling until healthy or the deadline.

    `fixed` needs two consecutive healthy samples of the SAME object (same uid) and, for a pod, no restart between
    them: a crashlooping container without a readiness probe is briefly Ready between crashes (review F3). A kind
    this module does not judge is `unknown`, never `not_fixed` (review F5).
    """
    if target is None or timeout_seconds <= 0 or target.kind not in _JUDGED_KINDS:
        return "unknown"
    deadline = time.monotonic() + timeout_seconds
    healthy_in_a_row, last_restarts, definitive, grace, last_unverified = (
        0,
        None,
        False,
        True,
        False,
    )
    while True:
        obj = await asyncio.to_thread(_get, target.kind, target.name, target.namespace)
        if obj is NOT_FOUND:
            return "not_fixed"  # gone: deleting the object is not a repair
        if obj is not None:
            definitive = True
            if (obj.get("metadata") or {}).get("uid") != target.uid:
                return "not_fixed"  # replaced by another object of the same name: a substitute, not a repair
            health = _healthy(target, obj)
            if health is False:
                return "not_fixed"
            # Restarts matter for long-running workloads; a Job is judged by its terminal Complete condition.
            if target.kind == "pod":
                restarts: int | None = _restarts(obj)
            elif target.kind == "job":
                restarts = 0
            elif health is True:
                restarts = await asyncio.to_thread(_workload_restarts, target, obj)
            else:
                restarts = None
            # A healthy sample whose restarts cannot be read (the pods read failed, or the selector has no
            # matchLabels) proves nothing about a crashloop behind it: it does not count (third review pass).
            unverified = health is True and restarts is None
            steady = not unverified and (
                last_restarts is None or restarts == last_restarts
            )
            healthy_in_a_row = (
                healthy_in_a_row + 1 if (health is True and steady) else 0
            )
            last_restarts = restarts if restarts is not None else last_restarts
            last_unverified = unverified
            if healthy_in_a_row >= 2:
                return "fixed"
        # A failed read (obj is None) is no information: it neither confirms nor refutes (second review, N3).
        if time.monotonic() >= deadline:
            if healthy_in_a_row == 1 and grace:
                grace = False  # the first healthy sample landed at the deadline: take its confirming sample (N5)
            else:
                # Healthy but unverifiable, or nothing definitive read: we do not know. Never `fixed` on that.
                return "not_fixed" if definitive and not last_unverified else "unknown"
        await asyncio.sleep(interval)
