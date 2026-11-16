"""A namespace-bound investigation runs kubectl as a short-lived identity that Kubernetes confines to that namespace.

Ten independent review passes (2026-11-14, `papers/v4-operator-r1/A3-LOOP-INVESTIGATION.md` §7e) each found a new
command string that `kubectl_tool`'s parser read differently from kubectl — a repeated `-n` reaching another
namespace, `get pods,secrets` reading Secrets. The string gates cannot be shown complete, so with
SESSION_IDENTITY_ENABLED a bound session's kubectl uses a kubeconfig holding only a TokenRequest token for a
ServiceAccount in its own namespace (ADR-007 Phase 6), and the API server's RBAC decides. These tests pin the server
side: the kubeconfig it builds, that every kubectl call of a bound session uses it, that nothing falls back to the
server's own identity, and that the watchtower does not start an investigation it cannot confine. The RBAC effect on a
real API server is `test_a_session_identity_is_confined_by_a_real_api_server.py`.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import time
from types import SimpleNamespace
from unittest import mock

import pytest
import yaml

from app.core.config import settings
from app.tools import kubectl_tool as kt
from app.tools import session_identity as si

BASE_VIEW = {
    "clusters": [
        {
            "name": "kind-x",
            "cluster": {
                "server": "https://127.0.0.1:6443",
                "certificate-authority-data": "Q0FEQVRB",
                "extensions": [{"name": "x"}],
            },
        }
    ],
    "users": [{"name": "admin", "user": {"client-key-data": "QURNSU5LRVk="}}],
}


def _jwt(exp: float) -> str:
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part({'exp': int(exp), 'sub': 'system:serviceaccount:b3-c1:x'})}.sig"


class FakeKubectl:
    """The server-identity kubectl calls session_identity makes, answered like a real API server would."""

    def __init__(
        self,
        *,
        sa_exists=True,
        role_exists=True,
        apply_rc=0,
        token_rc=0,
        view=BASE_VIEW,
    ):
        self.calls: list[tuple[list[str], str | None, str]] = []
        self.sa_exists, self.role_exists, self.apply_rc, self.token_rc, self.view = (
            sa_exists,
            role_exists,
            apply_rc,
            token_rc,
            view,
        )

    def __call__(self, argv, *a, input=None, env=None, **k):
        self.calls.append((list(argv), input, (env or {}).get("KUBECONFIG", "")))
        if argv[1:3] == ["config", "view"]:
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(self.view), stderr=""
            )
        if argv[1:3] == ["create", "token"]:
            if not self.sa_exists:
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr='error: failed to create token: serviceaccounts "kubeintellect-session" not found',
                )
            if self.token_rc:
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="error: forbidden: cannot create resource",
                )
            return SimpleNamespace(
                returncode=0, stdout=_jwt(time.time() + 900) + "\n", stderr=""
            )
        if argv[1:3] == ["get", "clusterrole"]:
            return SimpleNamespace(
                returncode=0 if self.role_exists else 1,
                stdout="clusterrole/x\n",
                stderr=""
                if self.role_exists
                else 'Error from server (NotFound): clusterroles.rbac.authorization.k8s.io "kubeintellect-session" not found',
            )
        if argv[1:3] == ["apply", "-f"]:
            if self.apply_rc == 0:
                self.sa_exists = True
            return SimpleNamespace(
                returncode=self.apply_rc,
                stdout="",
                stderr="Error from server (Forbidden): no",
            )
        raise AssertionError(f"unexpected kubectl call {argv}")

    def minted(self) -> int:
        return sum(1 for c, _, _ in self.calls if c[1:3] == ["create", "token"])


@pytest.fixture(autouse=True)
def _fresh():
    si.clear_cache()
    yield
    si.clear_cache()


def _issue(fake: FakeKubectl, ns: str = "b3-c1", base: str = "/srv/admin.conf") -> str:
    with mock.patch.object(si.subprocess, "run", fake):
        return si.kubeconfig_for(ns, base)


# ── the kubeconfig ───────────────────────────────────────────────────────────


def test_the_session_kubeconfig_holds_only_a_token_for_the_namespace_service_account():
    fake = FakeKubectl()
    path = _issue(fake)
    doc = yaml.safe_load(open(path, encoding="utf-8"))
    assert [u["user"].keys() for u in doc["users"]] == [{"token": None}.keys()], (
        "one user, a token, nothing else"
    )
    assert doc["contexts"][0]["context"]["namespace"] == "b3-c1"
    assert doc["clusters"][0]["cluster"] == {
        "server": "https://127.0.0.1:6443",
        "certificate-authority-data": "Q0FEQVRB",
    }
    assert "QURNSU5LRVk=" not in open(path, encoding="utf-8").read(), (
        "the server's own credential is never copied"
    )
    token_call = [c for c, _, _ in fake.calls if c[1:3] == ["create", "token"]][0]
    assert token_call == [
        "kubectl",
        "create",
        "token",
        "kubeintellect-session",
        "-n",
        "b3-c1",
        "--duration=900s",
    ]
    assert all(kc == "/srv/admin.conf" for _, _, kc in fake.calls), (
        "minting uses the server's identity"
    )


def test_the_token_file_is_private():
    path = _issue(FakeKubectl())
    assert (
        stat.S_IMODE(os.stat(path).st_mode) == 0o400
    )  # read-only to its owner too (identity review, 2026-11-15)
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700


def test_a_token_is_reused_until_it_nears_expiry_then_reminted(monkeypatch):
    fake = FakeKubectl()
    first = _issue(fake)
    assert _issue(fake) == first and fake.minted() == 1
    real_time = time.time
    monkeypatch.setattr(
        si.time, "time", lambda: real_time() + 900 - 60
    )  # 60 s left: under the margin
    _issue(fake)
    assert fake.minted() == 2


def test_forget_drops_the_token():
    fake = FakeKubectl()
    _issue(fake)
    si.forget("b3-c1")
    _issue(fake)
    assert fake.minted() == 2


def test_an_in_cluster_server_uses_the_pod_api_address(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    fake = FakeKubectl()
    with mock.patch.object(si.subprocess, "run", fake):
        path = si.kubeconfig_for("b3-c1", "")  # the chart sets KUBECONFIG_PATH=""
    cluster = yaml.safe_load(open(path, encoding="utf-8"))["clusters"][0]["cluster"]
    assert cluster == {
        "server": "https://10.96.0.1:443",
        "certificate-authority": si._IN_CLUSTER_CA,
    }


# ── provisioning and failing closed ─────────────────────────────────────────


def test_a_missing_service_account_is_provisioned_bound_to_the_session_role():
    fake = FakeKubectl(sa_exists=False)
    _issue(fake)
    (applied,) = [inp for c, inp, _ in fake.calls if c[1:3] == ["apply", "-f"]]
    sa, rb = list(yaml.safe_load_all(applied))
    assert sa["kind"] == "ServiceAccount" and sa["metadata"] == {
        "name": "kubeintellect-session",
        "namespace": "b3-c1",
        "labels": {
            "app.kubernetes.io/managed-by": "kubeintellect",
            "kubeintellect.io/purpose": "session-identity",
        },
    }
    assert sa["automountServiceAccountToken"] is False
    assert rb["kind"] == "RoleBinding" and rb["metadata"]["namespace"] == "b3-c1", (
        "a RoleBinding, never cluster-wide"
    )
    assert rb["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": "kubeintellect-session",
    }
    assert rb["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": "kubeintellect-session",
            "namespace": "b3-c1",
        }
    ]
    assert fake.minted() == 1, (
        "provisioned before the first token request (once per process), not after a NotFound"
    )


def test_provisioning_off_and_no_service_account_is_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_IDENTITY_PROVISION", False)
    with pytest.raises(si.IdentityUnavailable, match="does not exist"):
        _issue(FakeKubectl(sa_exists=False))


def test_a_missing_cluster_role_is_named_not_provisioned_into_silence():
    fake = FakeKubectl(sa_exists=False, role_exists=False)
    with pytest.raises(
        si.IdentityUnavailable,
        match="ClusterRole 'kubeintellect-session'.*not installed",
    ):
        _issue(fake)
    assert not [c for c, _, _ in fake.calls if c[1:3] == ["apply", "-f"]]


@pytest.mark.parametrize(
    "fake",
    [
        FakeKubectl(token_rc=1),
        FakeKubectl(sa_exists=False, apply_rc=1),
        FakeKubectl(view={"clusters": []}),
    ],
)
def test_any_failure_is_unavailable_never_a_fallback(fake, monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    with pytest.raises(si.IdentityUnavailable):
        _issue(fake)


@pytest.mark.parametrize("ns", ["kube-system", "../etc", "B3", "", "a" * 64, "ns\n"])
def test_a_protected_or_malformed_namespace_gets_no_identity(ns):
    fake = FakeKubectl()
    with pytest.raises(si.IdentityUnavailable):
        _issue(fake, ns=ns)
    assert not fake.calls, "nothing is minted or created"


# ── every kubectl call of a bound session uses it ───────────────────────────


def _run_tool(
    command: str,
    *,
    bound: str | None = "b3-c1",
    bypass: bool = True,
    stdin: str | None = None,
    identity=lambda ns, base: f"/run/ki/{ns}.kubeconfig",
    preflight: bool = False,
):
    seen: list[tuple[list[str], str]] = []

    def fake_run(argv, *a, env=None, **k):
        seen.append((list(argv), (env or {}).get("KUBECONFIG", "")))
        if "diff" in argv:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    conf = {"user_role": "operator", "thread_id": "auto-id", "hitl_bypass": bypass}
    if bound:
        conf["bound_namespace"] = bound
    with (
        mock.patch.object(settings, "SESSION_IDENTITY_ENABLED", True),
        mock.patch.object(settings, "PREFLIGHT_DIFF_ENABLED", preflight),
        mock.patch.object(settings, "KUBECONFIG_PATH", "/srv/admin.conf"),
        mock.patch.object(si, "kubeconfig_for", identity),
        mock.patch.object(kt.subprocess, "run", fake_run),
        mock.patch.object(kt, "interrupt", lambda p: True),
        mock.patch.object(kt, "_capture_rollback_point", lambda *a, **k: None),
    ):
        out = kt.run_kubectl.invoke(
            {"command": command, **({"stdin": stdin} if stdin else {})},
            config={"configurable": conf},
        )
    return out, seen


def test_a_bound_read_runs_as_the_session_identity():
    out, seen = _run_tool("kubectl get pods -n b3-c1")
    assert seen and all(kc == "/run/ki/b3-c1.kubeconfig" for _, kc in seen)


def test_a_bound_write_and_its_dry_run_run_as_the_session_identity():
    cm = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: b3-c1\ndata:\n  k: v\n"
    out, seen = _run_tool("kubectl apply -f - -n b3-c1", stdin=cm, preflight=True)
    verbs = [argv[1] for argv, _ in seen]
    assert "diff" in verbs and "apply" in verbs, verbs
    assert all(kc == "/run/ki/b3-c1.kubeconfig" for _, kc in seen)


def test_an_approval_waiting_bound_session_also_runs_as_it():
    """A2: a human approves the write later, and it still cannot leave the namespace."""
    out, seen = _run_tool(
        "kubectl rollout restart deployment/web -n b3-c1", bypass=False
    )
    assert seen and all(kc == "/run/ki/b3-c1.kubeconfig" for _, kc in seen)


def test_no_identity_means_no_kubectl():
    def unavailable(ns, base):
        raise si.IdentityUnavailable("the token request was refused")

    out, seen = _run_tool("kubectl get pods -n b3-c1", identity=unavailable)
    assert not seen, "never falls back to the server's identity"
    assert (
        out.startswith("[Session identity unavailable]")
        and "token request was refused" in out
    )


def test_a_chat_session_keeps_the_server_identity():
    out, seen = _run_tool("kubectl get pods -n b3-c1", bound=None, bypass=False)
    assert seen and all(kc == "/srv/admin.conf" for _, kc in seen)


def test_off_by_default_changes_nothing():
    assert settings.SESSION_IDENTITY_ENABLED is False
    seen: list[str] = []
    with (
        mock.patch.object(settings, "KUBECONFIG_PATH", "/srv/admin.conf"),
        mock.patch.object(
            kt.subprocess,
            "run",
            lambda argv, *a, env=None, **k: (
                seen.append(env["KUBECONFIG"])
                or SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
            ),
        ),
    ):
        kt.run_kubectl.invoke(
            {"command": "kubectl get pods -n b3-c1"},
            config={
                "configurable": {"user_role": "operator", "bound_namespace": "b3-c1"}
            },
        )
    assert seen == ["/srv/admin.conf"]


def test_unauthorized_with_a_session_token_forgets_it():
    forgotten = []

    def fake_run(argv, *a, env=None, **k):
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="error: You must be logged in to the server (Unauthorized)",
        )

    with (
        mock.patch.object(settings, "SESSION_IDENTITY_ENABLED", True),
        mock.patch.object(
            si, "kubeconfig_for", lambda ns, base: "/run/ki/b3-c1.kubeconfig"
        ),
        mock.patch.object(si, "forget", forgotten.append),
        mock.patch.object(kt.subprocess, "run", fake_run),
    ):
        kt.run_kubectl.invoke(
            {"command": "kubectl get pods -n b3-c1"},
            config={
                "configurable": {"user_role": "operator", "bound_namespace": "b3-c1"}
            },
        )
    assert forgotten == ["b3-c1"]


# ── the watchtower does not start what it cannot confine ────────────────────


@pytest.mark.asyncio
async def test_the_watchtower_records_error_and_runs_no_turn_without_an_identity(
    monkeypatch,
):
    from app.autonomy import watchtower as wt
    from app.detectors.models import Finding

    monkeypatch.setattr(wt, "a3_allowed", lambda playbook, ns: True)
    monkeypatch.setattr(wt.settings, "AUTO_APPROVE_ENABLED", True)
    monkeypatch.setattr(wt.settings, "SESSION_IDENTITY_ENABLED", True)
    monkeypatch.setattr(wt.settings, "AUTOFIX_BREAKER_SECONDS", 3600)

    def unavailable(ns, base):
        raise si.IdentityUnavailable(
            "ClusterRole 'kubeintellect-session' is not installed"
        )

    monkeypatch.setattr(si, "kubeconfig_for", unavailable)
    turns, concluded = [], []
    monkeypatch.setattr(wt, "_run_turn", lambda *a, **k: turns.append(a))
    real = wt._conclude
    monkeypatch.setattr(
        wt, "_conclude", lambda f, s, o, d: (concluded.append((o, d)), real(f, s, o, d))
    )
    finding = Finding(
        playbook="CreateContainerConfigError",
        cluster_id="c",
        namespace="b3-c1",
        object_name="web-1",
        evidence="x",
    )
    await wt._investigate(finding, "A3")
    assert not turns
    assert concluded == [
        (
            "error",
            {
                "error": "session identity unavailable: ClusterRole 'kubeintellect-session' "
                "is not installed"
            },
        )
    ]
    assert wt._should_auto_fix(finding, "A3") is True, (
        "a missing identity is a deployment fault, not a failed fix"
    )


# ── the readonly role runs as the read-only identity ────────────────────────


def test_the_readonly_identity_is_minted_in_the_home_namespace_and_never_provisioned(
    monkeypatch,
):
    monkeypatch.setattr(settings, "READONLY_IDENTITY_NAMESPACE", "kubeintellect")
    view = {
        **BASE_VIEW,
        "contexts": [
            {"name": "c", "context": {"cluster": "kind-x", "namespace": "team-a"}}
        ],
    }
    fake = FakeKubectl(view=view)
    with mock.patch.object(si.subprocess, "run", fake):
        path = si.readonly_kubeconfig("/srv/admin.conf")
    doc = yaml.safe_load(open(path, encoding="utf-8"))
    assert [c for c, _, _ in fake.calls if c[1:3] == ["create", "token"]] == [
        [
            "kubectl",
            "create",
            "token",
            "kubeintellect-readonly",
            "-n",
            "kubeintellect",
            "--duration=900s",
        ]
    ]
    assert doc["contexts"][0]["context"]["namespace"] == "team-a", (
        "an unscoped read means what it meant before"
    )
    missing = FakeKubectl(sa_exists=False)
    si.clear_cache()
    with (
        mock.patch.object(si.subprocess, "run", missing),
        pytest.raises(si.IdentityUnavailable, match="does not exist"),
    ):
        si.readonly_kubeconfig("/srv/admin.conf")
    assert not [c for c, _, _ in missing.calls if c[1] == "apply"], (
        "a cluster-wide grant is never made by the server"
    )


def test_no_home_namespace_is_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "READONLY_IDENTITY_NAMESPACE", "")
    monkeypatch.setattr(si, "_home_namespace", lambda: "")
    with pytest.raises(si.IdentityUnavailable):
        si.readonly_kubeconfig("/srv/admin.conf")


def _run_as(
    role: str,
    *,
    bound: str | None = None,
    readonly=lambda base: "/run/ki/readonly.kubeconfig",
):
    seen: list[str] = []

    def fake_run(argv, *a, env=None, **k):
        seen.append((env or {}).get("KUBECONFIG", ""))
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    conf = {"user_role": role, "thread_id": "ro"}
    if bound:
        conf["bound_namespace"] = bound
    with (
        mock.patch.object(settings, "READONLY_IDENTITY_ENABLED", True),
        mock.patch.object(settings, "SESSION_IDENTITY_ENABLED", True),
        mock.patch.object(settings, "KUBECONFIG_PATH", "/srv/admin.conf"),
        mock.patch.object(si, "readonly_kubeconfig", readonly),
        mock.patch.object(
            si, "kubeconfig_for", lambda ns, base: f"/run/ki/{ns}.kubeconfig"
        ),
        mock.patch.object(kt.subprocess, "run", fake_run),
    ):
        out = kt.run_kubectl.invoke(
            {"command": "kubectl get pods -A"}, config={"configurable": conf}
        )
    return out, seen


def test_a_readonly_caller_runs_as_the_readonly_identity():
    out, seen = _run_as("readonly")
    assert seen == ["/run/ki/readonly.kubeconfig"]


@pytest.mark.parametrize("role", ["operator", "admin", "superadmin"])
def test_other_roles_keep_the_server_identity(role):
    out, seen = _run_as(role)
    assert seen == ["/srv/admin.conf"]


def test_a_bound_session_uses_its_namespace_identity_whatever_its_role():
    out, seen = _run_as("readonly", bound="b3-c1")
    assert seen == ["/run/ki/b3-c1.kubeconfig"]


def test_no_readonly_identity_means_no_kubectl():
    def unavailable(base):
        raise si.IdentityUnavailable(
            "ServiceAccount 'kubeintellect-readonly' does not exist"
        )

    out, seen = _run_as("readonly", readonly=unavailable)
    assert not seen and out.startswith("[Session identity unavailable]")
