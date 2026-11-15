"""A session identity holds after the independent review of ADR-007 Phase 6 (2026-11-15).

The review reproduced its findings on a Kind cluster. The host-local commands (findings 1 and 2) are refused in
`test_commands_that_act_on_the_kubeintellect_host_are_refused.py`; the chart's scope (finding 3) is pinned in
`test_the_session_role_is_never_broader_than_the_server.py`. This file pins the rest:

* the session kubeconfig is the file kubectl is pointed at, so kubectl can write to it. It is now read-only and checked
  against what was written before every reuse; a changed file is never used, it is re-issued (defence in depth behind
  the `kubectl config` refusal);
* the pre-fetch reads a bound session's turn makes before the model runs (`context_fetcher`, `targeted_investigator`)
  ran as the server, so a bound session read a model-named namespace with the server's identity (finding 4); they now
  use the session identity and stay in the session's namespace;
* the manifest schema check read the schema as the server (finding 5); it uses the call's identity;
* a filesystem or permission error while issuing the identity escaped as a crash (finding 6); it is `IdentityUnavailable`;
* a pre-existing session ServiceAccount was used without checking its RoleBinding; with provisioning on, the binding is
  applied once per process, so a RoleBinding to another role is reported, not used;
* in-cluster, the read-only identity's unscoped reads went to `default`, not to the server pod's own namespace;
* "approval elevates" (owner, 2026-11-15): a bound session that stops at an approval prompt says what an approval
  means (it runs with the approver's role and the server's own permissions, outside the namespace's identity), and a
  Forbidden from the session identity says the investigation reached its bound, so the model stops instead of
  rephrasing.
"""

from __future__ import annotations

import logging
import os
import stat
from types import SimpleNamespace
from unittest import mock

import pytest
import yaml

from app.core.config import settings
from app.tools import kubectl_tool as kt
from app.tools import session_identity as si
from tests.test_a_bound_investigation_runs_as_its_own_namespace_identity import (
    FakeKubectl,
    _issue,
)


@pytest.fixture(autouse=True)
def _fresh():
    si.clear_cache()
    yield
    si.clear_cache()


# ── the session kubeconfig cannot be changed and then used ──────────────────


def test_the_session_kubeconfig_is_read_only_to_its_owner():
    path = _issue(FakeKubectl())
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o400


def test_a_changed_session_kubeconfig_is_never_reused(caplog):
    fake = FakeKubectl()
    path = _issue(fake)
    original = yaml.safe_load(open(path, encoding="utf-8"))
    os.chmod(path, 0o600)
    with open(
        path, "w", encoding="utf-8"
    ) as fh:  # what `kubectl config set …` would have written
        yaml.safe_dump(
            {
                **original,
                "users": [{"name": "session", "user": {"tokenFile": "/srv/other"}}],
            },
            fh,
        )
    with caplog.at_level(logging.WARNING):
        again = _issue(fake)
    doc = yaml.safe_load(open(again, encoding="utf-8"))
    assert list(doc["users"][0]["user"]) == ["token"], "the changed file was not used"
    assert fake.minted() == 2, "re-issued, not repaired from the changed file"
    assert any("changed" in r.getMessage() for r in caplog.records)


def test_a_deleted_session_kubeconfig_is_re_issued():
    fake = FakeKubectl()
    path = _issue(fake)
    os.unlink(path)
    assert os.path.exists(_issue(fake)) and fake.minted() == 2


# ── a filesystem error is IdentityUnavailable, not a crash ──────────────────


@pytest.mark.parametrize(
    "exc", [PermissionError("denied"), OSError(28, "No space left on device")]
)
def test_a_filesystem_error_while_issuing_is_unavailable(monkeypatch, exc):
    def broken():
        raise exc

    monkeypatch.setattr(si, "_private_dir", broken)
    with pytest.raises(si.IdentityUnavailable):
        _issue(FakeKubectl())


def test_kubectl_that_cannot_be_executed_is_unavailable():
    def not_executable(*a, **k):
        raise PermissionError(13, "Permission denied")

    with (
        mock.patch.object(si.subprocess, "run", not_executable),
        pytest.raises(si.IdentityUnavailable),
    ):
        si.kubeconfig_for("b3-c1", "/srv/admin.conf")


# ── an existing ServiceAccount's binding is applied, not assumed ────────────


def test_an_existing_service_account_still_gets_its_binding_applied_once_per_process():
    fake = FakeKubectl(sa_exists=True)
    _issue(fake)
    _issue(fake, ns="b3-c2")
    si.forget("b3-c1")
    _issue(fake)
    applied = [inp for c, inp, _ in fake.calls if c[1:3] == ["apply", "-f"]]
    assert [
        list(yaml.safe_load_all(a))[1]["metadata"]["namespace"] for a in applied
    ] == ["b3-c1", "b3-c2"]


def test_a_binding_to_another_role_is_reported_not_used():
    # The API server refuses to change a RoleBinding's roleRef; the apply fails and nothing is minted.
    fake = FakeKubectl(sa_exists=True, apply_rc=1)
    with pytest.raises(si.IdentityUnavailable, match="could not provision"):
        _issue(fake)
    assert fake.minted() == 0


def test_with_provisioning_off_an_existing_account_is_used_as_the_chart_made_it(
    monkeypatch,
):
    monkeypatch.setattr(settings, "SESSION_IDENTITY_PROVISION", False)
    fake = FakeKubectl(sa_exists=True)
    _issue(fake)
    assert not [c for c, _, _ in fake.calls if c[1] == "apply"]


# ── in-cluster, the read-only identity defaults to the pod's own namespace ──


def test_in_cluster_the_readonly_identity_defaults_to_the_pod_namespace(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.setattr(settings, "READONLY_IDENTITY_NAMESPACE", "kubeintellect")
    monkeypatch.setattr(si, "_pod_namespace", lambda: "kubeintellect")
    with mock.patch.object(si.subprocess, "run", FakeKubectl()):
        path = si.readonly_kubeconfig("")
    assert (
        yaml.safe_load(open(path, encoding="utf-8"))["contexts"][0]["context"][
            "namespace"
        ]
        == "kubeintellect"
    )


# ── the pre-fetch reads of a bound session use its identity ─────────────────


def _bound(ns: str = "b3-c1") -> dict:
    return {
        "configurable": {
            "thread_id": "pre",
            "user_role": "operator",
            "bound_namespace": ns,
            "hitl_bypass": True,
        }
    }


def _snapshot_run(seen):
    def fake_run(argv, *a, env=None, **k):
        seen.append((list(argv), (env or {}).get("KUBECONFIG", "")))
        return SimpleNamespace(
            returncode=0, stdout="NAME READY STATUS\nweb-0 1/1 Running\n", stderr=""
        )

    return fake_run


@pytest.fixture
def identity_on(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_IDENTITY_ENABLED", True)
    monkeypatch.setattr(settings, "KUBECONFIG_PATH", "/srv/admin.conf")
    monkeypatch.setattr(
        si, "kubeconfig_for", lambda ns, base: f"/run/ki/{ns}.kubeconfig"
    )


def test_a_snapshot_read_of_a_bound_session_runs_as_its_identity(identity_on):
    from app.agent.nodes import context_fetcher as cf

    seen: list = []
    with mock.patch.object(cf.subprocess, "run", _snapshot_run(seen)):
        ok, _, _ = cf._kubectl_snapshot(["get", "pods", "-n", "b3-c1"], _bound())
    assert ok and seen == [
        (["kubectl", "get", "pods", "-n", "b3-c1"], "/run/ki/b3-c1.kubeconfig")
    ]


def test_a_snapshot_read_without_an_identity_does_not_run(identity_on, monkeypatch):
    from app.agent.nodes import context_fetcher as cf

    def unavailable(ns, base):
        raise si.IdentityUnavailable("the token request was refused")

    monkeypatch.setattr(si, "kubeconfig_for", unavailable)
    seen: list = []
    with mock.patch.object(cf.subprocess, "run", _snapshot_run(seen)):
        ok, text, _ = cf._kubectl_snapshot(["get", "pods", "-n", "b3-c1"], _bound())
    assert not ok and not seen and "token request was refused" in text


async def test_the_context_snapshot_of_a_bound_session_stays_in_its_namespace(
    identity_on,
):
    from app.agent.nodes import context_fetcher as cf

    seen: list = []
    with (
        mock.patch.object(cf.subprocess, "run", _snapshot_run(seen)),
        mock.patch.object(cf, "emit", mock.AsyncMock()),
    ):
        await cf.context_fetcher({"session_id": "pre"}, _bound())
    seen = [
        (argv, kc)
        for argv, kc in seen
        if argv[1:3] in (["get", "pods"], ["get", "events"])
    ]  # not cluster_id's
    assert len(seen) == 2, "the snapshot still runs"
    for argv, kc in seen:
        assert kc == "/run/ki/b3-c1.kubeconfig"
        assert "--all-namespaces" not in argv and "-A" not in argv
        assert argv[argv.index("-n") + 1] == "b3-c1"


async def test_a_chat_context_snapshot_is_unchanged(monkeypatch):
    from app.agent.nodes import context_fetcher as cf

    monkeypatch.setattr(settings, "KUBECONFIG_PATH", "/srv/admin.conf")
    seen: list = []
    with (
        mock.patch.object(cf.subprocess, "run", _snapshot_run(seen)),
        mock.patch.object(cf, "emit", mock.AsyncMock()),
    ):
        await cf.context_fetcher(
            {"session_id": "chat"},
            {"configurable": {"thread_id": "chat", "user_role": "admin"}},
        )
    seen = [
        (argv, kc)
        for argv, kc in seen
        if argv[1:3] in (["get", "pods"], ["get", "events"])
    ]
    assert len(seen) == 2 and all(
        "--all-namespaces" in argv and kc == "/srv/admin.conf" for argv, kc in seen
    )


async def test_a_targeted_read_of_a_bound_session_runs_as_its_identity(identity_on):
    from app.agent import workflow as wf
    from app.agent.nodes import context_fetcher as cf

    seen: list = []
    state = {
        "session_id": "pre",
        "targeted_investigation": {"namespace": "b3-c1", "pod": "web-0", "issue": "x"},
    }
    with (
        mock.patch.object(cf.subprocess, "run", _snapshot_run(seen)),
        mock.patch.object(wf, "emit", mock.AsyncMock()),
    ):
        await wf.targeted_investigator(state, _bound())
    assert len(seen) == 3 and all(kc == "/run/ki/b3-c1.kubeconfig" for _, kc in seen)


async def test_a_targeted_read_of_another_namespace_is_not_made_for_a_bound_session(
    identity_on,
):
    """Finding 4: the namespace comes from a line the model wrote."""
    from app.agent import workflow as wf
    from app.agent.nodes import context_fetcher as cf

    seen: list = []
    state = {
        "session_id": "pre",
        "targeted_investigation": {"namespace": "team-b", "pod": "db-0", "issue": "x"},
    }
    with (
        mock.patch.object(cf.subprocess, "run", _snapshot_run(seen)),
        mock.patch.object(wf, "emit", mock.AsyncMock()),
    ):
        out = await wf.targeted_investigator(state, _bound())
    assert not seen
    assert (
        "b3-c1" in out["cluster_snapshot"]
        and "No pod description" in out["cluster_snapshot"]
    )


# ── the manifest schema check reads as the call's identity ──────────────────


def test_the_manifest_check_uses_the_session_identity(identity_on, monkeypatch):
    from app.tools import manifest_lint

    linted: list[str] = []
    monkeypatch.setattr(settings, "MANIFEST_LINT_ENABLED", True)
    monkeypatch.setattr(
        manifest_lint, "lint", lambda stdin, kubeconfig: linted.append(kubeconfig) or []
    )
    cm = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: b3-c1\ndata:\n  k: v\n"
    with (
        mock.patch.object(
            kt.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="ok", stderr=""),
        ),
        mock.patch.object(kt, "_capture_rollback_point", lambda *a, **k: None),
        mock.patch.object(settings, "PREFLIGHT_DIFF_ENABLED", False),
        mock.patch.object(settings, "AUTO_APPROVE_ENABLED", True),
    ):
        kt.run_kubectl.invoke(
            {"command": "kubectl apply -f - -n b3-c1", "stdin": cm}, config=_bound()
        )
    assert linted == ["/run/ki/b3-c1.kubeconfig"]


def test_no_identity_means_no_manifest_check_either(identity_on, monkeypatch):
    from app.tools import manifest_lint

    def unavailable(ns, base):
        raise si.IdentityUnavailable("the token request was refused")

    linted: list[str] = []
    monkeypatch.setattr(si, "kubeconfig_for", unavailable)
    monkeypatch.setattr(settings, "MANIFEST_LINT_ENABLED", True)
    monkeypatch.setattr(
        manifest_lint, "lint", lambda stdin, kubeconfig: linted.append(kubeconfig) or []
    )
    cm = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n"
    out = kt.run_kubectl.invoke(
        {"command": "kubectl apply -f - -n b3-c1", "stdin": cm}, config=_bound()
    )
    assert not linted and out.startswith("[Session identity unavailable]")


# ── approval elevates, and the prompt says so ───────────────────────────────


def _prompt_for(command: str, config: dict) -> dict:
    asked: list[dict] = []
    with (
        mock.patch.object(
            kt.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="ok", stderr=""),
        ),
        mock.patch.object(kt, "_capture_rollback_point", lambda *a, **k: None),
        mock.patch.object(kt, "interrupt", lambda p: asked.append(p) or False),
        mock.patch.object(settings, "AUTO_APPROVE_ENABLED", True),
        mock.patch.object(settings, "PREFLIGHT_DIFF_ENABLED", False),
    ):
        kt.run_kubectl.invoke({"command": command}, config=config)
    assert len(asked) == 1
    return asked[0]


def test_a_bound_session_prompt_says_what_an_approval_grants(identity_on):
    payload = _prompt_for(
        "kubectl create job report-new --image=busybox -n b3-c1", _bound()
    )
    summary = payload["human_summary"]
    assert (
        "b3-c1" in summary
        and "your own role" in summary
        and "KubeIntellect's own Kubernetes permissions" in summary
    )
    assert payload["elevation"] == "server_identity"


def test_a_chat_prompt_is_unchanged(identity_on):
    payload = _prompt_for(
        "kubectl rollout restart deployment/web -n b3-c1",
        {"configurable": {"thread_id": "c", "user_role": "admin"}},
    )
    assert (
        payload["human_summary"]
        == "About to run: `kubectl rollout restart deployment/web -n b3-c1`"
    )
    assert "elevation" not in payload


def test_a_forbidden_from_the_session_identity_says_the_investigation_reached_its_bound(
    identity_on,
):
    def forbidden(argv, *a, env=None, **k):
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr='Error from server (Forbidden): configmaps is forbidden: User "system:serviceaccount:b3-c1:'
            'kubeintellect-session" cannot list resource "configmaps" in API group "" in the namespace "team-b"',
        )

    with mock.patch.object(kt.subprocess, "run", forbidden):
        out = kt.run_kubectl.invoke(
            {"command": "kubectl get configmaps -n team-b"}, config=_bound()
        )
    assert "restricted to namespace 'b3-c1'" in out
    assert "[permanent error" in out
