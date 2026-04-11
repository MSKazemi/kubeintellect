# tests/test_v2_phase1_tools.py
"""
Tests for Phase 1 v2 tool additions:

security_tools: secret_exists, image_pull_secret_check, network_policy_audit
rbac_tools    : rbac_who_can, rbac_check

Coverage:
  - All tools return valid JSON strings
  - output["status"] is always present
  - Happy path returns status="success"
  - 404 / not-found path returns status="error" with appropriate error_type
  - secret_exists returns key names only (never values)
  - image_pull_secret_check detects wrong type and missing secret
  - network_policy_audit matches policies by label selector
  - rbac_who_can walks ClusterRoleBindings and RoleBindings
  - rbac_check calls SubjectAccessReview and reflects allowed/denied
"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kubernetes.client.exceptions import ApiException


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse(result) -> dict:
    """Tools return JSON strings; _handle_k8s_exceptions returns dict for propagated errors."""
    if isinstance(result, dict):
        return result
    return json.loads(result)


def _make_api_exception(status: int = 404) -> ApiException:
    exc = ApiException(status=status, reason="Not Found")
    exc.body = json.dumps({"message": f"not found ({status})"})
    return exc


# ─────────────────────────────────────────────────────────────────────────────
# secret_exists
# ─────────────────────────────────────────────────────────────────────────────

class TestSecretExists(unittest.TestCase):

    def _make_secret(self, name="my-secret", secret_type="Opaque", keys=("key1", "key2")):
        data = {k: "dmFsdWU=" for k in keys}  # base64 values — tool must NOT return these
        meta = SimpleNamespace(
            name=name,
            namespace="default",
            creation_timestamp=None,
        )
        return SimpleNamespace(metadata=meta, type=secret_type, data=data)

    def test_happy_path_secret_exists(self):
        mock_core = MagicMock()
        mock_core.read_namespaced_secret.return_value = self._make_secret()
        with patch("app.agents.tools.tools_lib.security_tools.get_core_v1_api", return_value=mock_core):
            from app.agents.tools.tools_lib.security_tools import secret_exists
            result = _parse(secret_exists(namespace="default", secret_name="my-secret"))
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["exists"])
        self.assertEqual(result["secret_type"], "Opaque")
        self.assertIn("key1", result["key_names"])
        self.assertIn("key2", result["key_names"])

    def test_key_names_only_no_values(self):
        """Values must never appear in the output — only key names."""
        mock_core = MagicMock()
        mock_core.read_namespaced_secret.return_value = self._make_secret(keys=("password",))
        with patch("app.agents.tools.tools_lib.security_tools.get_core_v1_api", return_value=mock_core):
            from app.agents.tools.tools_lib.security_tools import secret_exists
            raw = secret_exists(namespace="default", secret_name="my-secret")
        # Base64 sentinel value must never appear
        self.assertNotIn("dmFsdWU=", raw)
        result = _parse(raw)
        self.assertEqual(result["key_names"], ["password"])

    def test_secret_not_found(self):
        mock_core = MagicMock()
        mock_core.read_namespaced_secret.side_effect = _make_api_exception(404)
        with patch("app.agents.tools.tools_lib.security_tools.get_core_v1_api", return_value=mock_core):
            from app.agents.tools.tools_lib.security_tools import secret_exists
            result = _parse(secret_exists(namespace="default", secret_name="missing"))
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["exists"])

    def test_api_error_propagated(self):
        mock_core = MagicMock()
        mock_core.read_namespaced_secret.side_effect = _make_api_exception(403)
        with patch("app.agents.tools.tools_lib.security_tools.get_core_v1_api", return_value=mock_core):
            from app.agents.tools.tools_lib.security_tools import secret_exists
            result = _parse(secret_exists(namespace="default", secret_name="my-secret"))
        self.assertEqual(result["status"], "error")


# ─────────────────────────────────────────────────────────────────────────────
# image_pull_secret_check
# ─────────────────────────────────────────────────────────────────────────────

class TestImagePullSecretCheck(unittest.TestCase):

    def _make_deployment(self, pull_secret_names):
        pull_secrets = [SimpleNamespace(name=n) for n in pull_secret_names]
        spec = SimpleNamespace(image_pull_secrets=pull_secrets)
        template_spec = SimpleNamespace(spec=spec)
        dep_spec = SimpleNamespace(template=template_spec)
        meta = SimpleNamespace(name="myapp", namespace="default")
        return SimpleNamespace(metadata=meta, spec=dep_spec)

    def _make_secret(self, secret_type="kubernetes.io/dockerconfigjson"):
        meta = SimpleNamespace(name="regcred", namespace="default", creation_timestamp=None)
        return SimpleNamespace(metadata=meta, type=secret_type, data={".dockerconfigjson": "x"})

    def test_happy_path_valid_secret(self):
        dep = self._make_deployment(["regcred"])
        secret = self._make_secret("kubernetes.io/dockerconfigjson")
        mock_apps = MagicMock()
        mock_apps.read_namespaced_deployment.return_value = dep
        mock_core = MagicMock()
        mock_core.read_namespaced_secret.return_value = secret
        with patch("app.agents.tools.tools_lib.security_tools.get_apps_v1_api", return_value=mock_apps), \
             patch("app.agents.tools.tools_lib.security_tools.get_core_v1_api", return_value=mock_core):
            from app.agents.tools.tools_lib.security_tools import image_pull_secret_check
            result = _parse(image_pull_secret_check(namespace="default", deployment_name="myapp"))
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["all_valid"])
        self.assertEqual(len(result["findings"]), 1)
        self.assertTrue(result["findings"][0]["is_correct_type"])

    def test_wrong_secret_type(self):
        dep = self._make_deployment(["regcred"])
        secret = self._make_secret("Opaque")  # wrong type
        mock_apps = MagicMock()
        mock_apps.read_namespaced_deployment.return_value = dep
        mock_core = MagicMock()
        mock_core.read_namespaced_secret.return_value = secret
        with patch("app.agents.tools.tools_lib.security_tools.get_apps_v1_api", return_value=mock_apps), \
             patch("app.agents.tools.tools_lib.security_tools.get_core_v1_api", return_value=mock_core):
            from app.agents.tools.tools_lib.security_tools import image_pull_secret_check
            result = _parse(image_pull_secret_check(namespace="default", deployment_name="myapp"))
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["all_valid"])
        self.assertFalse(result["findings"][0]["is_correct_type"])
        self.assertIn("Opaque", result["findings"][0]["issue"])

    def test_secret_missing(self):
        dep = self._make_deployment(["regcred"])
        mock_apps = MagicMock()
        mock_apps.read_namespaced_deployment.return_value = dep
        mock_core = MagicMock()
        mock_core.read_namespaced_secret.side_effect = _make_api_exception(404)
        with patch("app.agents.tools.tools_lib.security_tools.get_apps_v1_api", return_value=mock_apps), \
             patch("app.agents.tools.tools_lib.security_tools.get_core_v1_api", return_value=mock_core):
            from app.agents.tools.tools_lib.security_tools import image_pull_secret_check
            result = _parse(image_pull_secret_check(namespace="default", deployment_name="myapp"))
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["all_valid"])
        self.assertFalse(result["findings"][0]["exists"])

    def test_no_pull_secrets_configured(self):
        dep = self._make_deployment([])
        mock_apps = MagicMock()
        mock_apps.read_namespaced_deployment.return_value = dep
        with patch("app.agents.tools.tools_lib.security_tools.get_apps_v1_api", return_value=mock_apps), \
             patch("app.agents.tools.tools_lib.security_tools.get_core_v1_api", return_value=MagicMock()):
            from app.agents.tools.tools_lib.security_tools import image_pull_secret_check
            result = _parse(image_pull_secret_check(namespace="default", deployment_name="myapp"))
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["all_valid"])
        self.assertEqual(result["image_pull_secrets_configured"], [])

    def test_deployment_not_found(self):
        mock_apps = MagicMock()
        mock_apps.read_namespaced_deployment.side_effect = _make_api_exception(404)
        with patch("app.agents.tools.tools_lib.security_tools.get_apps_v1_api", return_value=mock_apps), \
             patch("app.agents.tools.tools_lib.security_tools.get_core_v1_api", return_value=MagicMock()):
            from app.agents.tools.tools_lib.security_tools import image_pull_secret_check
            result = _parse(image_pull_secret_check(namespace="default", deployment_name="missing"))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result.get("error_type"), "not_found")


# ─────────────────────────────────────────────────────────────────────────────
# network_policy_audit
# ─────────────────────────────────────────────────────────────────────────────

class TestNetworkPolicyAudit(unittest.TestCase):

    def _make_pod(self, labels=None):
        meta = SimpleNamespace(name="my-pod", namespace="default", labels=labels or {"app": "web"})
        return SimpleNamespace(metadata=meta)

    def _make_policy(self, name="allow-ingress", policy_types=None, pod_selector_labels=None,
                     ingress=None, egress=None):
        sel = SimpleNamespace(
            match_labels=pod_selector_labels or {},
            match_expressions=None,
        )
        spec = SimpleNamespace(
            pod_selector=sel,
            policy_types=policy_types or ["Ingress"],
            ingress=ingress,
            egress=egress,
        )
        meta = SimpleNamespace(name=name, namespace="default")
        return SimpleNamespace(metadata=meta, spec=spec)

    def test_happy_path_matching_policy(self):
        pod = self._make_pod(labels={"app": "web"})
        # Policy that selects app=web
        policy = self._make_policy(
            pod_selector_labels={"app": "web"},
            policy_types=["Ingress"],
            ingress=[],
        )
        mock_core = MagicMock()
        mock_core.read_namespaced_pod.return_value = pod
        mock_net = MagicMock()
        mock_net.list_namespaced_network_policy.return_value = SimpleNamespace(items=[policy])
        with patch("app.agents.tools.tools_lib.security_tools.get_core_v1_api", return_value=mock_core), \
             patch("app.agents.tools.tools_lib.security_tools.get_networking_v1_api", return_value=mock_net):
            from app.agents.tools.tools_lib.security_tools import network_policy_audit
            result = _parse(network_policy_audit(namespace="default", pod_name="my-pod"))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["total_matching"], 1)
        self.assertTrue(result["ingress_covered"])
        self.assertEqual(result["matching_policies"][0]["policy_name"], "allow-ingress")

    def test_no_matching_policy(self):
        pod = self._make_pod(labels={"app": "web"})
        # Policy selects app=db — does not match web pod
        policy = self._make_policy(pod_selector_labels={"app": "db"})
        mock_core = MagicMock()
        mock_core.read_namespaced_pod.return_value = pod
        mock_net = MagicMock()
        mock_net.list_namespaced_network_policy.return_value = SimpleNamespace(items=[policy])
        with patch("app.agents.tools.tools_lib.security_tools.get_core_v1_api", return_value=mock_core), \
             patch("app.agents.tools.tools_lib.security_tools.get_networking_v1_api", return_value=mock_net):
            from app.agents.tools.tools_lib.security_tools import network_policy_audit
            result = _parse(network_policy_audit(namespace="default", pod_name="my-pod"))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["total_matching"], 0)
        self.assertFalse(result["ingress_covered"])
        self.assertFalse(result["egress_covered"])

    def test_empty_selector_matches_all(self):
        pod = self._make_pod(labels={"app": "web"})
        # Empty selector matches all pods
        policy = self._make_policy(pod_selector_labels={})
        mock_core = MagicMock()
        mock_core.read_namespaced_pod.return_value = pod
        mock_net = MagicMock()
        mock_net.list_namespaced_network_policy.return_value = SimpleNamespace(items=[policy])
        with patch("app.agents.tools.tools_lib.security_tools.get_core_v1_api", return_value=mock_core), \
             patch("app.agents.tools.tools_lib.security_tools.get_networking_v1_api", return_value=mock_net):
            from app.agents.tools.tools_lib.security_tools import network_policy_audit
            result = _parse(network_policy_audit(namespace="default", pod_name="my-pod"))
        self.assertEqual(result["total_matching"], 1)

    def test_pod_not_found(self):
        mock_core = MagicMock()
        mock_core.read_namespaced_pod.side_effect = _make_api_exception(404)
        with patch("app.agents.tools.tools_lib.security_tools.get_core_v1_api", return_value=mock_core), \
             patch("app.agents.tools.tools_lib.security_tools.get_networking_v1_api", return_value=MagicMock()):
            from app.agents.tools.tools_lib.security_tools import network_policy_audit
            result = _parse(network_policy_audit(namespace="default", pod_name="missing"))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result.get("error_type"), "not_found")


# ─────────────────────────────────────────────────────────────────────────────
# rbac_who_can
# ─────────────────────────────────────────────────────────────────────────────

class TestRbacWhoCan(unittest.TestCase):

    def _make_crb(self, subject_kind, subject_name, role_name, subject_namespace=None):
        subject = SimpleNamespace(kind=subject_kind, name=subject_name, namespace=subject_namespace)
        role_ref = SimpleNamespace(kind="ClusterRole", name=role_name)
        meta = SimpleNamespace(name="test-crb", namespace=None)
        return SimpleNamespace(metadata=meta, subjects=[subject], role_ref=role_ref)

    def _make_cluster_role(self, rule_resources, verbs=None):
        rule = SimpleNamespace(
            api_groups=[""],
            resources=rule_resources,
            verbs=verbs or ["get", "list"],
            resource_names=None,
        )
        meta = SimpleNamespace(name="test-cr")
        return SimpleNamespace(metadata=meta, rules=[rule])

    def test_happy_path_user_with_cluster_role(self):
        crb = self._make_crb("User", "alice", "view")
        cr = self._make_cluster_role(["pods", "services"])
        mock_rbac = MagicMock()
        mock_rbac.list_cluster_role_binding.return_value = SimpleNamespace(items=[crb])
        mock_rbac.list_role_binding_for_all_namespaces.return_value = SimpleNamespace(items=[])
        mock_rbac.read_cluster_role.return_value = cr
        with patch("app.agents.tools.tools_lib.rbac_tools.get_rbac_v1_api", return_value=mock_rbac):
            from app.agents.tools.tools_lib.rbac_tools import rbac_who_can
            result = _parse(rbac_who_can(subject_kind="User", subject_name="alice"))
        self.assertEqual(result["status"], "success")
        self.assertGreater(result["total_rules"], 0)
        resources = [r["resource"] for r in result["rules"]]
        self.assertIn("pods", resources)
        self.assertIn("services", resources)

    def test_no_bindings_returns_zero_rules(self):
        mock_rbac = MagicMock()
        mock_rbac.list_cluster_role_binding.return_value = SimpleNamespace(items=[])
        mock_rbac.list_role_binding_for_all_namespaces.return_value = SimpleNamespace(items=[])
        with patch("app.agents.tools.tools_lib.rbac_tools.get_rbac_v1_api", return_value=mock_rbac):
            from app.agents.tools.tools_lib.rbac_tools import rbac_who_can
            result = _parse(rbac_who_can(subject_kind="User", subject_name="nobody"))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["total_rules"], 0)

    def test_service_account_namespace_matching(self):
        """ServiceAccount subjects must match on namespace too."""
        # CRB binds SA in namespace "prod" — should NOT match namespace "dev"
        crb = self._make_crb("ServiceAccount", "my-sa", "view", subject_namespace="prod")
        cr = self._make_cluster_role(["pods"])
        mock_rbac = MagicMock()
        mock_rbac.list_cluster_role_binding.return_value = SimpleNamespace(items=[crb])
        mock_rbac.list_role_binding_for_all_namespaces.return_value = SimpleNamespace(items=[])
        mock_rbac.read_cluster_role.return_value = cr
        with patch("app.agents.tools.tools_lib.rbac_tools.get_rbac_v1_api", return_value=mock_rbac):
            from app.agents.tools.tools_lib.rbac_tools import rbac_who_can
            result = _parse(rbac_who_can(
                subject_kind="ServiceAccount", subject_name="my-sa",
                subject_namespace="dev"
            ))
        self.assertEqual(result["total_rules"], 0)

    def test_api_error_propagated(self):
        mock_rbac = MagicMock()
        mock_rbac.list_cluster_role_binding.side_effect = _make_api_exception(403)
        with patch("app.agents.tools.tools_lib.rbac_tools.get_rbac_v1_api", return_value=mock_rbac):
            from app.agents.tools.tools_lib.rbac_tools import rbac_who_can
            result = _parse(rbac_who_can(subject_kind="User", subject_name="alice"))
        self.assertEqual(result["status"], "error")


# ─────────────────────────────────────────────────────────────────────────────
# rbac_check
# ─────────────────────────────────────────────────────────────────────────────

class TestRbacCheck(unittest.TestCase):

    def _make_sar_result(self, allowed: bool, reason: str = ""):
        status = SimpleNamespace(allowed=allowed, reason=reason, evaluation_error=None)
        return SimpleNamespace(status=status)

    def test_happy_path_allowed(self):
        mock_auth = MagicMock()
        mock_auth.create_subject_access_review.return_value = self._make_sar_result(True, "RBAC: allowed")
        with patch("app.agents.tools.tools_lib.rbac_tools.get_authorization_v1_api", return_value=mock_auth):
            from app.agents.tools.tools_lib.rbac_tools import rbac_check
            result = _parse(rbac_check(
                subject_kind="User", subject_name="alice",
                verb="get", resource="pods", namespace="default",
            ))
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["allowed"])
        mock_auth.create_subject_access_review.assert_called_once()

    def test_happy_path_denied(self):
        mock_auth = MagicMock()
        mock_auth.create_subject_access_review.return_value = self._make_sar_result(
            False, "RBAC: no role grants the required permissions"
        )
        with patch("app.agents.tools.tools_lib.rbac_tools.get_authorization_v1_api", return_value=mock_auth):
            from app.agents.tools.tools_lib.rbac_tools import rbac_check
            result = _parse(rbac_check(
                subject_kind="User", subject_name="alice",
                verb="delete", resource="nodes",
            ))
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["allowed"])

    def test_service_account_user_string(self):
        """ServiceAccount subjects are formatted as system:serviceaccount:<ns>:<name>."""
        mock_auth = MagicMock()
        mock_auth.create_subject_access_review.return_value = self._make_sar_result(True)
        with patch("app.agents.tools.tools_lib.rbac_tools.get_authorization_v1_api", return_value=mock_auth):
            from app.agents.tools.tools_lib.rbac_tools import rbac_check
            rbac_check(
                subject_kind="ServiceAccount", subject_name="my-sa",
                verb="list", resource="configmaps", namespace="default",
                subject_namespace="default",
            )
        call_args = mock_auth.create_subject_access_review.call_args
        body = call_args[1].get("body") or call_args[0][0]
        self.assertEqual(body.spec.user, "system:serviceaccount:default:my-sa")

    def test_api_error_propagated(self):
        mock_auth = MagicMock()
        mock_auth.create_subject_access_review.side_effect = _make_api_exception(403)
        with patch("app.agents.tools.tools_lib.rbac_tools.get_authorization_v1_api", return_value=mock_auth):
            from app.agents.tools.tools_lib.rbac_tools import rbac_check
            result = _parse(rbac_check(
                subject_kind="User", subject_name="alice",
                verb="get", resource="pods",
            ))
        self.assertEqual(result["status"], "error")


if __name__ == "__main__":
    unittest.main()
