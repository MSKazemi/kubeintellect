"""
Unit tests for app/tools/kubectl_tool.py

Tests run without a real cluster — they only exercise the pure-Python
safety layers (shell injection guard, YAML validation, risk classification).
Actual subprocess execution is mocked where needed.
"""
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

    def test_accepts_jsonpath_with_backslash_separators(self):
        # Backslashes are required for jsonpath {"\n"} separators and are safe
        # because the tool runs with shell=False.
        proc = MagicMock()
        proc.stdout = "nginx\nredis\n"
        proc.stderr = ""
        with patch("subprocess.run", return_value=proc):
            result = self._call(
                r'kubectl get pods -A -o jsonpath={range .items[*]}{.metadata.name}{"\n"}{end}'
            )
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
        from app.tools.kubectl_tool import run_kubectl
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


# ── Always-confirm gate (overrides hitl_bypass) ───────────────────────────────

class TestAlwaysConfirm:
    """Some actions ALWAYS require user confirmation, even when hitl_bypass=True.

    These are cascading-blast operations: namespace/pv/crd deletion, drain,
    and live workload mutations via `set image|resources`. Regression tests
    for scenarios 09-multi-step (delete namespace) and 14-rollout-stuck
    (set image) which slipped through HITL on superadmin auto_approve.
    """

    def _invoke(self, command, hitl_bypass=True, user_role="superadmin"):
        from app.tools.kubectl_tool import run_kubectl
        captured = {}

        def fake_interrupt(value):
            captured.update(value)
            return True

        proc = MagicMock(); proc.stdout = "ok"; proc.stderr = ""
        cfg = {"configurable": {"user_role": user_role, "hitl_bypass": hitl_bypass}}
        with patch("app.tools.kubectl_tool.interrupt", side_effect=fake_interrupt) as mock_intr:
            with patch("subprocess.run", return_value=proc):
                run_kubectl.invoke({"command": command}, config=cfg)
        return captured, mock_intr

    @pytest.mark.parametrize("cmd", [
        "kubectl delete namespace scenario-test",
        "kubectl delete ns scenario-test",
        "kubectl delete namespaces scenario-test",
        "kubectl delete pv my-volume",
        "kubectl delete persistentvolume my-volume",
        "kubectl delete crd certificates.cert-manager.io",
        "kubectl drain kind-worker --ignore-daemonsets",
        "kubectl set image deployment/myapp container=image:v2 -n default",
        "kubectl set resources deployment/myapp -c=app --limits=cpu=200m -n default",
    ])
    def test_always_confirm_overrides_bypass(self, cmd):
        captured, mock_intr = self._invoke(cmd, hitl_bypass=True)
        mock_intr.assert_called_once()
        assert captured.get("always_confirm") is True
        assert captured.get("risk_level") == "high"

    def test_pod_delete_does_not_override_bypass(self):
        """`delete pod` is destructive but not catastrophic — bypass should still work."""
        _, mock_intr = self._invoke("kubectl delete pod foo -n default", hitl_bypass=True)
        mock_intr.assert_not_called()

    def test_apply_does_not_override_bypass(self):
        """Plain `apply` is medium-risk — bypass applies normally."""
        _, mock_intr = self._invoke("kubectl apply -f -", hitl_bypass=True)
        mock_intr.assert_not_called()

    def test_get_pods_never_triggers(self):
        """Read-only verbs never trip HITL regardless of bypass state."""
        _, mock_intr = self._invoke("kubectl get pods -n default", hitl_bypass=False)
        mock_intr.assert_not_called()

    def test_set_image_blocks_without_bypass_too(self):
        """Regression: `set` was missing from DESTRUCTIVE_VERBS entirely.
        Now it must trip HITL even on a normal (non-bypass) session."""
        captured, mock_intr = self._invoke(
            "kubectl set image deployment/myapp c=img:v2 -n default",
            hitl_bypass=False,
        )
        mock_intr.assert_called_once()
        assert captured.get("risk_level") == "high"

    def test_always_confirm_dry_run_still_skips(self):
        """--dry-run continues to skip HITL even for always-confirm actions."""
        _, mock_intr = self._invoke(
            "kubectl delete namespace scenario-test --dry-run=client",
            hitl_bypass=True,
        )
        mock_intr.assert_not_called()

    @pytest.mark.parametrize("role", ["admin", "superadmin"])
    def test_always_confirm_fires_for_admin_roles(self, role):
        """admin and superadmin both trip the always-confirm gate on bypass."""
        _, mock_intr = self._invoke(
            "kubectl delete namespace scenario-test",
            hitl_bypass=True,
            user_role=role,
        )
        mock_intr.assert_called_once()

    def test_set_image_always_confirms_for_operator(self):
        """`set` is medium-risk so operator can run it — but `set image`
        is always-confirm, so HITL still fires under bypass."""
        _, mock_intr = self._invoke(
            "kubectl set image deployment/myapp c=img:v2 -n default",
            hitl_bypass=True,
            user_role="operator",
        )
        mock_intr.assert_called_once()

    def test_readonly_blocked_before_always_confirm(self):
        """readonly is rejected at the role layer; HITL is never invoked."""
        from app.tools.kubectl_tool import run_kubectl
        proc = MagicMock(); proc.stdout = ""; proc.stderr = ""
        cfg = {"configurable": {"user_role": "readonly", "hitl_bypass": True}}
        with patch("app.tools.kubectl_tool.interrupt") as mock_intr:
            with patch("subprocess.run", return_value=proc):
                result = run_kubectl.invoke(
                    {"command": "kubectl delete namespace scenario-test"}, config=cfg
                )
        mock_intr.assert_not_called()
        assert "Permission Denied" in result

    def test_operator_blocked_on_delete_namespace(self):
        """operator can't run high-risk verbs at all (delete is _HIGH_RISK)."""
        from app.tools.kubectl_tool import run_kubectl
        proc = MagicMock(); proc.stdout = ""; proc.stderr = ""
        cfg = {"configurable": {"user_role": "operator", "hitl_bypass": True}}
        with patch("app.tools.kubectl_tool.interrupt") as mock_intr:
            with patch("subprocess.run", return_value=proc):
                result = run_kubectl.invoke(
                    {"command": "kubectl delete namespace scenario-test"}, config=cfg
                )
        mock_intr.assert_not_called()
        assert "Permission Denied" in result


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
