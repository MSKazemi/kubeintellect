"""Cluster identity resolution for the reflexion subsystem.

Every learned pattern is bound to the cluster it was observed on. Without
this, patterns from a Kind dev cluster would pollute prompts on prod EKS
and vice versa.

Identity strategy (in priority order):
  1. `kubectl config current-context` — usually unique, human-readable.
  2. Hash of API server URL — survives context rename, distinguishes
     Kind/EKS/GKE/AKS without any cloud SDK.
  3. Literal "unknown" — if both fail, we still write rows but they're
     scoped to a sentinel that read paths can filter out.

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


@lru_cache(maxsize=1)
def get_cluster_id() -> str:
    """Return a stable identifier for the cluster this process is connected to."""
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

    logger.warning("cluster_id: could not resolve cluster identity, returning 'unknown'")
    return "unknown"


def reset_cluster_id_cache() -> None:
    """Clear the cluster_id cache. Used by tests; rarely needed at runtime."""
    get_cluster_id.cache_clear()
