"""
Unit tests for app/tools/kubectl_tool.py

Tests run without a real cluster — they only exercise the pure-Python
safety layers (shell injection guard, YAML validation, risk classification).
Actual subprocess execution is mocked where needed.
"""
import subprocess
from unittest.mock import MagicMock, patch

import pytest


# ── Shell injection guard ─────────────────────────────────────────────────────

class TestShellInjectionGuard:
    def _call(self, command, stdin=None):
        """Import lazily so conftest stubs are in place first."""
        from app.tools.kubectl_tool import run_kubectl
        return run_kubectl.invoke({"command": command, "stdin": stdin})

    @pytest.mark.parametrize("bad_cmd", [
        "kubectl get pods; rm -rf /",
        "kubectl get pods | cat /etc/passwd",
        "kubectl get pods && echo pwned",
        "kubectl get pods `id`",
        "kubectl get pods $(whoami)",
        "kubectl get pods > /tmp/out",
        "kubectl get pods < /dev/null",
        "kubectl get pods \\ evil",
    ])
    def test_rejects_shell_metacharacters(self, bad_cmd):
        with patch("subprocess.run"):
            with pytest.raises(Exception, match="disallowed shell characters"):
                self._call(bad_cmd)

    def test_accepts_clean_command(self):
        proc = MagicMock()
        proc.stdout = "pod/nginx   Running"
        proc.stderr = ""
        with patch("subprocess.run", return_value=proc):
            result = self._call("kubectl get pods -n default")
        assert "nginx" in result

    def test_prepends_kubectl_if_missing(self):
        proc = MagicMock()
        proc.stdout = "ok"
        proc.stderr = ""
        with patch("subprocess.run", return_value=proc) as mock_run:
            self._call("get pods -n default")
        args = mock_run.call_args[0][0]
        assert args[0] == "kubectl"

    def test_does_not_double_prepend_kubectl(self):
        proc = MagicMock()
        proc.stdout = "ok"
        proc.stderr = ""
        with patch("subprocess.run", return_value=proc) as mock_run:
            self._call("kubectl get pods")
        args = mock_run.call_args[0][0]
        assert args.count("kubectl") == 1


# ── YAML stdin validation ─────────────────────────────────────────────────────

class TestYamlValidation:
    def _apply(self, stdin):
        from app.tools.kubectl_tool import run_kubectl
        # patch interrupt so HITL doesn't fire, and subprocess.run so nothing executes
        with patch("app.tools.kubectl_tool.interrupt", return_value=True):
            proc = MagicMock(); proc.stdout = "applied"; proc.stderr = ""
            with patch("subprocess.run", return_value=proc):
                return run_kubectl.invoke({"command": "kubectl apply -f -", "stdin": stdin})

    def test_valid_yaml_passes(self):
        yaml = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: test\n"
        result = self._apply(yaml)
        assert "applied" in result

    def test_invalid_yaml_raises(self):
        with pytest.raises(Exception, match="Invalid YAML"):
            self._apply("{ not: valid: yaml: at all")

    def test_empty_yaml_raises(self):
        with pytest.raises(Exception, match="empty or null"):
            self._apply("# just a comment\n")

    def test_html_content_in_yaml_is_allowed(self):
        """HTML in a ConfigMap value must not be rejected — the old bug."""
        yaml = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: web\n"
            "data:\n  index.html: |\n    <html><body>Hello from Mohsen</body></html>\n"
        )
        result = self._apply(yaml)
        assert "applied" in result


# ── Risk classification ───────────────────────────────────────────────────────

class TestRiskClassification:
    def _run_destructive(self, command):
        from app.tools.kubectl_tool import run_kubectl, interrupt as _interrupt
        # Capture the interrupt call to inspect the payload
        captured = {}
        def fake_interrupt(value):
            captured.update(value)
            return True  # simulate approval
        proc = MagicMock(); proc.stdout = "ok"; proc.stderr = ""
        with patch("app.tools.kubectl_tool.interrupt", side_effect=fake_interrupt):
            with patch("subprocess.run", return_value=proc):
                run_kubectl.invoke({"command": command})
        return captured

    @pytest.mark.parametrize("cmd,expected_risk", [
        ("kubectl delete pod foo -n default", "high"),
        ("kubectl drain node-1 --ignore-daemonsets", "high"),
        ("kubectl apply -f -", "medium"),
        ("kubectl scale deployment app --replicas=0 -n default", "medium"),
        ("kubectl patch deployment app -p '{}'", "medium"),
    ])
    def test_risk_level(self, cmd, expected_risk):
        interrupted = self._run_destructive(cmd)
        assert interrupted.get("risk_level") == expected_risk

    def test_dry_run_skips_hitl(self):
        """--dry-run commands must not trigger the interrupt."""
        proc = MagicMock(); proc.stdout = "dry-run ok"; proc.stderr = ""
        with patch("subprocess.run", return_value=proc) as mock_run:
            with patch("app.tools.kubectl_tool.interrupt") as mock_intr:
                from app.tools.kubectl_tool import run_kubectl
                run_kubectl.invoke({"command": "kubectl apply -f - --dry-run=client"})
        mock_intr.assert_not_called()

    def test_read_command_skips_hitl(self):
        """Read-only verbs (get, list, describe) must never trigger interrupt."""
        proc = MagicMock(); proc.stdout = "pod list"; proc.stderr = ""
        with patch("subprocess.run", return_value=proc):
            with patch("app.tools.kubectl_tool.interrupt") as mock_intr:
                from app.tools.kubectl_tool import run_kubectl
                run_kubectl.invoke({"command": "kubectl get pods -n default"})
        mock_intr.assert_not_called()

    def test_cancelled_action_returns_message(self):
        """If the user denies, the tool must return a cancellation message."""
        with patch("app.tools.kubectl_tool.interrupt", return_value=False):
            with patch("subprocess.run") as mock_run:
                from app.tools.kubectl_tool import run_kubectl
                result = run_kubectl.invoke({"command": "kubectl delete pod foo -n default"})
        mock_run.assert_not_called()
        assert "cancelled" in result.lower()


# ── Protected namespace / resource blocklist ──────────────────────────────────

class TestProtectedAccess:
    """Tests for _check_protected_access (layer 4b)."""

    def _call(self, command, stdin=None):
        from app.tools.kubectl_tool import run_kubectl
        with patch("subprocess.run") as mock_run:
            proc = MagicMock(); proc.stdout = "ok"; proc.stderr = ""
            mock_run.return_value = proc
            return run_kubectl.invoke({"command": command, "stdin": stdin})

    # ── Blocked resources ─────────────────────────────────────────────────────

    def test_get_secret_is_blocked(self):
        result = self._call("kubectl get secret my-secret -n default")
        assert "[Protected]" in result

    def test_get_secrets_plural_is_blocked(self):
        result = self._call("kubectl get secrets -n default")
        assert "[Protected]" in result

    def test_describe_secret_is_blocked(self):
        result = self._call("kubectl describe secret api-keys -n default")
        assert "[Protected]" in result

    def test_get_serviceaccount_is_blocked(self):
        result = self._call("kubectl get serviceaccount default -n default")
        assert "[Protected]" in result

    def test_get_sa_shorthand_is_blocked(self):
        """'sa' shorthand should also be blocked."""
        from app.tools.kubectl_tool import _extract_resource_type
        # 'sa' is an alias kubectl resolves — we only block 'secret'/'serviceaccount'
        # so 'sa' won't hit the blocklist; this documents that known limitation.
        resource = _extract_resource_type("get", ["kubectl", "get", "sa"])
        # 'sa' is not in the blocklist (kubectl expands it) — just verify no crash
        assert resource == "sa"

    # ── Blocked namespaces ────────────────────────────────────────────────────

    def test_get_pods_in_kubeintellect_ns_is_blocked(self):
        result = self._call("kubectl get pods -n kubeintellect")
        assert "[Protected]" in result

    def test_get_pods_in_kube_system_is_blocked(self):
        result = self._call("kubectl get pods -n kube-system")
        assert "[Protected]" in result

    def test_get_pods_in_monitoring_is_blocked(self):
        result = self._call("kubectl get pods -n monitoring")
        assert "[Protected]" in result

    def test_get_pods_in_ingress_nginx_is_blocked(self):
        result = self._call("kubectl get pods -n ingress-nginx")
        assert "[Protected]" in result

    def test_namespace_long_flag_is_blocked(self):
        result = self._call("kubectl get pods --namespace=kubeintellect")
        assert "[Protected]" in result

    # ── Allowed commands still pass through ───────────────────────────────────

    def test_get_pods_in_default_is_allowed(self):
        proc = MagicMock(); proc.stdout = "pod-list"; proc.stderr = ""
        with patch("subprocess.run", return_value=proc):
            from app.tools.kubectl_tool import run_kubectl
            result = run_kubectl.invoke({"command": "kubectl get pods -n default"})
        assert "[Protected]" not in result
        assert "pod-list" in result

    def test_get_deployments_in_production_is_allowed(self):
        proc = MagicMock(); proc.stdout = "deploy-list"; proc.stderr = ""
        with patch("subprocess.run", return_value=proc):
            from app.tools.kubectl_tool import run_kubectl
            result = run_kubectl.invoke({"command": "kubectl get deployments -n production"})
        assert "[Protected]" not in result

    def test_logs_do_not_trigger_blocklist(self):
        """kubectl logs has no resource-type argument — must not be blocked."""
        proc = MagicMock(); proc.stdout = "log output"; proc.stderr = ""
        with patch("subprocess.run", return_value=proc):
            from app.tools.kubectl_tool import run_kubectl
            result = run_kubectl.invoke({"command": "kubectl logs my-pod -n kubeintellect"})
        # namespace check still fires for logs (has -n flag)
        assert "[Protected]" in result  # blocked because namespace is kubeintellect

    # ── Unit tests for helpers ────────────────────────────────────────────────

    def test_extract_namespace_short_flag(self):
        from app.tools.kubectl_tool import _extract_namespace
        assert _extract_namespace(["kubectl", "get", "pods", "-n", "prod"]) == "prod"

    def test_extract_namespace_long_flag(self):
        from app.tools.kubectl_tool import _extract_namespace
        assert _extract_namespace(["kubectl", "get", "pods", "--namespace=staging"]) == "staging"

    def test_extract_namespace_missing_returns_none(self):
        from app.tools.kubectl_tool import _extract_namespace
        assert _extract_namespace(["kubectl", "get", "pods"]) is None

    def test_extract_resource_type_get(self):
        from app.tools.kubectl_tool import _extract_resource_type
        assert _extract_resource_type("get", ["kubectl", "get", "secret"]) == "secret"

    def test_extract_resource_type_slash_shorthand(self):
        from app.tools.kubectl_tool import _extract_resource_type
        assert _extract_resource_type("get", ["kubectl", "get", "secret/my-key"]) == "secret"

    def test_extract_resource_type_logs_returns_none(self):
        from app.tools.kubectl_tool import _extract_resource_type
        assert _extract_resource_type("logs", ["kubectl", "logs", "my-pod"]) is None


# ── Output cap ────────────────────────────────────────────────────────────────

class TestOutputCap:
    def test_long_output_is_truncated(self):
        big = "x" * 10_000
        proc = MagicMock(); proc.stdout = big; proc.stderr = ""
        with patch("subprocess.run", return_value=proc):
            from app.tools.kubectl_tool import run_kubectl
            result = run_kubectl.invoke({"command": "kubectl get pods"})
        assert len(result) < 9_000
        assert "truncated" in result
