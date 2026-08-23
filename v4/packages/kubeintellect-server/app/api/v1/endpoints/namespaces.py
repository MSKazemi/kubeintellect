"""GET /v1/namespaces — list the cluster namespaces the caller is allowed to see.

Protected namespaces are removed here for the same reason `run_kubectl` removes them from
`kubectl get namespaces`: they are the ones the product refuses to operate on, and the
`kubeintellect` namespace is where this release's own credentials live. Until 2026-08-20 this
endpoint ran its own `kubectl` and returned the raw list, so the tool-level filter hardened in
`_filter_namespace_output` was simply not on this path — the same guarantee, two code paths, one
of them not enforcing it.

It also returned ``200 {"namespaces": []}`` when the ``kubectl`` call *failed* — the return code
was never checked — so an unreachable cluster, an expired credential or an RBAC denial was
reported as *"this cluster has zero namespaces"*. That is a confident wrong answer, and `kq`
believed it: `/ns prod` answered **"Namespace 'prod' not found in the cluster"**, sending an
operator to look for a deleted namespace instead of at their kubeconfig. The client already
distinguishes "could not determine" from "not present" and accepts the namespace in the first
case — it just never saw the first case, because this endpoint always claimed success.
An empty list now means exactly one thing: the cluster has no namespaces the caller may see.
"""
from __future__ import annotations

import os
import shlex
import subprocess

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.config import settings

router = APIRouter()


class NamespacesResponse(BaseModel):
    namespaces: list[str]


@router.get("/namespaces", response_model=NamespacesResponse)
def list_namespaces() -> NamespacesResponse:
    kubeconfig = os.path.expanduser(settings.KUBECONFIG_PATH)
    env = {**os.environ, "KUBECONFIG": kubeconfig}
    args = shlex.split("kubectl get namespaces -o jsonpath={.items[*].metadata.name}")
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
            shell=False,
        )
    except FileNotFoundError as exc:  # kubectl not on PATH in this image
        raise HTTPException(
            status_code=503,
            detail="Cannot list namespaces: kubectl is not installed on the server.",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(
            status_code=503,
            detail="Cannot list namespaces: kubectl did not respond within 10s.",
        ) from exc

    if proc.returncode != 0:
        # Report the failure instead of an empty list. Only the first stderr line is passed on:
        # it carries the actionable part ("connection refused", "Unauthorized") without echoing
        # a multi-line dump back to the caller.
        reason = (proc.stderr or "").strip().splitlines()
        detail = reason[0][:300] if reason else f"kubectl exited {proc.returncode}"
        raise HTTPException(status_code=503, detail=f"Cannot list namespaces: {detail}")

    names = proc.stdout.split() if proc.stdout.strip() else []
    blocked = settings.kubectl_blocked_namespaces
    return NamespacesResponse(namespaces=[n for n in names if n.lower() not in blocked])
