"""Cluster identity resolution for the reflexion subsystem.

Every learned pattern is bound to the cluster it was observed on. Without
this, patterns from a Kind dev cluster would pollute prompts on prod EKS
and vice versa.

Identity strategy (in priority order):
  1. `CLUSTER_ID` — an explicit name from config. Always wins.
  2. `kubectl config current-context` — usually unique, human-readable.
  3. Hash of API server URL — survives context rename, distinguishes
     Kind/EKS/GKE/AKS without any cloud SDK.
  4. The `kube-system` namespace UID — the conventional cluster identifier, and the only
     one available in-cluster. Best-effort: it needs cluster-scoped read permission, which
     the chart's default (namespaced, read-only) RBAC does not grant.
  5. Literal "unknown" — the sentinel.

**Read the sentinel correctly.** Strategies 2 and 3 both shell out to `kubectl config`, which
needs a kubeconfig *file*. An in-cluster deployment has none — the chart sets
``KUBECONFIG_PATH: ""`` so kubectl uses the pod's ServiceAccount — and both commands then exit 1
with empty stdout (verified against kubectl: "error: current-context is not set" and "error:
current-context must exist in order to minify"). So until 2026-08-20, *every* Helm-deployed
instance resolved to "unknown", and the per-cluster scoping this module exists to provide was
inert in precisely the deployment mode it was written for. It looked fine in development, where a
kubeconfig is present.

This docstring also used to claim the sentinel was "scoped to a sentinel that read paths can
filter out". No read path filters it, and deliberately so: in a single-cluster deployment every
row is sentinel-scoped and is legitimately that cluster's own data, so discarding it would break
memory for the common case in order to protect the rare one. **The mitigation is to set
``CLUSTER_ID`` whenever several clusters share one database** — not to drop rows on read.

Cached for the lifetime of the process. Cluster context is configured at
deploy time; runtime rotation is rare and a process restart picks it up.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from functools import lru_cache

from app.core.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _kubectl(args: list[str], timeout: int = 5) -> str:
    """Run kubectl and return stdout, or empty string on any failure."""
    kubeconfig = os.path.expanduser(settings.KUBECONFIG_PATH)
    env = {**os.environ, "KUBECONFIG": kubeconfig}
    try:
        proc = subprocess.run(
            ["kubectl"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            shell=False,
        )
        return (proc.stdout or "").strip()
    except Exception as exc:
        logger.debug(f"cluster_id: kubectl {' '.join(args[:3])} failed: {exc}")
        return ""


#: What ``get_cluster_id`` returns when nothing identifies the cluster. Exported so callers can
#: recognise it rather than hardcoding the string (the SQL guards elsewhere exclude ``''``, which
#: this function never returns — a mismatch that made those guards look like they covered this).
UNRESOLVED_CLUSTER_ID = "unknown"


def cluster_id_is_resolved(cluster_id: str) -> bool:
    """False when the id is the sentinel — i.e. memory is not actually scoped to a cluster."""
    return bool(cluster_id) and cluster_id != UNRESOLVED_CLUSTER_ID


@lru_cache(maxsize=1)
def get_cluster_id() -> str:
    """Return a stable identifier for the cluster this process is connected to."""
    configured = (settings.CLUSTER_ID or "").strip()
    if configured:
        return configured

    ctx = _kubectl(["config", "current-context"])

    # Server URL gives us a deterministic fallback when contexts are
    # renamed or absent. Even short-lived clusters keep their server URL.
    server = _kubectl(["config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"])

    if ctx and server:
        # Hash the server to keep the id short and avoid leaking internal hostnames.
        digest = hashlib.sha256(server.encode("utf-8")).hexdigest()[:8]
        return f"{ctx}:{digest}"

    if ctx:
        return ctx

    if server:
        digest = hashlib.sha256(server.encode("utf-8")).hexdigest()[:12]
        return f"server:{digest}"

    # In-cluster: there is no kubeconfig to read, so ask the API for the conventional cluster
    # identifier. Reading kube-system's immutable UID is not operating on the namespace — the
    # protected-namespace rules govern what the agent may *do*, not this internal identity probe.
    uid = _kubectl(["get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}"])
    if uid:
        return f"uid:{uid[:12]}"

    logger.warning(
        "cluster_id: could not resolve cluster identity — falling back to %r. Memory, findings "
        "and learned patterns are scoped to this id, so if more than one cluster writes to this "
        "database they will share one scope. Set CLUSTER_ID to name this cluster explicitly.",
        UNRESOLVED_CLUSTER_ID,
    )
    return UNRESOLVED_CLUSTER_ID


def reset_cluster_id_cache() -> None:
    """Clear the cluster_id cache. Used by tests; rarely needed at runtime."""
    get_cluster_id.cache_clear()
