"""KubeIntellect reaches the clusters the deployment lists, one command at a time, and no other (plan Phase 6b).

Owner decision 2026-11-15: single cluster by default — `--context` is refused, as it always was, because which cluster a
command reaches is the deployment's decision, not the query's. A deployment that manages several clusters lists them
(`KUBECTL_ALLOWED_CONTEXTS`, contexts of `KUBECTL_CONTEXTS_KUBECONFIG`); a command may then name one of them with
`--context=<name>`:

* only a listed name, given once, with no other connection or identity flag;
* never by editing the kubeconfig — `kubectl config use-context` would switch the one shared file for every session,
  and `config set-credentials` could add a cluster nobody listed; both stay refused;
* kubectl runs with a kubeconfig holding only that context (read-only, checked before reuse), so everything that
  reads the cluster for this call — the rollback capture, the dry-run preflight, the schema check — reads the same one;
* a write on another cluster always asks, even with auto-approve, and the prompt names the cluster;
* an investigation stays on the cluster it was opened for;
* a `readonly` key reads another cluster as the read-only identity minted there (fails closed if it is not installed).
"""

from __future__ import annotations

import os
import stat
from types import SimpleNamespace
from unittest import mock

import pytest
import yaml

from app.core.config import settings
from app.tools import kubectl_tool as kt
from app.tools import session_identity as si

_LISTED = "prod-eu,staging"


@pytest.fixture(autouse=True)
def _fresh():
    si.clear_cache()
    yield
    si.clear_cache()


def _run(
    command,
    role="admin",
    *,
    listed=_LISTED,
    bound=None,
    bypass=False,
    readonly_identity=False,
):
    ran: list[tuple[list[str], str]] = []
    asked: list[dict] = []

    def fake_run(argv, *a, env=None, **k):
        ran.append((list(argv), (env or {}).get("KUBECONFIG", "")))
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    conf: dict = {"user_role": role, "thread_id": f"mc-{role}", "hitl_bypass": bypass}
    if bound:
        conf["bound_namespace"] = bound
    with (
        mock.patch.object(settings, "KUBECTL_ALLOWED_CONTEXTS", listed),
        mock.patch.object(settings, "KUBECONFIG_PATH", "/srv/home.conf"),
        mock.patch.object(
            settings, "KUBECTL_CONTEXTS_KUBECONFIG", "/srv/clusters.conf"
        ),
        mock.patch.object(settings, "READONLY_IDENTITY_ENABLED", readonly_identity),
        mock.patch.object(settings, "AUTO_APPROVE_ENABLED", True),
        mock.patch.object(settings, "MANIFEST_LINT_ENABLED", False),
        mock.patch.object(settings, "PREFLIGHT_DIFF_ENABLED", False),
        mock.patch.object(
            si, "context_kubeconfig", lambda ctx: f"/run/ki/ctx-{ctx}.kubeconfig"
        ),
        mock.patch.object(si, "readonly_kubeconfig", lambda base: f"{base}+readonly"),
        mock.patch.object(
            si, "kubeconfig_for", lambda ns, base: f"/run/ki/{ns}.kubeconfig"
        ),
        mock.patch.object(kt.subprocess, "run", fake_run),
        mock.patch.object(kt, "interrupt", lambda p: asked.append(p) or True),
        mock.patch.object(kt, "_capture_rollback_point", lambda *a, **k: None),
    ):
        out = kt.run_kubectl.invoke({"command": command}, config={"configurable": conf})
    return out, ran, asked


# ── single cluster by default ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "kubectl get pods -n a --context=prod-eu",
        "kubectl get pods -n a --context prod-eu",
    ],
)
def test_with_no_list_a_context_is_refused_as_before(command):
    out, ran, _ = _run(command, listed="")
    assert not ran and out.startswith("[Protected] '--context' is not permitted")


def test_the_setting_is_off_by_default():
    assert (
        settings.KUBECTL_ALLOWED_CONTEXTS == ""
        and settings.kubectl_allowed_contexts == ()
    )


def test_the_list_is_read_as_exact_names():
    with mock.patch.object(
        settings,
        "KUBECTL_ALLOWED_CONTEXTS",
        " prod-eu , ,arn:aws:eks:eu-west-1:1:cluster/x ",
    ):
        assert settings.kubectl_allowed_contexts == (
            "prod-eu",
            "arn:aws:eks:eu-west-1:1:cluster/x",
        )


# ── a listed cluster ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "kubectl get pods -n a --context=prod-eu",
        "kubectl --context prod-eu get pods -n a",
    ],
)
def test_a_listed_context_runs_with_a_kubeconfig_holding_only_it(command):
    out, ran, asked = _run(command)
    assert len(ran) == 1 and not asked
    argv, kc = ran[0]
    assert kc == "/run/ki/ctx-prod-eu.kubeconfig"
    assert (
        not [a for a in argv if a.startswith("--context")] and "prod-eu" not in argv
    ), (
        "the cluster is chosen by the kubeconfig, not by a flag kubectl could be handed twice"
    )
    assert "[cluster: prod-eu]" in out


def test_without_a_context_nothing_changes():
    _, ran, _ = _run("kubectl get pods -n a")
    assert [kc for _, kc in ran] == [os.path.expanduser("/srv/home.conf")]


@pytest.mark.parametrize(
    "command,why",
    [
        ("kubectl get pods -n a --context=prod-us", "not one of the clusters"),
        ("kubectl get pods -n a --context=prod-eu --context=staging", "more than once"),
        ("kubectl get pods -n a --context=", "names no cluster"),
        (
            "kubectl get pods -n a --context=prod-eu --kubeconfig=/srv/x",
            "'--kubeconfig' is not permitted",
        ),
        (
            "kubectl get pods -n a --context=prod-eu --user=admin",
            "'--user' is not permitted",
        ),
        (
            "kubectl get pods -n a --context=prod-eu --as=system:admin",
            "'--as' is not permitted",
        ),
        ("kubectl get pods -n a --context=Prod-EU", "not one of the clusters"),
    ],
)
def test_anything_but_one_listed_name_alone_is_refused(command, why):
    out, ran, _ = _run(command)
    assert not ran and out.startswith("[Protected]") and why in out, out


@pytest.mark.parametrize(
    "command",
    [
        "kubectl config use-context prod-eu",
        "kubectl config set-context prod-eu --cluster=x",
    ],
)
def test_switching_by_editing_the_kubeconfig_is_still_refused(command):
    out, ran, asked = _run(command, role="superadmin")
    assert not ran and not asked and out.startswith("[Protected]")


# ── writes on another cluster ask, and say where ────────────────────────────


def test_a_write_on_a_listed_cluster_always_asks_and_names_it():
    out, ran, asked = _run(
        "kubectl rollout restart deployment/web -n a --context=staging", bypass=True
    )
    assert len(asked) == 1, "auto-approve does not reach another cluster"
    assert (
        asked[0]["cluster"] == "staging"
        and "cluster 'staging'" in asked[0]["human_summary"]
    )
    assert ran and ran[0][1] == "/run/ki/ctx-staging.kubeconfig"


def test_an_investigation_stays_on_its_cluster():
    out, ran, asked = _run(
        "kubectl get pods -n b3-c1 --context=prod-eu",
        role="operator",
        bound="b3-c1",
        bypass=True,
    )
    assert not ran and not asked
    assert out.startswith("[Protected]") and "cluster it was opened for" in out


# ── readonly keys ────────────────────────────────────────────────────────────


def test_a_readonly_key_reads_another_cluster_as_the_readonly_identity_minted_there():
    _, ran, _ = _run(
        "kubectl get pods -n a --context=prod-eu",
        role="readonly",
        readonly_identity=True,
    )
    assert [kc for _, kc in ran] == ["/run/ki/ctx-prod-eu.kubeconfig+readonly"]


def test_a_readonly_key_still_cannot_write_there():
    out, ran, _ = _run(
        "kubectl delete pod web-0 -n a --context=prod-eu",
        role="readonly",
        readonly_identity=True,
    )
    assert not ran and out.startswith("[Permission Denied]")


# ── the per-context kubeconfig ──────────────────────────────────────────────


class _FakeView:
    def __init__(self, rc=0):
        self.calls: list[list[str]] = []
        self.rc = rc

    def __call__(self, argv, *a, env=None, **k):
        self.calls.append(list(argv))
        doc = {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": argv[argv.index("--context") + 1],
        }
        return SimpleNamespace(
            returncode=self.rc,
            stdout=yaml.safe_dump(doc) if self.rc == 0 else "",
            stderr=""
            if self.rc == 0
            else 'error: no context exists with the name: "prod-eu"',
        )


def test_the_context_kubeconfig_is_minified_from_the_listed_source_read_only_and_reused(
    monkeypatch,
):
    monkeypatch.setattr(settings, "KUBECTL_ALLOWED_CONTEXTS", _LISTED)
    monkeypatch.setattr(settings, "KUBECTL_CONTEXTS_KUBECONFIG", "/srv/clusters.conf")
    fake = _FakeView()
    with mock.patch.object(si.subprocess, "run", fake):
        path = si.context_kubeconfig("prod-eu")
        assert si.context_kubeconfig("prod-eu") == path
    assert fake.calls == [
        [
            "kubectl",
            "config",
            "view",
            "--minify",
            "--flatten",
            "--context",
            "prod-eu",
            "-o",
            "yaml",
        ]
    ]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o400
    assert yaml.safe_load(open(path, encoding="utf-8"))["current-context"] == "prod-eu"


def test_a_changed_context_kubeconfig_is_rebuilt(monkeypatch):
    monkeypatch.setattr(settings, "KUBECTL_ALLOWED_CONTEXTS", _LISTED)
    monkeypatch.setattr(settings, "KUBECTL_CONTEXTS_KUBECONFIG", "/srv/clusters.conf")
    fake = _FakeView()
    with mock.patch.object(si.subprocess, "run", fake):
        path = si.context_kubeconfig("prod-eu")
        os.chmod(path, 0o600)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("# changed\n")
        si.context_kubeconfig("prod-eu")
    assert len(fake.calls) == 2


def test_an_unlisted_or_missing_context_has_no_kubeconfig(monkeypatch):
    monkeypatch.setattr(settings, "KUBECTL_ALLOWED_CONTEXTS", _LISTED)
    monkeypatch.setattr(settings, "KUBECTL_CONTEXTS_KUBECONFIG", "/srv/clusters.conf")
    with pytest.raises(si.IdentityUnavailable, match="not listed"):
        si.context_kubeconfig("prod-us")
    with (
        mock.patch.object(si.subprocess, "run", _FakeView(rc=1)),
        pytest.raises(si.IdentityUnavailable),
    ):
        si.context_kubeconfig("prod-eu")


def test_without_a_contexts_kubeconfig_the_server_kubeconfig_is_the_source(monkeypatch):
    monkeypatch.setattr(settings, "KUBECTL_ALLOWED_CONTEXTS", _LISTED)
    monkeypatch.setattr(settings, "KUBECTL_CONTEXTS_KUBECONFIG", "")
    monkeypatch.setattr(settings, "KUBECONFIG_PATH", "/srv/home.conf")
    seen: list[str] = []

    def fake(argv, *a, env=None, **k):
        seen.append(env["KUBECONFIG"])
        return _FakeView()(argv)

    with mock.patch.object(si.subprocess, "run", fake):
        si.context_kubeconfig("staging")
    assert seen == ["/srv/home.conf"]


# ── on a real API server (opt-in, a disposable cluster) ─────────────────────

_ADMIN = os.environ.get("KI_TEST_KIND_KUBECONFIG", "")


@pytest.mark.skipif(
    not _ADMIN,
    reason="opt-in: set KI_TEST_KIND_KUBECONFIG to a disposable cluster's admin kubeconfig",
)
def test_on_a_real_cluster_a_listed_context_runs_with_that_contexts_credentials(
    tmp_path, monkeypatch
):
    """`staging` is a second context with its own credential (a ServiceAccount token): `auth whoami` through it must
    be that ServiceAccount, and without `--context` the home kubeconfig's admin, as before."""
    import subprocess
    import uuid

    def kc(*args):
        p = subprocess.run(
            ["kubectl", "--kubeconfig", _ADMIN, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        assert p.returncode == 0, p.stderr
        return p.stdout

    ns = f"mc-{uuid.uuid4().hex[:6]}"
    kc("create", "namespace", ns)
    try:
        kc("-n", ns, "create", "serviceaccount", "staging-viewer")
        token = kc(
            "-n", ns, "create", "token", "staging-viewer", "--duration=600s"
        ).strip()
        view = yaml.safe_load(kc("config", "view", "--minify", "--flatten"))
        contexts = tmp_path / "clusters.conf"
        contexts.write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "v1",
                    "kind": "Config",
                    "clusters": view["clusters"],
                    "users": [{"name": "viewer", "user": {"token": token}}],
                    "contexts": [
                        {
                            "name": "staging",
                            "context": {
                                "cluster": view["clusters"][0]["name"],
                                "user": "viewer",
                            },
                        }
                    ],
                    "current-context": "staging",
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(settings, "KUBECTL_ALLOWED_CONTEXTS", "staging")
        monkeypatch.setattr(settings, "KUBECTL_CONTEXTS_KUBECONFIG", str(contexts))
        monkeypatch.setattr(settings, "KUBECONFIG_PATH", _ADMIN)
        cfg = {"configurable": {"user_role": "admin", "thread_id": f"mc-real-{ns}"}}
        there = kt.run_kubectl.invoke(
            {"command": "kubectl auth whoami --context=staging"}, config=cfg
        )
        here = kt.run_kubectl.invoke({"command": "kubectl auth whoami"}, config=cfg)
        assert (
            f"system:serviceaccount:{ns}:staging-viewer" in there
            and there.startswith("[cluster: staging]")
        )
        assert "staging-viewer" not in here
        refused = kt.run_kubectl.invoke(
            {"command": "kubectl auth whoami --context=kind-ki-identity"}, config=cfg
        )
        assert (
            refused.startswith("[Protected]") and "not one of the clusters" in refused
        )
    finally:
        subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                _ADMIN,
                "delete",
                "namespace",
                ns,
                "--wait=false",
            ],
            capture_output=True,
        )
