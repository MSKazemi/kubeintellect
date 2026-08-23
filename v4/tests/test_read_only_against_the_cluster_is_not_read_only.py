"""Two rows in the verb table said "read" about things that write, or that read everything.

Pass 102's lens: when every gate reads one parse, the parse's lookup tables are gates too — and
a table is data nobody tests. `_READ_ONLY_VERBS` is the strongest of them, because the verb logic
was deliberately inverted to an allowlist: *a verb is a write unless it is on this list*. A verb
wrongly **on** it is therefore not a missing rule, it is an open door.

**`auth`.** It was listed because `kubectl auth can-i` and `auth whoami` ask questions. But
`kubectl auth reconcile` **writes** — it creates and updates Roles, RoleBindings, ClusterRoles
and ClusterRoleBindings from a manifest. Measured 2026-08-20 with a `readonly` API key and a
stubbed kubectl::

    kubectl create -f -           (ClusterRoleBinding -> cluster-admin)   [Permission Denied]
    kubectl auth reconcile -f -   (the identical manifest)                RAN, no prompt

The role that is meant to hold no privilege could grant itself every privilege, from the same
manifest the write verb refuses. `auth` moves to `_READ_ONLY_SUBCOMMANDS`, the mechanism this
module already had for `rollout`, `config` and `certificate`, so `can-i`/`whoami` still read and
everything else — including a `reconcile` subcommand added by a future kubectl — is a write.

**`cluster-info dump`.** Read-only against the cluster is not the same as read-only against
*what may be read* — the distinction `run_helm` already makes about `helm get manifest`.
`kubectl cluster-info dump` walks every namespace and prints pod specs, events and container
logs, so it returns the contents of exactly the namespaces the blocklist withholds. No filter
reaches it: the verb names no resource type, so `_extract_resource_type` is `None` and both
namespace filters pass it through untouched. Measured 2026-08-20, a `readonly` key ran
`kubectl cluster-info dump --all-namespaces` unfiltered. It is a concatenated dump with no
per-object shape to filter, so it is refused — the same rule `_unfilterable_format_message`
applies to `-o custom-columns`. Bare `cluster-info` still works.
"""
from __future__ import annotations

import shlex
from unittest.mock import MagicMock, patch

import pytest

from app.tools import kubectl_tool as kt
from app.tools.kubectl_tool import run_kubectl

_RBAC = """apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: ki-escalate
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-admin
subjects:
- kind: ServiceAccount
  name: kubeintellect
  namespace: default
"""


def _run(command: str, role: str, stdin: str | None = None) -> tuple[str, bool, bool]:
    """(output, kubectl-ran, hit-the-HITL-interrupt)."""
    with patch("subprocess.run") as run:
        proc = MagicMock()
        proc.stdout, proc.stderr, proc.returncode = "reconciled\n", "", 0
        run.return_value = proc
        try:
            out = str(run_kubectl.invoke({"command": command, "stdin": stdin},
                                         config={"configurable": {"user_role": role,
                                                                  "thread_id": "t"}}))
            return out, run.called, False
        except KeyError as exc:
            if "__pregel_scratchpad" not in str(exc):
                raise
            return "", run.called, True


def _is_write(command: str) -> bool:
    args = shlex.split(command)
    return kt._is_write_verb(kt._extract_verb(args), args)


class TestAuthIsOnlyReadOnlyInPart:

    @pytest.mark.parametrize("command", [
        "kubectl auth can-i get pods",
        "kubectl auth can-i create deployments -n prod",
        "kubectl auth whoami",
        "kubectl -n prod auth can-i get pods",
    ])
    def test_the_questions_are_still_reads(self, command):
        assert _is_write(command) is False

    @pytest.mark.parametrize("command", [
        "kubectl auth reconcile -f -",
        "kubectl -n prod auth reconcile -f -",
        "kubectl auth reconcile --dry-run=none -f -",
    ])
    def test_reconcile_is_a_write(self, command):
        assert _is_write(command) is True

    def test_a_subcommand_kubectl_has_not_shipped_yet_is_a_write(self):
        """The point of an allowlist: what nobody enumerated arrives gated, not pre-approved."""
        assert _is_write("kubectl auth grant-everything") is True

    def test_a_readonly_key_cannot_reconcile_rbac(self):
        out, ran, hitl = _run("kubectl auth reconcile -f -", "readonly", _RBAC)
        assert "Permission Denied" in out and not ran and not hitl

    def test_it_is_refused_for_the_same_reason_create_is(self):
        """The identical manifest through the write verb — the two answers must match."""
        reconcile, _, _ = _run("kubectl auth reconcile -f -", "readonly", _RBAC)
        create, _, _ = _run("kubectl create -f -", "readonly", _RBAC)
        assert "Permission Denied" in reconcile and "Permission Denied" in create

    def test_a_readonly_key_can_still_ask_can_i(self):
        out, ran, _ = _run("kubectl auth can-i get pods", "readonly")
        assert ran and "Permission Denied" not in out

    def test_an_admin_reconcile_is_gated_not_silent(self):
        _, ran, hitl = _run("kubectl auth reconcile -f -", "admin", _RBAC)
        assert hitl and not ran

    def test_the_verb_left_the_read_only_set(self):
        assert "auth" not in kt._READ_ONLY_VERBS
        assert kt._READ_ONLY_SUBCOMMANDS["auth"] == {"can-i", "whoami"}


class TestClusterInfoDumpIsRefused:

    @pytest.mark.parametrize("command", [
        "kubectl cluster-info dump",
        "kubectl cluster-info dump --all-namespaces",
        "kubectl cluster-info dump -A -o json",
        "kubectl -n prod cluster-info dump",
        "kubectl cluster-info dump --output-directory=/tmp/x",
    ])
    @pytest.mark.parametrize("role", ["readonly", "operator", "admin", "superadmin"])
    def test_no_role_can_run_it(self, command, role):
        out, ran, hitl = _run(command, role)
        assert "[Protected]" in out, f"{command!r} as {role}: {out[:120]!r}"
        assert not ran and not hitl

    def test_the_refusal_says_what_to_do_instead(self):
        out, _, _ = _run("kubectl cluster-info dump", "admin")
        assert "-n <namespace>" in out

    @pytest.mark.parametrize("command", [
        "kubectl cluster-info",
        "kubectl -n prod cluster-info",
        "kubectl cluster-info --context-does-not-exist=x",
    ])
    def test_bare_cluster_info_still_works(self, command):
        out, ran, _ = _run(command, "readonly")
        assert ran and "[Protected]" not in out

    def test_it_is_still_classified_as_a_read(self):
        """Refusing it is not the same as calling it a write — the verb still reads."""
        assert _is_write("kubectl cluster-info dump") is False

    def test_the_table_names_only_this(self):
        assert kt._REJECTED_SUBCOMMANDS == {"cluster-info": {"dump"}}


class TestTheRestOfTheVerbTableStillHolds:

    @pytest.mark.parametrize("command", [
        "kubectl get pods -n prod",
        "kubectl describe pod api-0 -n prod",
        "kubectl logs api-0 -n prod",
        "kubectl top pods -n prod",
        "kubectl explain pod",
        "kubectl events -n prod",
        "kubectl version",
        "kubectl api-resources",
        "kubectl config view",
        "kubectl rollout status deploy/api -n prod",
    ])
    def test_the_reads_are_still_reads(self, command):
        assert _is_write(command) is False

    @pytest.mark.parametrize("command", [
        "kubectl delete pod api-0 -n prod",
        "kubectl rollout restart deploy/api -n prod",
        "kubectl certificate approve my-csr",
        "kubectl config set-credentials admin --token=x",
        "kubectl label pod api-0 x=y -n prod",
    ])
    def test_the_writes_are_still_writes(self, command):
        assert _is_write(command) is True

    def test_every_subcommand_aware_verb_left_the_flat_set(self):
        """A verb in both tables would read as a blanket read before the subcommand is looked at."""
        assert kt._READ_ONLY_VERBS & set(kt._READ_ONLY_SUBCOMMANDS) == set()
