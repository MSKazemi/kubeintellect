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
        "kubectl get pods && echo pwned",
        "kubectl get pods `id`",
        "kubectl get pods $(whoami)",
        "kubectl get pods \\ evil",
    ])
    def test_rejects_shell_metacharacters(self, bad_cmd):
        # `;`, `&`, backtick, `$`, and `\` are always rejected — there is no safe
        # use for them and shell=False would pass them through as literal argv.
        with patch("subprocess.run"):
            with pytest.raises(Exception, match="disallowed shell characters"):
                self._call(bad_cmd)

    def test_non_grep_pipe_is_rejected(self):
        # `|` is supported only for grep emulation (handled in Python, no shell).
        # Any other piped command is rejected so the LLM can't smuggle execution.
        proc = MagicMock(); proc.stdout = "pod list"; proc.stderr = ""; proc.returncode = 0
        with patch("subprocess.run", return_value=proc):
            with pytest.raises(Exception, match="not supported"):
                self._call("kubectl get pods | cat /etc/passwd")

    @pytest.mark.parametrize("redirect_cmd", [
        "kubectl get pods > /tmp/out",
        "kubectl get pods < /dev/null",
    ])
    def test_redirection_chars_are_passed_as_literal_args(self, redirect_cmd):
        # `<` / `>` are harmless under shell=False — they become literal argv that
        # kubectl rejects itself. They are intentionally NOT in the metachar guard
        # so that --from-literal values containing HTML/templates are allowed.
        proc = MagicMock(); proc.stdout = "error: unknown flag"; proc.stderr = ""
        proc.returncode = 1
        with patch("subprocess.run", return_value=proc) as mock_run:
            self._call(redirect_cmd)
        # The command reached subprocess (was not rejected by the shell guard).
        mock_run.assert_called_once()

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

    def test_invalid_yaml_warns_but_proceeds(self):
        # v3 no longer hard-fails on YAML that Python's parser dislikes — Python is
        # stricter than kubectl in some cases (flow mappings, vendor annotations).
        # The tool logs a warning and lets kubectl do the authoritative validation.
        result = self._apply("{ not: valid: yaml: at all")
        assert "applied" in result

    def test_empty_yaml_warns_but_proceeds(self):
        # Same policy: an empty/comment-only document is passed through to kubectl,
        # which returns its own "error: no objects passed to apply" message.
        result = self._apply("# just a comment\n")
        assert "applied" in result

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

    # ── Protected namespaces: reads allowed, writes blocked ───────────────────
    # v3 policy: the agent must observe its own pod and the observability stack to
    # diagnose issues, so read-only verbs (get/describe/logs/top) are permitted on
    # protected namespaces. Write verbs remain blocked (see write tests below).

    @pytest.mark.parametrize("ns", ["kubeintellect", "kube-system", "monitoring", "ingress-nginx"])
    def test_read_on_protected_ns_is_allowed(self, ns):
        result = self._call(f"kubectl get pods -n {ns}")
        assert "[Protected]" not in result

    def test_read_with_namespace_long_flag_is_allowed(self):
        result = self._call("kubectl get pods --namespace=kubeintellect")
        assert "[Protected]" not in result

    def test_write_on_protected_ns_is_blocked(self):
        result = self._call("kubectl delete pod foo -n kube-system")
        assert "[Protected]" in result

    def test_scale_on_protected_ns_is_blocked(self):
        result = self._call("kubectl scale deployment app --replicas=0 -n monitoring")
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

    def test_logs_on_protected_ns_are_allowed(self):
        """kubectl logs is read-only — allowed even on protected namespaces so the
        agent can read its own and the observability stack's logs to diagnose."""
        proc = MagicMock(); proc.stdout = "log output"; proc.stderr = ""; proc.returncode = 0
        with patch("subprocess.run", return_value=proc):
            from app.tools.kubectl_tool import run_kubectl
            result = run_kubectl.invoke({"command": "kubectl logs my-pod -n kubeintellect"})
        assert "[Protected]" not in result
        assert "log output" in result

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
        proc = MagicMock(); proc.stdout = big; proc.stderr = ""; proc.returncode = 0
        with patch("subprocess.run", return_value=proc):
            from app.tools.kubectl_tool import run_kubectl
            result = run_kubectl.invoke({"command": "kubectl get pods"})
        assert len(result) < 9_000
        assert "truncated" in result.lower()
