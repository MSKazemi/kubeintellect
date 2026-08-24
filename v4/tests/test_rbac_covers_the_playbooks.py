"""Every resource a shipped playbook investigates must be readable by the shipped role.

Two lists that have to agree and were maintained independently: the `investigate:` steps in
`app/agent/playbooks/*.yaml`, and the rules in `deploy/helm/kubeintellect/templates/rbac.yaml`.
Nothing compared them. Measured 2026-08-24 against the rendered chart, six (apiGroup, resource)
pairs named by playbook steps were granted by no rule:

    resourcequotas                   ''                              quota_exceeded
    horizontalpodautoscalers         autoscaling                     hpa_not_scaling
    apiservices                      apiregistration.k8s.io          hpa_not_scaling
    storageclasses                   storage.k8s.io                  pvc_pending
    validating/mutatingwebhookconfigurations  admissionregistration.k8s.io  webhook_admission_rejected

Four of the 23 playbooks hit `Error from server (Forbidden)` on their own first step — during
the incident they exist for. Every signal around it was green: `helm install` succeeded,
`kubeintellect status` reported a reachable cluster, the playbook count said 23, and the
playbook file was present and valid. RBAC is a declarative match on (apiGroup, resource, verb),
so evaluating the rendered rules is exact rather than an approximation.

This test derives the requirement from the playbooks, so a playbook added tomorrow that reads a
new kind fails here rather than in production.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_V4 = Path(__file__).resolve().parents[1]
_PLAYBOOKS = _V4 / "packages" / "kubeintellect-server" / "app" / "agent" / "playbooks"
_CHART = _V4 / "deploy" / "helm" / "kubeintellect"

# kubectl shorthand → the (apiGroup, resource) an RBAC rule has to name. A kind missing here is
# reported by `test_every_kind_a_playbook_names_is_resolvable`, not silently skipped — an
# unresolvable name is exactly how a gap hides.
_KINDS: dict[str, tuple[str, str]] = {
    "po": ("", "pods"), "pod": ("", "pods"), "pods": ("", "pods"),
    "svc": ("", "services"), "service": ("", "services"), "services": ("", "services"),
    "endpoints": ("", "endpoints"), "ep": ("", "endpoints"),
    "event": ("", "events"), "events": ("", "events"),
    "node": ("", "nodes"), "nodes": ("", "nodes"), "no": ("", "nodes"),
    "pv": ("", "persistentvolumes"), "persistentvolume": ("", "persistentvolumes"),
    "pvc": ("", "persistentvolumeclaims"),
    "persistentvolumeclaim": ("", "persistentvolumeclaims"),
    "configmap": ("", "configmaps"), "cm": ("", "configmaps"),
    "secret": ("", "secrets"), "secrets": ("", "secrets"),
    "namespace": ("", "namespaces"), "ns": ("", "namespaces"),
    "resourcequota": ("", "resourcequotas"), "quota": ("", "resourcequotas"),
    "serviceaccount": ("", "serviceaccounts"),
    "deploy": ("apps", "deployments"), "deployment": ("apps", "deployments"),
    "deployments": ("apps", "deployments"),
    "replicaset": ("apps", "replicasets"), "rs": ("apps", "replicasets"),
    "daemonset": ("apps", "daemonsets"), "ds": ("apps", "daemonsets"),
    "statefulset": ("apps", "statefulsets"), "sts": ("apps", "statefulsets"),
    "job": ("batch", "jobs"), "jobs": ("batch", "jobs"),
    "cronjob": ("batch", "cronjobs"),
    "hpa": ("autoscaling", "horizontalpodautoscalers"),
    "horizontalpodautoscaler": ("autoscaling", "horizontalpodautoscalers"),
    "networkpolicy": ("networking.k8s.io", "networkpolicies"),
    "netpol": ("networking.k8s.io", "networkpolicies"),
    "ingress": ("networking.k8s.io", "ingresses"),
    "storageclass": ("storage.k8s.io", "storageclasses"),
    "sc": ("storage.k8s.io", "storageclasses"),
    "apiservice": ("apiregistration.k8s.io", "apiservices"),
    "validatingwebhookconfiguration": (
        "admissionregistration.k8s.io", "validatingwebhookconfigurations"),
    "validatingwebhookconfigurations": (
        "admissionregistration.k8s.io", "validatingwebhookconfigurations"),
    "mutatingwebhookconfiguration": (
        "admissionregistration.k8s.io", "mutatingwebhookconfigurations"),
    "mutatingwebhookconfigurations": (
        "admissionregistration.k8s.io", "mutatingwebhookconfigurations"),
}

# Named, with the reason, rather than left to look like an oversight.
_DELIBERATELY_NOT_GRANTED: dict[tuple[str, str], str] = {
    ("", "secrets"): (
        "createcontainerconfigerror.yaml tells the operator to list a Secret's *keys*, but RBAC "
        "cannot grant key-only read — `get` on a Secret returns its values. Granting it would "
        "hand every Secret in the cluster to the agent and to the LLM prompt path. Owner "
        "decision, tracked in the note as T111; the playbook step is a human instruction."
    ),
}

_CMD = re.compile(r"kubectl\s+(?:get|describe)\s+([a-zA-Z][\w.\-]*(?:,[a-zA-Z][\w.\-]*)*)")


def _named_kinds() -> dict[str, set[str]]:
    """Every kubectl noun a playbook names, mapped to the files that name it."""
    found: dict[str, set[str]] = {}
    for path in sorted(_PLAYBOOKS.rglob("*.yaml")):
        for match in _CMD.finditer(path.read_text(encoding="utf-8")):
            for token in match.group(1).split(","):
                found.setdefault(token.lower(), set()).add(path.name)
    return found


def _granted_for_read() -> set[tuple[str, str]]:
    """(apiGroup, resource) pairs the rendered chart grants `get`/`list` on."""
    proc = subprocess.run(
        ["helm", "template", "rbac-test", str(_CHART)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    granted: set[tuple[str, str]] = set()
    for doc in yaml.safe_load_all(proc.stdout):
        if not doc or doc.get("kind") not in ("Role", "ClusterRole"):
            continue
        for rule in doc.get("rules") or []:
            if not {"get", "list", "*"} & set(rule.get("verbs") or []):
                continue
            for group in rule.get("apiGroups") or []:
                for resource in rule.get("resources") or []:
                    granted.add((group, resource))
    return granted


pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="needs a real helm to render the chart"
)


class TestTheShippedRoleCanRunTheShippedPlaybooks:
    def test_no_playbook_reads_a_resource_the_chart_does_not_grant(self):
        granted = _granted_for_read()
        missing = []
        for kind, files in sorted(_named_kinds().items()):
            pair = _KINDS.get(kind)
            if pair is None or pair in _DELIBERATELY_NOT_GRANTED:
                continue
            if pair not in granted and ("*", "*") not in granted:
                missing.append(f"{pair[1]} (apiGroup={pair[0]!r}) ← {', '.join(sorted(files))}")
        assert not missing, (
            "Playbook steps read resources the shipped role cannot: they return "
            "`Error from server (Forbidden)` during the incident the playbook exists for.\n  "
            + "\n  ".join(missing)
        )

    def test_every_kind_a_playbook_names_is_resolvable(self):
        """An unmapped noun is skipped by the test above — which is how a gap would hide."""
        unmapped = sorted(k for k in _named_kinds() if k not in _KINDS)
        assert not unmapped, (
            "These kubectl nouns appear in playbooks but are not in _KINDS, so the coverage "
            f"check silently ignores them: {unmapped}"
        )

    @pytest.mark.parametrize("pair,reason", sorted(_DELIBERATELY_NOT_GRANTED.items()))
    def test_the_exceptions_are_still_exceptions(self, pair, reason):
        """If a future edit grants one of these, that is a security change and must be noticed."""
        assert pair not in _granted_for_read(), (
            f"{pair[1]} is now granted, but it is listed as deliberately withheld: {reason}"
        )


class TestTheseTestsWouldNoticeTheDefect:
    def test_the_playbooks_really_were_parsed(self):
        kinds = _named_kinds()
        assert len(kinds) >= 15, f"only {len(kinds)} kubectl nouns found across the playbooks"
        assert "hpa" in kinds and "storageclass" in kinds

    def test_the_chart_really_was_rendered(self):
        granted = _granted_for_read()
        assert len(granted) >= 20, f"only {len(granted)} granted pairs parsed from the chart"
        assert ("", "pods") in granted

    def test_a_playbook_naming_an_ungranted_kind_is_caught(self, tmp_path, monkeypatch):
        """Red-green against a new input: plant one and prove the check fails."""
        monkeypatch.setitem(_KINDS, "widget", ("acme.io", "widgets"))
        monkeypatch.setattr(
            f"{__name__}._named_kinds", lambda: {"widget": {"planted.yaml"}}
        )
        with pytest.raises(AssertionError, match="widgets"):
            TestTheShippedRoleCanRunTheShippedPlaybooks(
            ).test_no_playbook_reads_a_resource_the_chart_does_not_grant()
