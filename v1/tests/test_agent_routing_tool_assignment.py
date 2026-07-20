# tests/test_agent_routing_tool_assignment.py
"""
Tests for the five fixes from decision 2026-04-03-agent_routing_tool_assignment:

Issue 1 — list_all_pods_across_namespaces excluded from Logs agent tools
Issue 3 — cross-namespace 404 fallback in deployment + rollout tools
Issue 4 — label-selector tools accept optional namespace parameter
Issue 5 — supervisor dispatch fingerprinting blocks re-dispatch loops
"""

import unittest
from unittest.mock import MagicMock, patch


# ── Issue 1 ───────────────────────────────────────────────────────────────────

class TestLogsToolset(unittest.TestCase):
    """list_all_pods_across_namespaces must NOT appear in k8s_logs_tools."""

    def test_list_all_pods_excluded_from_logs(self):
        from app.agents.tools.kubernetes_tools import k8s_logs_tools
        names = {t.name for t in k8s_logs_tools}
        self.assertNotIn(
            "list_all_pods_across_namespaces",
            names,
            "list_all_pods_across_namespaces must not be in the Logs agent tool set",
        )

    def test_logs_toolset_still_has_pod_log_tool(self):
        from app.agents.tools.kubernetes_tools import k8s_logs_tools
        names = {t.name for t in k8s_logs_tools}
        self.assertIn(
            "get_pod_logs",
            names,
            "get_pod_logs must remain in the Logs agent tool set",
        )


# ── Issue 3 ───────────────────────────────────────────────────────────────────

class TestCrossNamespaceFallback(unittest.TestCase):
    """Deployment 404 triggers cluster-wide search; 403 on fallback returns permission error."""

    def _make_api_exception(self, status: int) -> Exception:
        from kubernetes.client.exceptions import ApiException
        exc = ApiException(status=status)
        exc.body = None
        return exc

    def _make_deployment(self, name: str, namespace: str):
        d = MagicMock()
        d.metadata.name = name
        d.metadata.namespace = namespace
        d.spec.replicas = 1
        d.status.updated_replicas = 1
        d.status.available_replicas = 1
        d.status.ready_replicas = 1
        d.status.unavailable_replicas = 0
        return d

    @patch("app.agents.tools.tools_lib.deployment_tools.get_apps_v1_api")
    def test_deployment_found_in_different_namespace(self, mock_api):
        """get_deployment_rollout_status finds deployment in namespace B when queried with A."""
        from app.agents.tools.tools_lib.deployment_tools import get_deployment_rollout_status

        mock_apps = MagicMock()
        mock_api.return_value = mock_apps

        # First call (read_namespaced) raises 404
        mock_apps.read_namespaced_deployment.side_effect = self._make_api_exception(404)

        # Fallback call returns the deployment in namespace B
        real_deployment = self._make_deployment("my-app", "namespace-b")
        fallback_result = MagicMock()
        fallback_result.items = [real_deployment]
        mock_apps.list_deployment_for_all_namespaces.return_value = fallback_result

        result = get_deployment_rollout_status(namespace="namespace-a", deployment_name="my-app")

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"]["namespace"], "namespace-b")
        mock_apps.list_deployment_for_all_namespaces.assert_called_once_with(
            field_selector="metadata.name=my-app",
            timeout_seconds=10,
        )

    @patch("app.agents.tools.tools_lib.deployment_tools.get_apps_v1_api")
    def test_403_on_fallback_returns_permission_error(self, mock_api):
        """get_deployment_rollout_status returns permission error (not 'not found') on 403."""
        from app.agents.tools.tools_lib.deployment_tools import get_deployment_rollout_status

        mock_apps = MagicMock()
        mock_api.return_value = mock_apps

        mock_apps.read_namespaced_deployment.side_effect = self._make_api_exception(404)
        mock_apps.list_deployment_for_all_namespaces.side_effect = self._make_api_exception(403)

        result = get_deployment_rollout_status(namespace="namespace-a", deployment_name="my-app")

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_type"], "PermissionDenied")
        self.assertIn("403", result["message"])
        # Must NOT look like a generic "not found" answer
        self.assertNotIn("not found", result["message"].lower().replace("not found in namespace", ""))

    @patch("app.agents.tools.tools_lib.rollout_tools.get_apps_v1_api")
    def test_rollout_status_found_in_different_namespace(self, mock_api):
        """rollout_status (generic) finds deployment in namespace B when queried with A."""
        from app.agents.tools.tools_lib.rollout_tools import rollout_status

        mock_apps = MagicMock()
        mock_api.return_value = mock_apps

        from kubernetes.client.exceptions import ApiException
        exc = ApiException(status=404)
        exc.body = None
        mock_apps.read_namespaced_deployment.side_effect = exc

        real_deployment = self._make_deployment("my-app", "namespace-b")
        real_deployment.status.conditions = []
        fallback_result = MagicMock()
        fallback_result.items = [real_deployment]
        mock_apps.list_deployment_for_all_namespaces.return_value = fallback_result

        result = rollout_status(namespace="namespace-a", name="my-app", kind="Deployment")
        # rollout_status now returns a JSON string; parse it
        import json as _json
        if isinstance(result, str):
            result = _json.loads(result)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"]["namespace"], "namespace-b")
        self.assertIn("namespace_resolved", result["data"])


# ── Issue 4 ───────────────────────────────────────────────────────────────────

class TestLabelSelectorNamespaceParam(unittest.TestCase):
    """label-selector tools scope results to a namespace when namespace is provided."""

    @patch("app.agents.tools.tools_lib.pod_tools.get_core_v1_api")
    def test_pod_label_selector_with_namespace_uses_namespaced_call(self, mock_api):
        from app.agents.tools.tools_lib.pod_tools import list_resources_with_label

        mock_core = MagicMock()
        mock_api.return_value = mock_core
        mock_core.list_namespaced_pod.return_value = MagicMock(items=[])

        result = list_resources_with_label(label_selector="app=nginx", namespace="staging")

        mock_core.list_namespaced_pod.assert_called_once_with(
            namespace="staging",
            label_selector="app=nginx",
            timeout_seconds=10,
        )
        mock_core.list_pod_for_all_namespaces.assert_not_called()
        self.assertEqual(result["status"], "success")

    @patch("app.agents.tools.tools_lib.pod_tools.get_core_v1_api")
    def test_pod_label_selector_without_namespace_uses_all_namespaces(self, mock_api):
        from app.agents.tools.tools_lib.pod_tools import list_resources_with_label

        mock_core = MagicMock()
        mock_api.return_value = mock_core
        mock_core.list_pod_for_all_namespaces.return_value = MagicMock(items=[])

        result = list_resources_with_label(label_selector="app=nginx")

        mock_core.list_pod_for_all_namespaces.assert_called_once()
        mock_core.list_namespaced_pod.assert_not_called()
        self.assertEqual(result["status"], "success")

    @patch("app.agents.tools.tools_lib.namespace_tools.get_core_v1_api")
    def test_namespace_label_selector_with_namespace_uses_namespaced_calls(self, mock_api):
        from app.agents.tools.tools_lib.namespace_tools import list_resources_with_label

        mock_core = MagicMock()
        mock_api.return_value = mock_core
        mock_core.list_namespaced_pod.return_value = MagicMock(items=[])
        mock_core.list_namespaced_service.return_value = MagicMock(items=[])
        mock_core.list_namespaced_config_map.return_value = MagicMock(items=[])

        result = list_resources_with_label(label_selector="env=prod", namespace="staging")

        mock_core.list_namespaced_pod.assert_called_once_with(
            namespace="staging", label_selector="env=prod", timeout_seconds=10
        )
        mock_core.list_namespaced_service.assert_called_once_with(
            namespace="staging", label_selector="env=prod", timeout_seconds=10
        )
        mock_core.list_namespaced_config_map.assert_called_once_with(
            namespace="staging", label_selector="env=prod", timeout_seconds=10
        )
        self.assertEqual(result["status"], "success")


# ── Issue 5 ───────────────────────────────────────────────────────────────────

class TestDispatchFingerprinting(unittest.TestCase):
    """Supervisor returns FINISH on second dispatch of same agent for same user turn."""

    def _make_state(self, human_content: str, seen_dispatches=None) -> dict:
        from langchain_core.messages import HumanMessage
        return {
            "messages": [HumanMessage(content=human_content)],
            "next": "",
            "intermediate_steps": [],
            "reflection_memory": [],
            "agent_results": {},
            "last_tool_error": None,
            "supervisor_cycles": 0,
            "task_complete": None,
            "steps_taken": [],
            "plan": [],
            "plan_step": 0,
            "seen_dispatches": seen_dispatches,
        }

    def test_second_dispatch_same_agent_returns_finish(self):
        """If the same agent is dispatched twice for the same human message, second call → FINISH."""
        from langchain_core.messages import HumanMessage
        from app.orchestration.routing import supervisor_router_node_func

        human_msg = HumanMessage(content="list all pods in default namespace")
        human_hash = str(hash(str(human_msg.content)[:200]))
        # Fingerprint format is agent:hash:steps_taken_count (steps_taken=[] → count=0)
        existing_fingerprint = f"Lifecycle:{human_hash}:0"

        state = self._make_state("list all pods in default namespace", seen_dispatches=[existing_fingerprint])

        mock_chain = MagicMock()
        route = MagicMock()
        route.next = "Lifecycle"
        route.plan = None
        route.needs_human_input = False
        route.task_plan = None
        mock_chain.invoke.return_value = route

        result = supervisor_router_node_func(state, mock_chain)

        self.assertEqual(result["next"], "FINISH",
                         "Expected FINISH on duplicate dispatch, got: " + result["next"])

    def test_first_dispatch_proceeds_normally(self):
        """First dispatch of an agent adds fingerprint and routes normally."""
        from app.orchestration.routing import supervisor_router_node_func

        state = self._make_state("list all pods in default namespace", seen_dispatches=[])

        mock_chain = MagicMock()
        route = MagicMock()
        route.next = "Lifecycle"
        route.plan = None
        route.needs_human_input = False
        route.task_plan = None
        mock_chain.invoke.return_value = route

        result = supervisor_router_node_func(state, mock_chain)

        self.assertEqual(result["next"], "Lifecycle")
        self.assertIn("seen_dispatches", result)
        self.assertEqual(len(result["seen_dispatches"]), 1)

    def test_different_agents_do_not_collide(self):
        """Dispatching agent B after agent A for the same turn is not blocked."""
        from langchain_core.messages import HumanMessage
        from app.orchestration.routing import supervisor_router_node_func

        human_msg = HumanMessage(content="check pod logs and metrics")
        human_hash = str(hash(str(human_msg.content)[:200]))
        state = self._make_state("check pod logs and metrics", seen_dispatches=[f"Logs:{human_hash}:0"])

        mock_chain = MagicMock()
        route = MagicMock()
        route.next = "Metrics"
        route.plan = None
        route.needs_human_input = False
        route.task_plan = None
        mock_chain.invoke.return_value = route

        result = supervisor_router_node_func(state, mock_chain)

        self.assertEqual(result["next"], "Metrics")


if __name__ == "__main__":
    unittest.main()
