"""A session identity is confined by a real API server, whatever command it sends — and the issuer is least-privilege.

Opt-in: runs only when `KI_TEST_KIND_KUBECONFIG` names the admin kubeconfig of a DISPOSABLE cluster (it creates
namespaces, RBAC objects and a Deployment, and deletes the namespaces after) and `helm` is on PATH. Recorded run:
`papers/v4-operator-r1/data/identity-2026-11-14/` (private).

What it shows that the unit tests cannot:

1. The server side works against RBAC as written, with the chart's own roles: the server, holding only `cluster-ro`,
   `cluster-ops` and the issuer role (not cluster-admin), can provision the ServiceAccount + RoleBinding — the RBAC
   escalation check on the RoleBinding passes through the `bind` grant — and mint the token. It cannot mint a token for
   any other ServiceAccount.
2. Commands sent with the session token, **directly with kubectl, bypassing every string gate in `kubectl_tool`**, are
   decided by the API server: the bypass shapes the review passes found (Secrets by list, by name, by `--raw`; another
   namespace; `--as`) and the faked fix (a new Job) are Forbidden; the repairs the pilots used are allowed.
3. `run_kubectl` in a bound session goes through that identity end to end.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
import yaml

ADMIN = os.environ.get("KI_TEST_KIND_KUBECONFIG", "")
_CHART = Path(__file__).resolve().parents[1] / "deploy" / "helm" / "kubeintellect"

pytestmark = pytest.mark.skipif(
    not ADMIN or shutil.which("helm") is None or shutil.which("kubectl") is None,
    reason="opt-in: set KI_TEST_KIND_KUBECONFIG to a disposable cluster's admin kubeconfig (and have helm, kubectl)",
)


def kc(
    kubeconfig: str, *args: str, stdin: str | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["kubectl", "--kubeconfig", kubeconfig, *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env={**os.environ, "KUBECONFIG": kubeconfig},
    )


def _ok(p: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    assert p.returncode == 0, p.stderr
    return p


def _forbidden(p: subprocess.CompletedProcess) -> bool:
    return p.returncode != 0 and "forbidden" in (p.stderr or "").lower()


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tag = uuid.uuid4().hex[:6]
    home, a, b = f"ki-{tag}", f"si-a-{tag}", f"si-b-{tag}"
    out = tmp_path_factory.mktemp("identity")
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "kubeintellect",
            str(_CHART),
            "--namespace",
            home,
            "--set",
            f"namespace={home}",
            "--set",
            "rbac.createClusterOps=true",
            "--set",
            "rbac.createNamespaced=false",
            "--show-only",
            "templates/serviceaccount.yaml",
            "--show-only",
            "templates/rbac.yaml",
            "--show-only",
            "templates/session-identity.yaml",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert rendered.returncode == 0, rendered.stderr
    for ns in (home, a, b):
        _ok(kc(ADMIN, "create", "namespace", ns))
    try:
        _ok(kc(ADMIN, "apply", "-f", "-", stdin=rendered.stdout))
        _ok(
            kc(
                ADMIN,
                "-n",
                a,
                "create",
                "secret",
                "generic",
                "db",
                "--from-literal=password=hunter2",
            )
        )
        _ok(
            kc(
                ADMIN,
                "-n",
                a,
                "create",
                "deployment",
                "web",
                "--image=registry.k8s.io/pause:3.10",
            )
        )
        _ok(kc(ADMIN, "-n", b, "create", "configmap", "other", "--from-literal=k=v"))
        # The server's own identity: the chart's ServiceAccount with the chart's roles, never the admin.
        token = _ok(
            kc(
                ADMIN,
                "-n",
                home,
                "create",
                "token",
                "kubeintellect-sa",
                "--duration=3600s",
            )
        ).stdout.strip()
        view = yaml.safe_load(
            _ok(kc(ADMIN, "config", "view", "--minify", "--flatten")).stdout
        )
        server_kc = out / "server.kubeconfig"
        server_kc.write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "v1",
                    "kind": "Config",
                    "clusters": view["clusters"],
                    "users": [{"name": "server", "user": {"token": token}}],
                    "contexts": [
                        {
                            "name": "server",
                            "context": {
                                "cluster": view["clusters"][0]["name"],
                                "user": "server",
                            },
                        }
                    ],
                    "current-context": "server",
                }
            ),
            encoding="utf-8",
        )
        yield {"home": home, "a": a, "b": b, "server": str(server_kc)}
    finally:
        for ns in (home, a, b):
            kc(ADMIN, "delete", "namespace", ns, "--wait=false")
        kc(ADMIN, "delete", "-f", "-", "--ignore-not-found", stdin=rendered.stdout)


@pytest.fixture(scope="module")
def session(world):
    from app.core.config import settings
    from app.tools import session_identity as si

    si.clear_cache()
    assert settings.SESSION_IDENTITY_PROVISION is True
    path = si.kubeconfig_for(world["a"], world["server"])
    yield path
    si.clear_cache()


def test_the_least_privilege_server_provisioned_and_minted(world, session):
    assert _ok(
        kc(ADMIN, "-n", world["a"], "get", "serviceaccount", "kubeintellect-session")
    )
    rb = yaml.safe_load(
        _ok(
            kc(
                ADMIN,
                "-n",
                world["a"],
                "get",
                "rolebinding",
                "kubeintellect-session",
                "-o",
                "yaml",
            )
        ).stdout
    )
    assert rb["roleRef"]["name"] == "kubeintellect-session"
    assert _forbidden(
        kc(world["server"], "-n", "kube-system", "create", "token", "default")
    ), "the issuer can mint the session account's tokens only"
    assert _forbidden(
        kc(world["server"], "-n", world["a"], "create", "token", "default")
    )


@pytest.mark.parametrize(
    "args",
    [
        ["get", "secrets"],
        ["get", "pods,secrets"],
        ["get", "secret", "db", "-o", "yaml"],
        ["get", "--raw", "/api/v1/namespaces/{a}/secrets"],
        ["get", "configmaps", "-n", "{b}"],
        [
            "-n",
            "{a}",
            "get",
            "configmaps",
            "-n",
            "{b}",
        ],  # the repeated -n of review pass 7: last one wins
        ["get", "configmaps", "-A"],
        ["get", "nodes"],
        ["get", "secrets", "--as=system:admin"],
        ["create", "job", "report-new", "--image=busybox"],  # the faked fix
        ["run", "stub", "--image=busybox"],
        ["create", "deployment", "decoy", "--image=busybox"],
        ["delete", "deployment", "web"],
        [
            "create",
            "rolebinding",
            "x",
            "--clusterrole=admin",
            "--serviceaccount={a}:kubeintellect-session",
        ],
        ["create", "token", "kubeintellect-session"],
    ],
)
def test_the_api_server_refuses_what_the_string_gates_once_missed(world, session, args):
    args = [x.format(**world) for x in args]
    if "-n" not in args and "-A" not in args:
        args = ["-n", world["a"], *args]
    p = kc(session, *args)
    assert _forbidden(p), f"{args}: rc={p.returncode} {p.stdout[:200]} {p.stderr[:200]}"


def test_exec_is_not_granted(world, session):
    p = kc(session, "-n", world["a"], "auth", "can-i", "create", "pods/exec")
    assert p.stdout.strip() == "no"


@pytest.mark.parametrize(
    "args",
    [
        ["get", "pods"],
        ["get", "deployment", "web", "-o", "yaml"],
        ["get", "events"],
        ["set", "image", "deployment/web", "pause=registry.k8s.io/pause:3.9"],
        ["rollout", "restart", "deployment/web"],
        [
            "patch",
            "deployment",
            "web",
            "-p",
            '{"metadata":{"annotations":{"ki/probe":"1"}}}',
        ],
        ["create", "configmap", "app-config", "--from-literal=APP_ENV=production"],
        ["delete", "pod", "-l", "app=web", "--wait=false"],
    ],
)
def test_the_repairs_the_pilots_used_are_allowed(world, session, args):
    _ok(kc(session, "-n", world["a"], *args))


def test_run_kubectl_in_a_bound_session_goes_through_the_identity(
    world, session, monkeypatch
):
    from app.core.config import settings
    from app.tools import kubectl_tool as kt

    monkeypatch.setattr(settings, "SESSION_IDENTITY_ENABLED", True)
    monkeypatch.setattr(settings, "KUBECONFIG_PATH", world["server"])
    cfg = {
        "configurable": {
            "user_role": "operator",
            "thread_id": f"id-{world['a']}",
            "bound_namespace": world["a"],
        }
    }
    out = kt.run_kubectl.invoke(
        {"command": f"kubectl get deployment web -n {world['a']}"}, config=cfg
    )
    assert "web" in out and "Forbidden" not in out
    # A read of another namespace that reaches the API server is refused there too (the string gates allow reads).
    out = kt.run_kubectl.invoke(
        {"command": f"kubectl get configmaps -n {world['b']}"}, config=cfg
    )
    assert "forbidden" in out.lower()


def test_control_the_servers_own_identity_could_do_it(world, session):
    """Negative control: the refusals above come from the session identity, not from a cluster that refuses anyway."""
    assert kc(world["server"], "-n", world["b"], "get", "configmaps").returncode == 0
    assert (
        kc(
            world["server"],
            "-n",
            world["a"],
            "create",
            "deployment",
            "decoy",
            "--image=registry.k8s.io/pause:3.10",
        ).returncode
        == 0
    )
    assert kc(ADMIN, "-n", world["a"], "get", "secrets").returncode == 0


@pytest.fixture(scope="module")
def readonly(world):
    from app.core.config import settings
    from app.tools import session_identity as si

    old = settings.READONLY_IDENTITY_NAMESPACE
    settings.READONLY_IDENTITY_NAMESPACE = world["home"]
    try:
        yield si.readonly_kubeconfig(world["server"])
    finally:
        settings.READONLY_IDENTITY_NAMESPACE = old


@pytest.mark.parametrize(
    "args",
    [
        ["get", "secrets", "-A"],
        ["-n", "{a}", "get", "secret", "db", "-o", "yaml"],
        ["-n", "{a}", "get", "pods,secrets"],
        ["get", "--raw", "/api/v1/namespaces/{a}/secrets"],
        ["-n", "{a}", "delete", "deployment", "web"],
        ["-n", "{a}", "create", "configmap", "x", "--from-literal=k=v"],
        ["-n", "{a}", "scale", "deployment", "web", "--replicas=0"],
        ["-n", "{a}", "get", "secrets", "--as=system:admin"],
    ],
)
def test_the_readonly_identity_cannot_write_or_read_a_secret(world, readonly, args):
    p = kc(readonly, *[x.format(**world) for x in args])
    assert _forbidden(p), f"{args}: rc={p.returncode} {p.stdout[:200]} {p.stderr[:200]}"


def test_the_readonly_identity_reads_the_cluster(world, readonly):
    assert "web" in _ok(kc(readonly, "get", "deployments", "-A")).stdout
    _ok(kc(readonly, "get", "nodes"))


def test_kubectl_itself_cannot_rewrite_the_session_kubeconfig(world, session):
    """Defence in depth behind the `kubectl config` refusal (identity review, finding 1): the file kubectl is pointed at
    is read-only, so even a command that reached kubectl could not change the identity the next call uses."""
    before = Path(session).read_bytes()
    p = kc(session, "config", "set-context", "--current", f"--namespace={world['b']}")
    assert p.returncode != 0, p.stdout
    assert Path(session).read_bytes() == before
    assert "kubeintellect-session" in _ok(kc(session, "auth", "whoami")).stdout
