"""`-n kube-system` is refused; `-A` returned the same rows, and could mutate them.

Eleven passes hardened `run_kubectl`'s protected-namespace check, and all eleven asked the same
question: *which namespace does this command name?* A command naming **every** namespace names
none in particular, so the check never fired. Absence of a namespace was read as "nothing to
protect" rather than "everything, including the protected ones".

Measured 2026-08-20 against the real tool:

    kubectl get pods -n kube-system          refused
    kubectl get pods -A                      RAN — returned kube-system, kubeintellect, monitoring
    kubectl get events -A                    RAN
    kubectl get configmaps -A -o yaml        RAN
    kubectl delete pods -n kube-system       refused
    kubectl delete pods --all-namespaces     reached the approval prompt

The mutation half is the worse one: approved, it would have deleted pods in `kube-system`,
`monitoring` and `kubeintellect` — the namespace KubeIntellect itself runs in. It also composes
badly with the fail-open approval gate fixed the same day: before that fix, any unrecognised
reply resumed as approval.

Reads keep working — `kubectl get pods -A` is how an agent sees the shape of a cluster — and
their output is filtered instead. `-o name` and `-o jsonpath` carry no namespace to filter on
and are refused rather than passed through, the same fail-closed choice the structured
namespace filter makes on a payload it cannot parse.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import yaml

from app.core.config import settings
from app.tools.kubectl_tool import run_kubectl

TABLE = (
    "NAMESPACE      NAME                   READY   STATUS\n"
    "prod           web-1                  1/1     Running\n"
    "kube-system    kube-apiserver-cp      1/1     Running\n"
    "kubeintellect  kubeintellect-api-abc  1/1     Running\n"
    "monitoring     langfuse-web-xyz       1/1     Running\n"
)
LISTING = {"apiVersion": "v1", "kind": "List", "items": [
    {"metadata": {"name": "web-1", "namespace": "prod"}, "data": {"LOG": "info"}},
    {"metadata": {"name": "kube-root-ca", "namespace": "kube-system"}, "data": {"ca.crt": "PEM"}},
    {"metadata": {"name": "ki-config", "namespace": "kubeintellect"},
     "data": {"AZURE_OPENAI_ENDPOINT": "https://x.openai.azure.com"}},
]}
DESCRIBE = (
    "Name:         web-1\nNamespace:    prod\nStatus:       Running\n\n"
    "Name:         kube-apiserver\nNamespace:    kube-system\nStatus:       Running\n"
)
PROTECTED = ("kube-system", "kubeintellect", "monitoring", "kube-apiserver",
             "kube-root-ca", "openai.azure.com", "langfuse")


def _run(command: str, stdout: str = TABLE, role: str = "readonly"):
    with patch("subprocess.run") as run:
        proc = MagicMock()
        proc.stdout, proc.stderr, proc.returncode = stdout, "", 0
        run.return_value = proc
        try:
            out = str(run_kubectl.invoke(
                {"command": command, "config": {"configurable": {"user_role": role}}}))
        except KeyError as exc:                       # interrupt() outside a graph == HITL fired
            out = f"<HITL {exc}>"
    return out, run.called


def _leaks(out: str) -> list[str]:
    return [name for name in PROTECTED if name in out]


class TestClusterWideMutationsAreRefused:
    @pytest.mark.parametrize("command", [
        "kubectl delete pods --all-namespaces",
        "kubectl delete pods -A",
        "kubectl delete pods -A --field-selector=status.phase=Failed",
        "kubectl label pods -A env=x",
        "kubectl annotate pods --all-namespaces k=v",
        "kubectl patch pods -A -p {}",
        "kubectl delete pods --all-namespaces=true",
    ])
    @pytest.mark.parametrize("role", ["operator", "admin", "superadmin"])
    def test_no_role_may_mutate_every_namespace_at_once(self, command, role):
        out, ran = _run(command, role=role)
        assert "[Protected]" in out, f"{role} {command!r}: {out[:140]!r}"
        assert not ran, "kubectl was executed"
        assert "HITL" not in out, "offered for approval instead of refused"

    def test_the_message_says_how_to_proceed(self):
        out, _ran = _run("kubectl delete pods -A", role="admin")
        assert "-n <namespace>" in out

    def test_an_explicit_false_is_not_a_cluster_wide_request(self):
        """`--all-namespaces=false` is the default, not a request for every namespace."""
        out, ran = _run("kubectl delete pods --all-namespaces=false -n prod", role="admin")
        assert "[Protected]" not in out, out[:140]
        assert ran or "HITL" in out, "a namespaced delete should still reach the approval gate"


class TestClusterWideReadsAreFilteredNotRefused:
    def test_the_default_table_drops_protected_rows(self):
        out, ran = _run("kubectl get pods -A")
        assert ran, "reads must keep working"
        assert _leaks(out) == [], out[:200]
        assert "web-1" in out and "prod" in out
        assert out.splitlines()[0].startswith("NAMESPACE"), "header was dropped"

    @pytest.mark.parametrize("command", [
        "kubectl get pods --all-namespaces",
        "kubectl get events -A",
        "kubectl get pods -A -o wide",
        "kubectl top pods -A",
    ])
    def test_every_table_shaped_listing_is_filtered(self, command):
        out, _ran = _run(command)
        assert _leaks(out) == [], f"{command!r}: {out[:160]!r}"

    def test_json_is_filtered_and_stays_parseable(self):
        """CHANGED-2026-08-20: the whole output is parseable, not just a prefix of it.

        This used to strip a trailing `[Protected] …` line before parsing
        (`out.split("\\n[Protected]")[0]`). That workaround was the bug: the filter appended
        its notice *after* `json.dumps`, so `json.loads(out)` raised
        `JSONDecodeError: Extra data`. The notice now lives inside the document, so the
        assertion is the stronger one it was always meant to be.
        """
        out, _ran = _run("kubectl get configmaps -A -o json", json.dumps(LISTING, indent=2))
        assert _leaks(out) == []
        doc = json.loads(out)
        assert [i["metadata"]["name"] for i in doc["items"]] == ["web-1"]
        assert "withheld" in doc["withheldByPolicy"]

    def test_yaml_is_filtered_and_stays_parseable(self):
        # CHANGED-2026-08-20 — see the JSON case above; the whole document parses now.
        out, _ran = _run("kubectl get configmaps --all-namespaces -o yaml",
                         yaml.safe_dump(LISTING))
        assert _leaks(out) == []
        body = out
        assert [i["metadata"]["name"] for i in yaml.safe_load(body)["items"]] == ["web-1"]

    def test_describe_drops_whole_blocks(self):
        out, _ran = _run("kubectl describe pods -A", DESCRIBE)
        assert _leaks(out) == []
        assert "web-1" in out and "Status:       Running" in out

    def test_the_filtering_is_declared_not_silent(self):
        out, _ran = _run("kubectl get pods -A")
        assert "withheld" in out, "three rows vanished with no explanation"


class TestFormatsThatCannotBeFilteredFailClosed:
    @pytest.mark.parametrize("command", [
        "kubectl get pods -A -o name",
        "kubectl get pods --all-namespaces -o name",
        "kubectl get pods -A -o jsonpath={.items[*].metadata.name}",
    ])
    def test_they_are_refused_with_an_actionable_message(self, command):
        out, _ran = _run(command, "pod/web-1\npod/kube-apiserver-cp\n")
        assert "[Protected]" in out, out[:160]
        assert _leaks(out) == []
        assert "-n <namespace>" in out

    def test_an_unparseable_structured_payload_fails_closed(self):
        out, _ran = _run("kubectl get pods -A -o json", "{not json")
        assert "[Protected]" in out

    def test_the_same_formats_are_fine_when_namespaced(self):
        """The refusal is about `-A`, not about the format."""
        out, ran = _run("kubectl get pods -n prod -o name", "pod/web-1\n")
        assert ran and "[Protected]" not in out and "web-1" in out


class TestNoOverBlocking:
    @pytest.mark.parametrize("command", [
        "kubectl get pods -n prod",
        "kubectl get pods",
        "kubectl get nodes",
        "kubectl get pods -n prod -o json",
    ])
    def test_ordinary_commands_are_untouched(self, command):
        out, ran = _run(command, TABLE)
        assert ran, f"over-blocked {command!r}"
        assert "[Protected]" not in out

    def test_a_cluster_wide_read_with_nothing_protected_is_unchanged(self):
        plain = "NAMESPACE  NAME   READY\nprod       web-1  1/1\nstaging    api-2  1/1\n"
        out, _ran = _run("kubectl get pods -A", plain)
        assert out.strip() == plain.strip(), "an innocent listing was rewritten"

    def test_a_resource_named_like_a_flag_value_is_not_confused(self):
        out, ran = _run("kubectl get pods -n prod -l app=all-namespaces", TABLE)
        assert ran and "[Protected]" not in out


class TestOneDefinition:
    def test_the_filter_follows_the_configured_blocklist(self):
        with patch.object(settings, "KUBECTL_BLOCKED_NAMESPACES", "prod"):
            out, _ran = _run("kubectl get pods -A")
            assert "web-1" not in out, "the configured blocklist was ignored"
            assert "kube-apiserver-cp" in out, "a hardcoded blocklist survived"
