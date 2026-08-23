"""The credential and namespace guarantees are about the product, not about one of its tools.

Passes 50–58 hardened `run_kubectl`. The agent has a second tool and the API has a second route,
and neither enforced any of it. Measured 2026-08-20:

    helm list -n kube-system                        RAN
    helm get values kubeintellect -n kubeintellect  RAN   ← this release's own configuration
    helm get manifest web -n prod                   RAN   ← renders `kind: Secret` with data:
    helm get all prometheus -n monitoring           RAN
    GET /v1/namespaces                              returned every namespace, unfiltered

`docs/security.md` says infrastructure namespaces are blocked *including reads*, and that Secrets
and ServiceAccounts are shielded *for every role, regardless of namespace*. `helm get manifest`
renders the release's Secret objects with their base64 `data:` intact, which is precisely the
thing `kubectl get secret` is refused for unconditionally.

`run_helm` was already right about the direction nobody had got wrong: its verb check is an
**allowlist**, so writes and unknown subcommands fail closed. Its `_extract_verb` was `tokens[1]`
— pass 51's exact defect — and behind that allowlist it produced a *usability* bug
(`helm -n prod list` → "not a supported subcommand") rather than a bypass. That contrast is the
argument for pass 53's inversion, stated as a test.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.core.config import settings
from app.tools.helm_tool import run_helm

MANIFEST = """---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  replicas: 2
---
apiVersion: v1
kind: Secret
metadata:
  name: api-keys
data:
  KUBEINTELLECT_ADMIN_KEYS: c3VwZXJzZWNyZXQ=
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: api-sa
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: cfg
data:
  LOG_LEVEL: info
"""

LIST_TABLE = (
    "NAME\tNAMESPACE\tREVISION\tSTATUS\n"
    "web\tprod\t3\tdeployed\n"
    "prom\tmonitoring\t1\tdeployed\n"
    "certs\tcert-manager\t2\tdeployed\n"
)
LIST_JSON = json.dumps([
    {"name": "web", "namespace": "prod"},
    {"name": "prom", "namespace": "monitoring"},
])
LIST_YAML = "- name: web\n  namespace: prod\n- name: prom\n  namespace: monitoring\n"


def _helm(command: str, stdout: str = "") -> tuple[str, bool]:
    with patch("subprocess.run") as run:
        proc = MagicMock()
        proc.stdout, proc.stderr, proc.returncode = stdout, "", 0
        run.return_value = proc
        return str(run_helm.invoke({"command": command})), run.called


class TestHelmHonoursTheNamespaceBlocklist:
    @pytest.mark.parametrize("command", [
        "helm list -n kube-system",
        "helm -n kube-system list",
        "helm get values kubeintellect -n kubeintellect",
        "helm get manifest x -n monitoring",
        "helm get manifest x -nmonitoring",
        "helm get manifest x -n=monitoring",
        "helm get all x --namespace cert-manager",
        "helm get all x --namespace=cert-manager",
        "helm status ingress -n ingress-nginx",
        "helm history x -n kube-public",
    ])
    def test_it_is_refused(self, command):
        out, ran = _helm(command)
        assert "[Protected]" in out, f"{command!r}: {out[:120]!r}"
        assert not ran

    @pytest.mark.parametrize("command", [
        "helm list -n prod",
        "helm -n prod list",
        "helm get values web -n staging",
        "helm status web -nprod",
        "helm list -A",
        "helm version",
        "helm env",
    ])
    def test_ordinary_namespaces_are_untouched(self, command):
        _out, ran = _helm(command, LIST_TABLE)
        assert ran, f"over-blocked {command!r}"

    def test_the_blocklist_is_read_from_config_not_copied(self):
        """One definition, so the two tools cannot drift about which namespaces are protected.

        Asserted behaviourally: re-point the setting and helm must follow it. A literal list
        copied into helm_tool.py would pass an equality check today and rot the first time
        KUBECTL_BLOCKED_NAMESPACES is edited.
        """
        with patch.object(settings, "KUBECTL_BLOCKED_NAMESPACES", "vault-system"):
            out, ran = _helm("helm list -n vault-system")
            assert "[Protected]" in out and not ran, "helm ignored the configured blocklist"
            _out, ran = _helm("helm list -n kube-system", LIST_TABLE)
            assert ran, "helm kept a hardcoded blocklist of its own"


class TestHelmDoesNotRenderProtectedKinds:
    @pytest.mark.parametrize("command", ["helm get manifest web -n prod",
                                         "helm get all web -n prod"])
    def test_secret_and_serviceaccount_documents_are_removed(self, command):
        out, ran = _helm(command, MANIFEST)
        assert ran
        assert "c3VwZXJzZWNyZXQ=" not in out, "the Secret's data survived"
        assert "kind: Secret" not in out
        assert "kind: ServiceAccount" not in out

    def test_the_rest_of_the_manifest_survives(self):
        """Stripping documents, not lines — the tool exists to show you the release."""
        out, _ran = _helm("helm get manifest web -n prod", MANIFEST)
        assert "kind: Deployment" in out and "replicas: 2" in out
        assert "kind: ConfigMap" in out and "LOG_LEVEL: info" in out

    def test_the_removal_is_announced_rather_than_silent(self):
        out, _ran = _helm("helm get manifest web -n prod", MANIFEST)
        assert "protected kind" in out and "2 object(s)" in out

    def test_a_manifest_with_nothing_protected_is_returned_unchanged(self):
        plain = "---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cfg\n"
        out, _ran = _helm("helm get manifest web -n prod", plain)
        assert out.strip() == plain.strip()


class TestHelmListIsFilteredInEveryFormat:
    @pytest.mark.parametrize("stdout", [LIST_TABLE, LIST_JSON, LIST_YAML])
    def test_releases_in_protected_namespaces_are_dropped(self, stdout):
        out, _ran = _helm("helm list -A", stdout)
        assert "monitoring" not in out, f"leaked: {out[:160]!r}"
        assert "prod" in out and "web" in out

    def test_the_table_header_is_kept(self):
        out, _ran = _helm("helm list -A", LIST_TABLE)
        assert out.splitlines()[0].startswith("NAME")

    def test_json_stays_parseable(self):
        out, _ran = _helm("helm list -A -o json", LIST_JSON)
        assert [r["name"] for r in json.loads(out)] == ["web"]

    def test_an_unparseable_json_listing_fails_closed(self):
        out, _ran = _helm("helm list -A -o json", "{not json")
        assert "[Protected]" in out


class TestWritesStillFailClosedAndTheVerbParseIsFixed:
    @pytest.mark.parametrize("command,fragment", [
        ("helm upgrade foo ./c -n prod", "write operation"),
        ("helm -n prod upgrade foo ./c", "write operation"),
        ("helm uninstall foo -n prod", "write operation"),
        ("helm -n prod frobnicate foo", "not a supported subcommand"),
    ])
    def test_the_verb_is_found_regardless_of_flag_order(self, command, fragment):
        out, ran = _helm(command)
        assert fragment in out, f"{command!r}: {out[:120]!r}"
        assert not ran


class TestTheNamespacesEndpoint:
    def test_it_does_not_return_protected_namespaces(self):
        from app.api.v1.endpoints.namespaces import list_namespaces
        with patch("subprocess.run") as run:
            proc = MagicMock()
            proc.stdout = "prod kube-system staging monitoring kubeintellect"
            proc.stderr, proc.returncode = "", 0
            run.return_value = proc
            result = list_namespaces()
        assert result.namespaces == ["prod", "staging"]

    def test_an_empty_cluster_answer_is_still_an_empty_list(self):
        from app.api.v1.endpoints.namespaces import list_namespaces
        with patch("subprocess.run") as run:
            proc = MagicMock()
            proc.stdout, proc.stderr, proc.returncode = "  ", "", 0
            run.return_value = proc
            assert list_namespaces().namespaces == []
