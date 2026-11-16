"""A call made for one listed cluster is never answered, recorded, checked or shown as if it were for another.

An independent review of the multi-cluster allowlist (2026-11-16, reproduced on Kind) found no way to reach an
unlisted cluster, but six places where `--context` was dropped before something that keys on the call:

1. the exactly-once guard (SELF_GOVERN_ENABLED) keyed `delete pvc data-a -n prod` and the same delete on
   `--context=staging` identically, so the second was "replayed" from the first cluster's result and never ran;
2. the read-only identity of every cluster was written to one file, so a concurrent read of one cluster could run
   against another's server and token;
3. the reflexion check verified a fix on another cluster by reading the home cluster, and learned from it;
4. `config view --context=<listed>` printed exec-plugin arguments the redactor does not recognise as secrets;
5. a rollback point taken on another cluster named no cluster, so a restore would target the home cluster;
6. the approval request on the wire carried no cluster.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from app.core.config import settings
from app.tools import kubectl_tool as kt
from app.tools import session_identity as si

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _listed(monkeypatch):
    monkeypatch.setattr(settings, "KUBECTL_ALLOWED_CONTEXTS", "staging")
    monkeypatch.setattr(settings, "KUBECONFIG_PATH", "/srv/home.conf")
    si.clear_cache()
    yield
    si.clear_cache()


# 1 ── exactly-once guard ─────────────────────────────────────────────────────


def test_the_same_irreversible_write_on_two_clusters_is_two_actions(monkeypatch):
    from test_adr008_exactly_once_irreversible import (
        _approve_with_token,
        _cfg,
        _Cluster,
        _Store,
    )

    from app.db import effect_log

    store, cluster, prompts = _Store(), _Cluster(), []

    def guard_interrupt(v):
        prompts.append(v)
        return _approve_with_token(v)

    monkeypatch.setattr(settings, "SELF_GOVERN_ENABLED", True)
    monkeypatch.setattr(effect_log, "get_store", lambda: store)
    monkeypatch.setattr(
        si, "context_kubeconfig", lambda c: f"/run/ki/ctx-{c}.kubeconfig"
    )
    with (
        mock.patch("app.tools.effect_guard.interrupt", guard_interrupt),
        mock.patch("subprocess.run", side_effect=cluster),
    ):
        cfg = _cfg(bypass=False)
        kt.run_kubectl.invoke(
            {"command": "kubectl delete pvc data-a -n prod"}, config=cfg
        )
        second = kt.run_kubectl.invoke(
            {"command": "kubectl delete pvc data-a -n prod --context=staging"},
            config=cfg,
        )
    assert "Replayed" not in second, second
    assert len(cluster.mutations) == 2, cluster.mutations
    assert (
        prompts[-1].get("cluster") == "staging"
        and "cluster 'staging'" in prompts[-1]["human_summary"]
    )


# 2 ── one read-only identity file per cluster ────────────────────────────────


def test_each_cluster_gets_its_own_readonly_identity_file(monkeypatch):
    from test_a_bound_investigation_runs_as_its_own_namespace_identity import (
        FakeKubectl,
    )

    monkeypatch.setattr(settings, "READONLY_IDENTITY_NAMESPACE", "kubeintellect")
    with mock.patch.object(si.subprocess, "run", FakeKubectl()):
        home = si.readonly_kubeconfig("/srv/home.conf")
        staging = si.readonly_kubeconfig("/run/ki/ctx-staging.kubeconfig")
    assert home != staging
    assert os.path.exists(home) and os.path.exists(staging)


# 3 ── reflexion does not learn another cluster's fix from the home cluster ──


def test_a_fix_on_another_cluster_is_not_verified_or_learned_on_this_one(monkeypatch):
    from langchain_core.messages import AIMessage

    from app.agent.nodes import coordinator as co

    monkeypatch.setattr(settings, "REFLEXION_ENABLED", True)
    monkeypatch.setattr(settings, "USE_SQLITE", False)
    verified: list = []
    monkeypatch.setattr(
        co,
        "_verify_resolution",
        lambda ns, pre_state=None: verified.append(ns) or (True, "resolved"),
    )
    msg = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "run_kubectl",
                "id": "1",
                "args": {
                    "command": "kubectl rollout restart deployment/web -n shop --context=staging"
                },
            }
        ],
    )
    keyed: list = []
    monkeypatch.setattr(co, "_outcome_key", lambda state, ns: keyed.append(ns) or "k")
    co._maybe_record_direct_outcome({"session_id": "s", "user_id": "u"}, [msg])
    assert verified == [], "the home cluster says nothing about staging"
    assert keyed == [], "and no outcome is recorded for it"


# 4 ── no kubeconfig of another cluster is shown ─────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "kubectl config view --context=staging",
        "kubectl config view --minify --context=staging -o jsonpath={.users[0].user.exec}",
        "kubectl --context staging config current-context",
    ],
)
def test_another_clusters_kubeconfig_is_not_shown(command, monkeypatch):
    ran: list = []
    monkeypatch.setattr(
        si, "context_kubeconfig", lambda c: f"/run/ki/ctx-{c}.kubeconfig"
    )
    with mock.patch.object(
        kt.subprocess,
        "run",
        lambda *a, **k: (
            ran.append(a) or SimpleNamespace(returncode=0, stdout="x", stderr="")
        ),
    ):
        out = kt.run_kubectl.invoke(
            {"command": command}, config={"configurable": {"user_role": "admin"}}
        )
    assert not ran and out.startswith("[Protected]")


# 5 ── a rollback point names its cluster ────────────────────────────────────


def test_a_rollback_point_on_another_cluster_names_it(monkeypatch):
    recorded: list[dict] = []
    monkeypatch.setattr(
        si, "context_kubeconfig", lambda c: f"/run/ki/ctx-{c}.kubeconfig"
    )
    monkeypatch.setattr(
        "app.db.flight_recorder.record",
        lambda sid, kind, payload: recorded.append(payload),
    )
    with (
        mock.patch.object(
            kt.subprocess,
            "run",
            lambda argv, *a, **k: SimpleNamespace(
                returncode=0,
                stdout="apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n  namespace: a\nspec:\n  replicas: 2\n",
                stderr="",
            ),
        ),
        mock.patch.object(kt, "interrupt", lambda p: True),
    ):
        kt.run_kubectl.invoke(
            {
                "command": "kubectl scale deployment web --replicas=0 -n a --context=staging"
            },
            config={"configurable": {"user_role": "admin", "thread_id": "rb"}},
        )
    (point,) = [p for p in recorded if p.get("type") == "rollback_point"]
    assert point["context"] == "staging" and "--context=staging" in point["command"]


def test_a_home_rollback_point_is_unchanged(monkeypatch):
    recorded: list[dict] = []
    monkeypatch.setattr(
        "app.db.flight_recorder.record",
        lambda sid, kind, payload: recorded.append(payload),
    )
    with (
        mock.patch.object(
            kt.subprocess,
            "run",
            lambda argv, *a, **k: SimpleNamespace(
                returncode=0,
                stdout="apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n",
                stderr="",
            ),
        ),
        mock.patch.object(kt, "interrupt", lambda p: True),
    ):
        kt.run_kubectl.invoke(
            {"command": "kubectl scale deployment web --replicas=0 -n a"},
            config={"configurable": {"user_role": "admin", "thread_id": "rb2"}},
        )
    (point,) = [p for p in recorded if p.get("type") == "rollback_point"]
    assert point.get("context") is None and "--context" not in point["command"]


# 6 ── the approval request on the wire names the cluster ───────────────────


def test_the_approval_request_on_the_wire_names_the_cluster():
    from app.agent.workflow import _translate_raw_event

    ev = _translate_raw_event(
        "s",
        {
            "event": "on_hitl_interrupt",
            "data": {
                "risk_level": "high",
                "command": "kubectl rollout restart deploy/web -n a --context=staging",
                "cluster": "staging",
            },
        },
    )
    assert ev is not None and ev.cluster == "staging"


def test_the_chat_completion_shows_the_cluster_to_the_approver():
    from app.api.v1.endpoints import chat_completions as cc

    chunk = cc._serialise_event(
        "c1",
        {
            "type": "hitl_request",
            "action_id": "a",
            "risk_level": "high",
            "command": "kubectl rollout restart deploy/web -n a --context=staging",
            "cluster": "staging",
        },
    )
    text = str(chunk)
    assert "staging" in text and "Cluster" in text
