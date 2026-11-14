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
            return None
        obj = json.loads(proc.stdout)
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001 - an unreadable object is "unknown", never "fixed"
        return None


def _get(kind: str, name: str, namespace: str) -> dict | None:
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
        uid = ((job or {}).get("metadata") or {}).get("uid")
        return Target("job", object_name, namespace, uid) if uid else None
    obj = _get("pod", object_name, namespace)
    if obj is None:
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
        if parent is None:
            break
        obj, kind, name = parent, owner_kind, owner_name
        if kind != "replicaset":
            break
    uid = (obj.get("metadata") or {}).get("uid")
    if not uid or kind == "replicaset":
        return None
    return Target(kind, name, namespace, uid)


def _healthy(target: Target, obj: dict) -> bool | None:
    """True healthy, False terminally failed, None not yet (keep polling)."""
    spec, status, meta = (
        obj.get("spec") or {},
        obj.get("status") or {},
        obj.get("metadata") or {},
    )
    if target.kind == "job":
        conditions = {
            c.get("type"): c.get("status") for c in status.get("conditions") or []
        }
        if conditions.get("Complete") == "True":
            return True
        if conditions.get("Failed") == "True":
            return False
        return None
    if target.kind in ("deployment", "statefulset"):
        want = int(spec.get("replicas", 1))
        settled = int(status.get("observedGeneration", 0)) >= int(
            meta.get("generation", 0)
        )
        ready = int(status.get("readyReplicas", 0) or 0)
        updated = int(status.get("updatedReplicas", 0) or 0)
        return (
            True
            if (want > 0 and settled and ready >= want and updated >= want)
            else None
        )
    if target.kind == "daemonset":
        want = int(status.get("desiredNumberScheduled", 0) or 0)
        return (
            True
            if (want > 0 and int(status.get("numberReady", 0) or 0) >= want)
            else None
        )
    if target.kind == "pod":
        ready = any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in status.get("conditions") or []
        )
        return True if ready else None
    return None


async def verify(
    target: Target | None,
    timeout_seconds: float,
    interval: float = _POLL_INTERVAL_SECONDS,
) -> str:
    """``fixed`` | ``not_fixed`` | ``unknown`` for the pinned object, polling until healthy or the deadline."""
    if target is None or timeout_seconds <= 0:
        return "unknown"
    deadline = time.monotonic() + timeout_seconds
    while True:
        obj = await asyncio.to_thread(_get, target.kind, target.name, target.namespace)
        if obj is None:
            return "not_fixed"  # gone: deleting the object is not a repair
        if (obj.get("metadata") or {}).get("uid") != target.uid:
            return "not_fixed"  # replaced by another object of the same name: a substitute, not a repair
        health = _healthy(target, obj)
        if health is True:
            return "fixed"
        if health is False or time.monotonic() >= deadline:
            return "not_fixed"
        await asyncio.sleep(interval)
