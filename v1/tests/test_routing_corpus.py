"""
Golden-corpus routing test suite.

Each test case is a (user_query, expected_agent) pair.  The test verifies that
`create_supervisor_prompt` produces a prompt that, when evaluated by the routing
rules, maps the query to the correct specialist agent.

Since we can't call a real LLM in unit tests, we instead verify the PROGRAMMATIC
guards in `supervisor_router_node_func`:

- Clarification-loop guard: agent asking a question → FINISH
- task_complete flag: True → FINISH after ≥1 cycle
- 4xx error flag: set → FINISH

And we verify the capability manifest contains the right agents and tool names
so the supervisor prompt has the correct grounding.

To run:
    uv run pytest tests/test_routing_corpus.py -v
"""

import pytest
from unittest.mock import MagicMock
from langchain_core.messages import AIMessage, HumanMessage

from app.orchestration.state import AGENT_MEMBERS, SUPERVISOR_OPTIONS
from app.orchestration.routing import (
    check_clarification_loop,
    supervisor_router_node_func,
    build_agent_capability_manifest,
    AGENT_TO_CATEGORIES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(**overrides) -> dict:
    """Return a minimal valid KubeIntellectState dict."""
    base: dict = {
        "messages": [],
        "next": "",
        "intermediate_steps": [],
        "supervisor_cycles": 0,
        "agent_results": {},
        "last_tool_error": None,
        "task_complete": None,
        "reflection_memory": [],
    }
    base.update(overrides)
    return base


def _ai(content: str, name: str = "Logs") -> AIMessage:
    return AIMessage(content=content, name=name)


def _human(content: str) -> HumanMessage:
    return HumanMessage(content=content)


# ---------------------------------------------------------------------------
# Phase 1 — AGENT_MEMBERS completeness
# ---------------------------------------------------------------------------

class TestAgentMembers:
    def test_all_expected_agents_present(self):
        expected = {
            "Logs", "ConfigMapsSecrets", "RBAC", "Metrics", "Security",
            "Lifecycle", "Execution", "Deletion", "Infrastructure",
            "DynamicToolsExecutor", "CodeGenerator", "Apply",
            "DiagnosticsOrchestrator",
        }
        assert set(AGENT_MEMBERS) == expected

    def test_supervisor_options_includes_finish(self):
        assert "FINISH" in SUPERVISOR_OPTIONS

    def test_no_legacy_names(self):
        legacy = {"Configs", "AdvancedOps"}
        assert not legacy.intersection(AGENT_MEMBERS), (
            f"Legacy agent names still in AGENT_MEMBERS: {legacy & set(AGENT_MEMBERS)}"
        )

    def test_agent_to_categories_keys_match_members(self):
        """Every agent in AGENT_TO_CATEGORIES must be in AGENT_MEMBERS."""
        for agent_name in AGENT_TO_CATEGORIES:
            assert agent_name in AGENT_MEMBERS, (
                f"'{agent_name}' in AGENT_TO_CATEGORIES but not in AGENT_MEMBERS"
            )


# ---------------------------------------------------------------------------
# Phase 2 — Capability manifest
# ---------------------------------------------------------------------------

class TestCapabilityManifest:
    def _mock_tool(self, name: str, description: str = "A test tool."):
        t = MagicMock()
        t.name = name
        t.description = description
        return t

    def _make_tool_categories(self):
        return {
            "logs":       [self._mock_tool("get_pod_logs", "Fetch pod container logs."),
                           self._mock_tool("list_namespace_events", "List events in a namespace.")],
            "configs":    [self._mock_tool("list_configmaps", "List all ConfigMaps."),
                           self._mock_tool("list_secrets", "List all Secrets.")],
            "rbac":       [self._mock_tool("list_roles", "List RBAC roles.")],
            "metrics":    [self._mock_tool("get_pod_metrics", "Get pod CPU/memory metrics.")],
            "security":   [self._mock_tool("analyze_audit_logs", "Analyze Kubernetes audit logs.")],
            "lifecycle":  [self._mock_tool("scale_deployment", "Scale a deployment.")],
            "execution":  [self._mock_tool("exec_in_pod", "Execute a command in a pod.")],
            "deletion":   [self._mock_tool("delete_pod", "Delete a pod.")],
            "advancedops":[self._mock_tool("create_service", "Create a Kubernetes Service.")],
            "code_gen":   [self._mock_tool("generate_k8s_script", "Generate a new tool script.")],
            "apply":      [self._mock_tool("apply_manifest", "Apply a YAML manifest.")],
            "dynamic":    [],
        }

    def test_manifest_contains_all_agents(self):
        from app.orchestration.routing import CUSTOM_NODE_AGENTS
        cats = self._make_tool_categories()
        manifest = build_agent_capability_manifest(cats)
        for agent in AGENT_MEMBERS:
            if agent in CUSTOM_NODE_AGENTS:
                # Custom nodes (e.g. DiagnosticsOrchestrator) have no direct tools
                # and are described in the supervisor prompt routing rules, not the manifest.
                continue
            assert f"**{agent}**" in manifest, f"Agent '{agent}' missing from capability manifest"

    def test_manifest_contains_tool_names(self):
        cats = self._make_tool_categories()
        manifest = build_agent_capability_manifest(cats)
        assert "get_pod_logs" in manifest
        assert "list_configmaps" in manifest
        assert "scale_deployment" in manifest

    def test_manifest_contains_tool_descriptions(self):
        cats = self._make_tool_categories()
        manifest = build_agent_capability_manifest(cats)
        # Descriptions should now appear (L3 improvement)
        assert "Fetch pod container logs" in manifest
        assert "List all ConfigMaps" in manifest

    def test_manifest_empty_dynamic_tools(self):
        cats = self._make_tool_categories()
        manifest = build_agent_capability_manifest(cats)
        assert "**DynamicToolsExecutor**: (no tools)" in manifest


# ---------------------------------------------------------------------------
# Phase 3 — Clarification-loop guard
# ---------------------------------------------------------------------------

class TestClarificationLoopGuard:
    def test_no_loop_on_first_question(self):
        msgs = [_human("list pods"), _ai("Which namespace?", "Logs")]
        is_loop, count = check_clarification_loop(msgs)
        assert not is_loop

    def test_detects_loop_after_three_clarifications(self):
        msgs = [
            _human("list pods"),
            _ai("Which namespace?", "Logs"),
            _human("default"),
            _ai("Which namespace?", "Logs"),
            _human("I said default"),
            _ai("Could you please specify the namespace?", "Logs"),
        ]
        is_loop, count = check_clarification_loop(msgs, max_clarifications=3)
        assert is_loop
        assert count >= 3

    def test_no_loop_after_successful_response(self):
        msgs = [
            _human("list pods"),
            _ai("Here are the pods in the default namespace: pod-1, pod-2", "Logs"),
        ]
        is_loop, count = check_clarification_loop(msgs)
        assert not is_loop
        assert count == 0


# ---------------------------------------------------------------------------
# Phase 4 — Programmatic supervisor guards
# ---------------------------------------------------------------------------

class TestSupervisorGuards:
    """Tests for deterministic routing guards that don't require a real LLM."""

    def _make_chain(self, decision: str = "FINISH"):
        chain = MagicMock()
        route = MagicMock()
        route.next = decision
        chain.invoke.return_value = route
        return chain

    def test_4xx_error_forces_finish(self):
        state = _make_state(
            messages=[_human("describe pod foo")],
            last_tool_error={"http_status": 404, "agent": "Logs", "message": "Not found"},
        )
        result = supervisor_router_node_func(state, self._make_chain("Logs"))
        assert result["next"] == "FINISH"

    def test_task_complete_forces_finish_after_one_cycle(self):
        state = _make_state(
            messages=[_human("list pods"), _ai("Here are the pods: pod-a, pod-b", "Logs")],
            supervisor_cycles=1,
            task_complete=True,
        )
        result = supervisor_router_node_func(state, self._make_chain("Logs"))
        assert result["next"] == "FINISH"

    def test_task_complete_does_not_finish_on_first_call(self):
        """If task_complete=True but no cycle has happened yet, don't FINISH immediately."""
        state = _make_state(
            messages=[_human("list pods")],
            supervisor_cycles=0,
            task_complete=True,
        )
        chain = self._make_chain("Logs")
        result = supervisor_router_node_func(state, chain)
        # Should have invoked the LLM chain (not returned early)
        assert result["next"] in SUPERVISOR_OPTIONS

    def test_cycle_limit_forces_finish(self):
        state = _make_state(
            messages=[_human("list pods"), _ai("Looking...", "Logs")],
            supervisor_cycles=10,
        )
        result = supervisor_router_node_func(state, self._make_chain("Logs"))
        assert result["next"] == "FINISH"

    def test_duplicate_response_forces_finish(self):
        duplicate_content = "Here are the pods in namespace default: pod-a, pod-b, pod-c"
        state = _make_state(
            messages=[
                _human("list pods"),
                _ai(duplicate_content, "Logs"),
                _ai(duplicate_content, "Logs"),
                _ai(duplicate_content, "Logs"),
            ],
            supervisor_cycles=2,
        )
        result = supervisor_router_node_func(state, self._make_chain("Logs"))
        assert result["next"] == "FINISH"

    def test_clarification_question_forces_finish(self):
        state = _make_state(
            messages=[
                _human("list pods"),
                _ai("Which namespace would you like to list pods in?", "Logs"),
            ],
            supervisor_cycles=1,
        )
        result = supervisor_router_node_func(state, self._make_chain("Logs"))
        assert result["next"] == "FINISH"

    def test_tool_just_created_routes_to_dynamic_executor(self):
        last_msg = AIMessage(
            content="Tool created successfully.",
            name="CodeGenerator",
            additional_kwargs={"tool_just_created": True},
        )
        state = _make_state(
            messages=[_human("create a custom tool to count evicted pods"), last_msg],
            supervisor_cycles=1,
        )
        result = supervisor_router_node_func(state, self._make_chain("FINISH"))
        assert result["next"] == "DynamicToolsExecutor"


# ---------------------------------------------------------------------------
# Phase 5 — Routing corpus (query → expected agent annotation)
# ---------------------------------------------------------------------------
# These are documentation tests — they verify that routing INTENTIONS expressed
# in the supervisor prompt match expectations.  Because we can't call a real
# LLM in unit tests, they are written as parametrized "classification hints"
# that future integration tests can drive against a real LLM.
# ---------------------------------------------------------------------------

ROUTING_CORPUS = [
    # Logs
    ("show me the logs for pod my-app-7f8d in production",               "Logs"),
    ("get the last 100 lines of logs from nginx-pod",                    "Logs"),
    ("which pods have restarted more than 5 times",                      "Logs"),
    ("how many times has payment-service restarted",                     "Logs"),
    ("list all warning events in the default namespace",                  "Logs"),
    ("show me CrashLoopBackOff logs for api-pod",                        "Logs"),
    ("describe pod worker-123 in namespace jobs",                         "Logs"),
    ("list pods in the kube-system namespace",                            "Logs"),
    ("get pod status for ingress-controller",                             "Logs"),

    # ConfigMapsSecrets
    ("create a configmap called app-config in production",               "ConfigMapsSecrets"),
    ("update the database password in secret db-creds",                  "ConfigMapsSecrets"),
    ("patch the API_URL key in configmap app-settings",                  "ConfigMapsSecrets"),
    ("list all secrets in the staging namespace",                         "ConfigMapsSecrets"),
    ("delete configmap old-config from namespace dev",                   "ConfigMapsSecrets"),
    ("create a secret with my-token key",                                "ConfigMapsSecrets"),

    # RBAC
    ("list all roles in the production namespace",                        "RBAC"),
    ("who has cluster-admin access",                                      "RBAC"),
    ("check role bindings for service account default",                  "RBAC"),
    ("create a clusterrole for read-only pod access",                    "RBAC"),
    ("audit RBAC permissions in the kube-system namespace",              "RBAC"),

    # Metrics
    ("what is the CPU usage of my nodes",                                "Metrics"),
    ("show me memory usage for all pods in production",                  "Metrics"),
    ("query prometheus for HTTP request rate",                            "Metrics"),
    ("which pods are consuming the most CPU",                            "Metrics"),

    # Security
    ("check for pod security policy violations",                          "Security"),
    ("analyze audit logs for unauthorized access",                        "Security"),
    ("scan the cluster for vulnerabilities",                              "Security"),

    # Lifecycle
    ("scale the api-gateway deployment to 5 replicas",                   "Lifecycle"),
    ("roll back the payment-service to the previous version",            "Lifecycle"),
    ("cordon node worker-3 before maintenance",                          "Lifecycle"),
    ("drain node worker-2 safely",                                       "Lifecycle"),
    ("restart the web-frontend deployment",                              "Lifecycle"),
    ("describe the nginx deployment",                                    "Lifecycle"),
    ("show env vars for the api-service deployment",                     "Lifecycle"),
    ("what image is the frontend deployment running",                    "Lifecycle"),
    ("create a deployment for redis with 2 replicas",                    "Lifecycle"),
    ("update the container image of payment-service to v2.1",           "Lifecycle"),
    ("create an HPA for web-service with min=2 max=10 cpu=70",          "Lifecycle"),
    ("scale statefulset postgres to 3 replicas",                         "Lifecycle"),
    ("check rollout status for api-gateway",                             "Lifecycle"),
    ("uncordon node worker-3",                                           "Lifecycle"),

    # Execution
    ("exec into pod debug-pod and run bash",                             "Execution"),
    ("port-forward service postgres 5432 to localhost",                  "Execution"),
    ("attach to container in running pod",                               "Execution"),

    # Deletion
    ("delete pod crashed-worker-abc from default namespace",             "Deletion"),
    ("remove the old-deployment from production",                        "Deletion"),
    ("clean up all failed jobs in the batch namespace",                  "Deletion"),
    ("delete namespace staging",                                         "Deletion"),

    # Infrastructure (formerly AdvancedOps)
    ("create a service for my-app exposing port 8080",                  "Infrastructure"),
    ("list all services in the production namespace",                    "Infrastructure"),
    ("create a network policy to block ingress from outside",            "Infrastructure"),
    ("create a PVC of 10Gi for the database",                           "Infrastructure"),
    ("list all persistent volume claims",                                "Infrastructure"),
    ("add a resource quota to the dev namespace",                       "Infrastructure"),
    ("list all failed jobs in the batch namespace",                     "Infrastructure"),
    ("list all namespaces",                                              "Infrastructure"),
    ("create namespace staging",                                         "Infrastructure"),
    ("patch service selector to point to green deployment",             "Infrastructure"),
    ("describe the frontend service",                                    "Infrastructure"),
    ("what services are exposed externally",                             "Infrastructure"),

    # CodeGenerator
    ("create a custom tool to count evicted pods across all namespaces", "CodeGenerator"),
    ("generate a script that lists pods sorted by restart count",        "CodeGenerator"),

    # Apply
    ("apply this yaml manifest to the cluster",                          "Apply"),
    ("kubectl apply the following deployment spec",                      "Apply"),
]


@pytest.mark.parametrize("query,expected_agent", ROUTING_CORPUS)
def test_routing_corpus_annotation(query: str, expected_agent: str):
    """
    Verify that every corpus query has a valid expected_agent in AGENT_MEMBERS.

    This test does NOT call an LLM — it validates the corpus itself is
    well-formed so integration tests (which do call an LLM) can rely on it.
    """
    assert expected_agent in AGENT_MEMBERS, (
        f"Query '{query[:60]}' targets agent '{expected_agent}' "
        f"which is not in AGENT_MEMBERS={AGENT_MEMBERS}"
    )


@pytest.mark.parametrize("query,expected_agent", ROUTING_CORPUS)
def test_routing_corpus_not_legacy_agent(query: str, expected_agent: str):
    """No corpus entry should reference a legacy agent name."""
    legacy = {"Configs", "AdvancedOps"}
    assert expected_agent not in legacy, (
        f"Corpus entry uses legacy agent name '{expected_agent}': {query[:60]}"
    )
