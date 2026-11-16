"""The chart's multi-cluster values (plan Phase 6b): off by default; when on, the listed contexts come with the kubeconfig
that holds them, mounted read-only, and the home cluster keeps its in-cluster identity.

`multiCluster.allowedContexts` without `multiCluster.kubeconfigSecret` would list clusters the pod has no credentials
for, so every `--context` would fail at run time; the chart refuses to render instead.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = Path(__file__).resolve().parents[1] / "deploy" / "helm" / "kubeintellect"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="needs a real helm to render the chart"
)


def _render(*sets: str) -> subprocess.CompletedProcess:
    args = ["helm", "template", "mc", str(_CHART)]
    for s in sets:
        args += ["--set", s]
    return subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", timeout=120
    )


def _docs(proc: subprocess.CompletedProcess) -> list[dict]:
    assert proc.returncode == 0, proc.stderr
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _config(docs) -> dict:
    return next(
        d
        for d in docs
        if d["kind"] == "ConfigMap" and "KUBECONFIG_PATH" in (d.get("data") or {})
    )["data"]


def _pod(docs) -> dict:
    return next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"][
        "spec"
    ]


def test_off_by_default_single_cluster():
    docs = _docs(_render())
    data = _config(docs)
    assert (
        data["KUBECTL_ALLOWED_CONTEXTS"] == ""
        and data["KUBECTL_CONTEXTS_KUBECONFIG"] == ""
    )
    assert data["KUBECONFIG_PATH"] == "", "the home cluster stays in-cluster"
    assert not [
        v
        for v in _pod(docs).get("volumes") or []
        if v.get("name") == "cluster-contexts"
    ]


def test_listed_contexts_come_with_their_kubeconfig_mounted_read_only():
    docs = _docs(
        _render(
            "multiCluster.allowedContexts={prod-eu,staging}",
            "multiCluster.kubeconfigSecret=ki-clusters",
        )
    )
    data = _config(docs)
    assert data["KUBECTL_ALLOWED_CONTEXTS"] == "prod-eu,staging"
    assert data["KUBECTL_CONTEXTS_KUBECONFIG"] == "/etc/kubeintellect/clusters/config"
    assert data["KUBECONFIG_PATH"] == "", (
        "the home cluster keeps the pod's own identity"
    )
    pod = _pod(docs)
    (vol,) = [v for v in pod["volumes"] if v["name"] == "cluster-contexts"]
    assert (
        vol["secret"]["secretName"] == "ki-clusters"
        and vol["secret"]["defaultMode"] == 0o440
    )
    (mount,) = [
        m
        for m in pod["containers"][0]["volumeMounts"]
        if m["name"] == "cluster-contexts"
    ]
    assert (
        mount["mountPath"] == "/etc/kubeintellect/clusters"
        and mount["readOnly"] is True
    )
    assert pod["securityContext"]["fsGroup"] == 1001, (
        "group-readable by the pod's own group only"
    )


def test_listed_contexts_without_a_kubeconfig_do_not_render():
    proc = _render("multiCluster.allowedContexts={prod-eu}")
    assert proc.returncode != 0 and "multiCluster.kubeconfigSecret" in proc.stderr
