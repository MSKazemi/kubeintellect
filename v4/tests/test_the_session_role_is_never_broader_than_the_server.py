"""The ClusterRole a namespace-bound investigation acts under is confined, and never broader than the server.

ADR-007 Phase 6 moves the namespace and Secret bounds of an autonomous investigation from string parsing to the API
server: the session runs as a ServiceAccount token bound in its own namespace to `sessionIdentity.clusterRole`. That
only helps if the role itself is right, so the rendered chart is checked here:

* no Secret verb, no workload creation, no exec / attach / port-forward, no wildcard — the faked fix of 2026-11-14 was
  a substitute Job, and the review passes found Secret reads by string tricks;
* every (group, resource, verb) it grants is also granted to the server by its own `rbac.*` roles **with at least the
  same scope** — a session can be bound in any namespace, so only a cluster-wide grant of the server's covers it, and
  the read-only identity's grants are compared by the scope of its own binding. Turning identities on can never widen
  what a deployment could already do. The first version compared (group, resource, verb) only and never rendered
  `rbac.createClusterReadOnly=false`, where the server reads its own namespace only and the session role still granted
  reads in every namespace an investigation opened (independent review, 2026-11-15, finding 3);
* with writes enabled, the writes the pilots' real repairs used are there (otherwise A3 silently cannot fix anything);
* the issuer role can mint tokens for the session ServiceAccount only, and bind the session ClusterRole only.
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

_WORKLOADS = {
    ("", "pods"),
    ("apps", "deployments"),
    ("apps", "statefulsets"),
    ("apps", "daemonsets"),
    ("apps", "replicasets"),
    ("batch", "jobs"),
    ("batch", "cronjobs"),
    ("", "replicationcontrollers"),
}


def _render(*sets: str) -> list[dict]:
    args = ["helm", "template", "si-test", str(_CHART)]
    for s in sets:
        args += ["--set", s]
    proc = subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", timeout=120
    )
    assert proc.returncode == 0, proc.stderr
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _role(docs: list[dict], name: str) -> dict | None:
    return next(
        (
            d
            for d in docs
            if d["kind"] == "ClusterRole" and d["metadata"]["name"] == name
        ),
        None,
    )


def _grants(rules: list[dict]) -> set[tuple[str, str, str]]:
    return {
        (g, r, v)
        for rule in rules
        for g in rule.get("apiGroups") or []
        for r in rule.get("resources") or []
        for v in rule.get("verbs") or []
    }


def _scoped_grants(docs: list[dict], sa: str) -> set[tuple[str, str, str, str]] | None:
    """(scope, group, resource, verb) that ServiceAccount `sa` holds through the chart's bindings; scope is "*" for a
    ClusterRoleBinding, else the RoleBinding's namespace. None = cluster-admin. The issuer role is left out: it is
    what hands identities out, compared separately."""
    out: set[tuple[str, str, str, str]] = set()
    for b in docs:
        if b["kind"] not in ("ClusterRoleBinding", "RoleBinding") or b["metadata"][
            "name"
        ].endswith("-session-issuer"):
            continue
        if not any(x.get("name") == sa for x in b.get("subjects") or []):
            continue
        if b["roleRef"]["name"] == "cluster-admin":
            return None
        scope = "*" if b["kind"] == "ClusterRoleBinding" else b["metadata"]["namespace"]
        role = next(
            (
                d
                for d in docs
                if d["kind"] == b["roleRef"]["kind"]
                and d["metadata"]["name"] == b["roleRef"]["name"]
            ),
            None,
        )
        out |= {(scope, *g) for g in _grants((role or {}).get("rules") or [])}
    return out


def _not_covered(
    grants: set[tuple[str, str, str, str]], server: set[tuple[str, str, str, str]]
) -> list:
    return sorted(
        g
        for g in grants
        if ("*", *g[1:]) not in server and not (g[0] != "*" and g in server)
    )


_CONFIGS = [
    (),
    ("rbac.createClusterOps=true",),
    ("rbac.createClusterReadOnly=false",),
    ("rbac.createClusterReadOnly=false", "rbac.createClusterOps=true"),
    ("rbac.createNamespaced=false",),
    ("rbac.createClusterReadOnly=false", "rbac.createNamespaced=false"),
]


def _server_grants(docs: list[dict]) -> set[tuple[str, str, str]] | None:
    """What the server's own ServiceAccount holds through the chart's rbac.* roles (None = cluster-admin)."""
    bindings = [
        d
        for d in docs
        if d["kind"] in ("ClusterRoleBinding", "RoleBinding")
        and any(s.get("name") == "kubeintellect-sa" for s in d.get("subjects") or [])
        and not d["metadata"]["name"].endswith("-session-issuer")
    ]
    if any(b["roleRef"]["name"] == "cluster-admin" for b in bindings):
        return None
    names = {b["roleRef"]["name"] for b in bindings}
    return {
        g
        for d in docs
        if d["kind"] in ("Role", "ClusterRole") and d["metadata"]["name"] in names
        for g in _grants(d.get("rules") or [])
    }


@pytest.mark.parametrize(
    "sets", [(), ("rbac.createClusterOps=true",), ("rbac.clusterAdmin=true",)]
)
def test_the_session_role_grants_no_secret_no_workload_creation_no_exec(sets):
    role = _role(_render(*sets), "kubeintellect-session")
    assert role is not None
    grants = _grants(role["rules"])
    assert not [g for g in grants if "*" in g], "no wildcard"
    assert not [g for g in grants if g[1].startswith("secrets")], (
        "no Secret verb of any kind"
    )
    assert not [
        g
        for g in grants
        if g[1]
        in ("pods/exec", "pods/attach", "pods/portforward", "serviceaccounts/token")
    ]
    assert not [g for g in grants if (g[0], g[1]) in _WORKLOADS and g[2] == "create"], (
        "no new workloads"
    )
    assert not [
        g
        for g in grants
        if (g[0], g[1]) in _WORKLOADS - {("", "pods")} and g[2] == "delete"
    ]
    assert not [g for g in grants if g[0] == "rbac.authorization.k8s.io"], (
        "cannot touch RBAC"
    )


@pytest.mark.parametrize("sets", _CONFIGS)
def test_the_session_role_is_a_subset_of_what_the_server_holds_in_every_namespace(sets):
    docs = _render(*sets)
    server = _scoped_grants(docs, "kubeintellect-sa")
    assert server is not None
    role = _role(docs, "kubeintellect-session")
    session = {
        ("*", *g) for g in _grants((role or {}).get("rules") or [])
    }  # bound in any namespace it investigates
    assert _not_covered(session, server) == [], (
        "the session identity would widen the deployment"
    )


@pytest.mark.parametrize("sets", _CONFIGS)
def test_the_readonly_identity_is_a_subset_of_what_the_server_holds(sets):
    docs = _render(*sets)
    server = _scoped_grants(docs, "kubeintellect-sa")
    readonly = _scoped_grants(docs, "kubeintellect-readonly")
    assert server is not None and readonly is not None
    assert _not_covered(readonly, server) == [], (
        "the read-only identity would widen the deployment"
    )


def test_without_cluster_reads_the_session_role_is_not_installed_so_investigations_fail_closed():
    docs = _render("rbac.createClusterReadOnly=false")
    assert _role(docs, "kubeintellect-session") is None
    cm = next(
        d
        for d in docs
        if d["kind"] == "ConfigMap"
        and "SESSION_IDENTITY_ENABLED" in (d.get("data") or {})
    )
    assert cm["data"]["SESSION_IDENTITY_ENABLED"] == "true", (
        "still on: the server reports the missing ClusterRole instead of running as itself"
    )


def test_without_cluster_reads_the_readonly_identity_reads_the_server_namespace_only():
    docs = _render("rbac.createClusterReadOnly=false")
    readonly = _scoped_grants(docs, "kubeintellect-readonly")
    assert readonly and {scope for scope, *_ in readonly} == {"kubeintellect"}
    assert not [g for g in readonly if g[2].startswith("secrets")]


def test_without_server_writes_the_session_has_none():
    grants = _grants(_role(_render(), "kubeintellect-session")["rules"])
    assert {v for _, _, v in grants} <= {"get", "list", "watch"}


def test_with_writes_enabled_the_pilots_repairs_are_possible():
    grants = _grants(
        _role(_render("rbac.createClusterOps=true"), "kubeintellect-session")["rules"]
    )
    for need in [
        (
            "apps",
            "deployments",
            "patch",
        ),  # set image / rollout restart / apply to an existing Deployment
        (
            "",
            "configmaps",
            "create",
        ),  # the missing ConfigMap of CreateContainerConfigError
        ("", "configmaps", "patch"),
        ("", "pods", "delete"),  # a pod its controller recreates
        ("apps", "replicasets", "list"),
    ]:  # rollout status / undo read the ReplicaSets
        assert need in grants, need


def test_the_issuer_mints_only_the_two_identity_accounts_and_binds_only_the_session_role():
    issuer = _role(_render(), "kubeintellect-session-issuer")
    assert issuer is not None
    for rule in issuer["rules"]:
        resources, verbs = set(rule["resources"]), set(rule["verbs"])
        if "serviceaccounts/token" in resources or verbs & {"bind", "get", "patch"}:
            assert rule.get("resourceNames"), f"unrestricted by name: {rule}"
        if "serviceaccounts/token" in resources:
            assert rule["resourceNames"] in (
                ["kubeintellect-session"],
                ["kubeintellect-readonly"],
            )
        if "bind" in verbs:
            assert rule["resourceNames"] == ["kubeintellect-session"] and resources == {
                "clusterroles"
            }
        assert not verbs & {"*", "escalate", "impersonate", "delete", "update"}


def test_the_server_is_told_and_off_means_nothing_is_installed():
    docs = _render()
    cm = next(
        d
        for d in docs
        if d["kind"] == "ConfigMap"
        and "SESSION_IDENTITY_ENABLED" in (d.get("data") or {})
    )
    assert cm["data"]["SESSION_IDENTITY_ENABLED"] == "true"
    assert cm["data"]["SESSION_IDENTITY_CLUSTER_ROLE"] == "kubeintellect-session"
    assert cm["data"]["SESSION_IDENTITY_TOKEN_SECONDS"] == "900"
    off = _render("sessionIdentity.enabled=false")
    assert _role(off, "kubeintellect-session") is None
    assert (
        next(
            d
            for d in off
            if d["kind"] == "ConfigMap"
            and "SESSION_IDENTITY_ENABLED" in (d.get("data") or {})
        )["data"]["SESSION_IDENTITY_ENABLED"]
        == "false"
    )


def test_the_readonly_identity_is_bound_to_the_read_only_role_only():
    docs = _render()
    sa = next(
        d
        for d in docs
        if d["kind"] == "ServiceAccount"
        and d["metadata"]["name"] == "kubeintellect-readonly"
    )
    assert sa["automountServiceAccountToken"] is False
    bindings = [
        d
        for d in docs
        if d["kind"] in ("ClusterRoleBinding", "RoleBinding")
        and any(s.get("name") == "kubeintellect-readonly" for s in d["subjects"])
    ]
    assert [b["roleRef"]["name"] for b in bindings] == ["kubeintellect-cluster-ro"]
    grants = _grants(_role(docs, "kubeintellect-cluster-ro")["rules"])
    assert not [g for g in grants if g[1].startswith("secrets") or "*" in g]
    persisted_writes = {g for g in grants if g[2] not in ("get", "list", "watch")} - {
        ("authorization.k8s.io", "subjectaccessreviews", "create")
    }  # a question to the API server, nothing stored
    assert persisted_writes == set()
    issuer = _role(docs, "kubeintellect-session-issuer")
    assert {
        "resources": ["serviceaccounts/token"],
        "resourceNames": ["kubeintellect-readonly"],
        "verbs": ["create"],
        "apiGroups": [""],
    } in issuer["rules"]
    cm = next(
        d
        for d in docs
        if d["kind"] == "ConfigMap"
        and "READONLY_IDENTITY_ENABLED" in (d.get("data") or {})
    )
    assert (
        cm["data"]["READONLY_IDENTITY_ENABLED"] == "true"
        and cm["data"]["READONLY_IDENTITY_NAMESPACE"]
    )


@pytest.mark.parametrize("values", ["values-kind.yaml", "values-local.yaml"])
def test_a_cluster_admin_install_still_gets_a_read_only_readonly_identity(values):
    """The server is cluster-admin there; the readonly role must not be."""
    proc = subprocess.run(
        ["helm", "template", "si-test", str(_CHART), "-f", str(_CHART / values)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    docs = [d for d in yaml.safe_load_all(proc.stdout) if d]
    binding = next(
        d
        for d in docs
        if d["kind"] == "ClusterRoleBinding"
        and d["metadata"]["name"].endswith("-readonly-identity")
    )
    role = _role(docs, binding["roleRef"]["name"])
    assert role is not None, (
        "the read-only role is rendered for the identity even without createClusterReadOnly"
    )
    assert not [g for g in _grants(role["rules"]) if g[1].startswith("secrets")]
    assert not [
        d
        for d in docs
        if d["kind"] == "ClusterRoleBinding"
        and d["metadata"]["name"].endswith("-cluster-ro-binding")
    ], "and the server is not bound to it when it did not ask"
