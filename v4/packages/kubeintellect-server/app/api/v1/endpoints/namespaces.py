"""GET /v1/namespaces — list all cluster namespaces."""
from __future__ import annotations

import os
import shlex
import subprocess

from fastapi import APIRouter
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
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
        shell=False,
    )
    names = proc.stdout.split() if proc.stdout.strip() else []
    return NamespacesResponse(namespaces=names)
