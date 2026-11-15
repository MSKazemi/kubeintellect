"""Commands that act on the KubeIntellect server itself, not on the cluster, are refused for every role and approval.

Owner decision 2026-11-15 ("approval elevates", single-cluster default): a sysadmin can approve anything a sysadmin
could do *to the cluster*. A few kubectl commands do something else. They change the kubeconfig the server runs with,
read or write files on the server's own disk, or run a program there:

* `kubectl config set|unset|set-credentials|use-context|…` rewrite the kubeconfig kubectl is pointed at. In a
  namespace-bound session that file is the session identity's, and the independent review of ADR-007 Phase 6
  (2026-11-15) swapped the session's token for the server's own credentials this way, with nothing asking a human;
* `--from-file`, `--from-env-file`, `--patch-file`, `-o jsonpath-file=…` and the other `*-file` output formats, and
  `debug --custom` read a file on the server and send or print it; the review copied a server credential into a
  ConfigMap with `--from-file`;
* `--profile`, `--profile-output`, `--cache-dir` write files on the server, `--kuberc` and `kubectl kuberc` load or
  write kubectl preferences that change every later call, `--proxy-url` sends the request and its credential
  through a host the command names;
* `kubectl kustomize`, `cp` and `proxy` read local paths, copy files between the server and a pod, or open a listener
  on the server.

An approval prompt for any of these would show a namespaced-looking command that does something on another machine,
so approving it cannot be informed. They are refused for every role, superadmin included, before any prompt. Reading
the kubeconfig's shape (`config view`, `current-context`, `get-contexts`) and inline output formats stay allowed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from app.core.config import settings
from app.tools import kubectl_tool as kt
from app.tools import session_identity as si

_HOST_LOCAL = [
    # the kubeconfig kubectl runs with
    "kubectl config set users.session.token x",
    "kubectl config unset users.session.token",
    "kubectl config set-credentials session --exec-command=/bin/true",
    "kubectl config set-credentials session --auth-provider=oidc",
    "kubectl config set-cluster cluster --server=https://192.0.2.1",
    "kubectl config set-context --current --namespace=other",
    "kubectl config use-context other",
    "kubectl config delete-context session",
    "kubectl config rename-context session other",
    "kubectl config",
    # kubectl preferences that change every later call
    "kubectl kuberc set --section=defaults --command=get --option=output=wide",
    "kubectl get pods -n default --kuberc=/srv/prefs",
    # reading a file on the server
    "kubectl create configmap c -n default --from-file=k=/srv/f",
    "kubectl create configmap c -n default --from-file /srv/f",
    "kubectl create configmap c -n default --from_file=/srv/f",
    "kubectl create configmap c -n default --from-env-file=/srv/f",
    "kubectl patch deployment web -n default --patch-file=/srv/f",
    "kubectl get pods -n default -o jsonpath-file=/srv/f",
    "kubectl get pods -n default -o=go-template-file=/srv/f",
    "kubectl get pods -n default -ogo-template-file=/srv/f",
    "kubectl get pods -n default --output templatefile --template=/srv/f",
    "kubectl get pods -n default -o custom-columns-file=/srv/f",
    "kubectl debug pod/web-0 -n default --custom=/srv/f --image=busybox",
    "kubectl kustomize /srv/d",
    "kubectl kustomize https://example.invalid/overlay",
    # writing a file on the server, or sending the credential elsewhere
    "kubectl get pods -n default --profile=cpu",
    "kubectl get pods -n default --profile-output=/srv/f",
    "kubectl get pods -n default --profile_output=/srv/f",
    "kubectl get pods -n default --cache-dir=/srv/d",
    "kubectl get pods -n default --proxy-url=http://192.0.2.1:3128",
    # files between the server and a pod, a listener on the server
    "kubectl cp default/web-0:/tmp/x /srv/x",
    "kubectl cp /srv/x default/web-0:/tmp/x",
    "kubectl proxy --port=8001",
]

_STILL_ALLOWED = [
    "kubectl config view",
    "kubectl config view --minify",
    "kubectl config current-context",
    "kubectl config get-contexts",
    "kubectl get pods -n default -o jsonpath={.items[*].metadata.name}",
    "kubectl get pods -n default -o go-template={{.kind}}",
    "kubectl create configmap c -n default --from-literal=k=v",
    "kubectl debug pod/web-0 -n default --profile=general --image=busybox",
    'kubectl patch deployment web -n default -p {"spec":{"replicas":2}}',
]


def _run(command: str, role: str, *, bound: str | None = None, bypass: bool = False):
    ran: list[list[str]] = []
    asked: list[dict] = []

    def fake_run(argv, *a, **k):
        ran.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    def approve(payload):
        asked.append(payload)
        return (
            True  # the approver says yes: approval must not be what lets these through
        )

    conf: dict = {"user_role": role, "thread_id": f"host-{role}", "hitl_bypass": bypass}
    if bound:
        conf["bound_namespace"] = bound
    with (
        mock.patch.object(kt.subprocess, "run", fake_run),
        mock.patch.object(kt, "interrupt", approve),
        mock.patch.object(kt, "_capture_rollback_point", lambda *a, **k: None),
        mock.patch.object(settings, "MANIFEST_LINT_ENABLED", False),
        mock.patch.object(settings, "PREFLIGHT_DIFF_ENABLED", False),
        mock.patch.object(settings, "AUTO_APPROVE_ENABLED", True),
        mock.patch.object(
            si, "kubeconfig_for", lambda ns, base: f"/run/ki/{ns}.kubeconfig"
        ),
    ):
        try:
            out = kt.run_kubectl.invoke(
                {"command": command}, config={"configurable": conf}
            )
        except (
            ValueError
        ) as exc:  # the metacharacter / parse gates raise; also a refusal
            out = f"[refused] {exc}"
    return out, ran, asked


@pytest.mark.parametrize("command", _HOST_LOCAL)
@pytest.mark.parametrize("role", ["superadmin", "admin", "operator"])
def test_no_role_and_no_approval_runs_a_command_on_the_kubeintellect_host(
    command, role
):
    out, ran, asked = _run(command, role)
    assert not ran, f"{role} ran {command!r}: {out}"
    assert out.startswith(("[Protected]", "[Permission Denied]", "[Unsupported]")), out
    assert not asked, "refused before any approval prompt"


@pytest.mark.parametrize("command", _HOST_LOCAL)
def test_a_readonly_key_runs_none_of_them_either(command):
    out, ran, _ = _run(command, "readonly")
    assert not ran, out
    assert out.startswith(("[Protected]", "[Permission Denied]", "[Unsupported]")), out


@pytest.mark.parametrize("command", _HOST_LOCAL)
def test_an_unattended_bound_session_runs_none_of_them(command):
    out, ran, asked = _run(command, "operator", bound="default", bypass=True)
    assert not ran and not asked, out


@pytest.mark.parametrize(
    "command",
    [
        "kubectl config set users.session.token x",
        "kubectl create configmap c -n default --from-file=k=/srv/f",
        "kubectl get pods -n default -o jsonpath-file=/srv/f",
        "kubectl kustomize /srv/d",
    ],
)
def test_the_refusal_says_why_and_where_it_can_be_done(command):
    out, _, _ = _run(command, "superadmin")
    assert out.startswith("[Protected]"), out
    assert "KubeIntellect server" in out and "approval" in out


@pytest.mark.parametrize("command", _STILL_ALLOWED)
def test_reading_the_kubeconfig_shape_and_inline_formats_still_run(command):
    out, ran, _ = _run(command, "admin")
    assert ran, out


def test_the_proxy_url_flag_is_a_connection_flag():
    """Which network path a request takes, and so who sees its bearer token, is the deployment's choice."""
    assert "--proxy-url" in kt._CONNECTION_FLAGS
