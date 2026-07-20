"""
KubeIntellect MCP Server

Exposes KubeIntellect's Kubernetes management capabilities via the Model Context
Protocol (MCP). Provides two access modes:

  1. Direct K8s tools — import the underlying Python functions from app/agents/tools/
     for low-latency, single-resource operations (list, describe, logs, scale, etc.)

  2. Workflow tool — POSTs to the KubeIntellect HTTP API for complex multi-step
     queries that benefit from the full multi-agent workflow and AI reasoning.

Usage (stdio transport, for Claude Desktop / any MCP client):
    uv run python -m app.mcp.server

Environment variables:
    KUBEINTELLECT_API_URL   Base URL of the KubeIntellect API (default: http://localhost:8000)
    KUBEINTELLECT_API_KEY   Optional bearer token for the API
    KUBECONFIG              Path to kubeconfig (default: ~/.kube/config)
"""

import json
import os
import sys

import httpx
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Project root on sys.path so "from app.xxx import" works when the server is
# launched from any working directory (e.g. `uv run python -m app.mcp.server`).
# ---------------------------------------------------------------------------
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
KUBEINTELLECT_API_URL = os.getenv("KUBEINTELLECT_API_URL", "http://localhost:8000")
KUBEINTELLECT_API_KEY = os.getenv("KUBEINTELLECT_API_KEY", "")

# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "KubeIntellect",
    instructions=(
        "KubeIntellect MCP server — AI-powered Kubernetes management. "
        "Use `kubeintellect_query` for any natural-language Kubernetes question or command. "
        "Direct tools (list_pods, get_pod_logs, scale_deployment, …) are available for "
        "targeted operations without going through the full AI workflow."
    ),
)


# ---------------------------------------------------------------------------
# Helper: call the KubeIntellect HTTP API
# ---------------------------------------------------------------------------

def _api_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if KUBEINTELLECT_API_KEY:
        h["Authorization"] = f"Bearer {KUBEINTELLECT_API_KEY}"
    return h


def _call_api(method: str, path: str, **kwargs) -> dict:
    """Synchronous HTTP helper — returns parsed JSON or an error dict."""
    url = f"{KUBEINTELLECT_API_URL}{path}"
    try:
        with httpx.Client(timeout=120) as client:
            resp = client.request(method, url, headers=_api_headers(), **kwargs)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as exc:
        return {"error": f"HTTP {exc.response.status_code}: {exc.response.text[:400]}"}
    except Exception as exc:
        return {"error": str(exc)}


def _fmt(value) -> str:
    """Pretty-print a dict/list/str result as a string."""
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, default=str)


# ---------------------------------------------------------------------------
# Import K8s tool functions (best-effort — graceful fallback to API if deps
# are unavailable, e.g. when running outside the virtualenv).
# ---------------------------------------------------------------------------

def _safe_import(module_path: str, names: list[str]) -> dict:
    try:
        import importlib
        mod = importlib.import_module(module_path)
        return {n: getattr(mod, n) for n in names if hasattr(mod, n)}
    except Exception:
        return {}


_pod_fns = _safe_import(
    "app.agents.tools.tools_lib.pod_tools",
    ["list_all_pods", "list_pods_in_namespace", "describe_kubernetes_pod",
     "get_pod_logs", "get_pod_events", "get_pod_diagnostics", "list_error_pods"],
)
_deploy_fns = _safe_import(
    "app.agents.tools.tools_lib.deployment_tools",
    ["list_all_deployments", "list_deployments_in_namespace",
     "describe_kubernetes_deployment", "scale_deployment",
     "rollout_restart_deployment", "get_deployment_rollout_status"],
)
_svc_fns = _safe_import(
    "app.agents.tools.tools_lib.services_tools",
    ["list_all_services", "list_services_in_namespace",
     "describe_service", "check_service_endpoints"],
)
_ns_fns = _safe_import(
    "app.agents.tools.tools_lib.namespace_tools",
    ["list_kubernetes_namespaces", "describe_namespace",
     "list_namespace_events", "get_namespace_warning_events",
     "get_namespace_resource_usage"],
)
_node_fns = _safe_import(
    "app.agents.tools.tools_lib.node_tools",
    ["list_kubernetes_nodes", "describe_node", "get_node_resource_usage",
     "cordon_node", "uncordon_node"],
)
_rbac_fns = _safe_import(
    "app.agents.tools.tools_lib.rbac_tools",
    ["list_roles", "list_cluster_roles", "list_role_bindings",
     "list_service_accounts", "check_who_can"],
)
_metrics_fns = _safe_import(
    "app.agents.tools.tools_lib.metrics_tools",
    ["get_pod_cpu_usage", "get_pod_memory_usage",
     "get_node_resource_usage", "get_cluster_resource_summary"],
)


def _call_direct(fn_map: dict, name: str, *args, **kwargs) -> str:
    """Call a directly imported K8s function, or return an error string."""
    fn = fn_map.get(name)
    if fn is None:
        return f"Error: function '{name}' could not be loaded (check K8s deps)."
    try:
        return _fmt(fn(*args, **kwargs))
    except Exception as exc:
        return f"Error: {exc}"


# ===========================================================================
# ── WORKFLOW TOOLS ──────────────────────────────────────────────────────────
# ===========================================================================

@mcp.tool()
def kubeintellect_query(
    query: str,
    conversation_id: str = "",
    user_id: str = "mcp-user",
) -> str:
    """
    Send a natural-language query to KubeIntellect's multi-agent AI workflow.

    Use this for any complex Kubernetes question, troubleshooting task, or
    multi-step command — the AI supervisor routes the request to the correct
    specialist agent (logs, metrics, RBAC, lifecycle, security, etc.).

    Examples:
      - "Why are the pods in namespace prod crashing?"
      - "Scale the api-server deployment to 5 replicas"
      - "Show recent warnings across all namespaces"
      - "Check if any service accounts have cluster-admin permissions"

    For a subsequent turn in the same conversation, pass the same conversation_id.
    """
    payload: dict = {
        "model": "kubeintellect",
        "messages": [{"role": "user", "content": query}],
        "stream": False,
        "user_id": user_id or "mcp-user",
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id

    result = _call_api("POST", "/v1/chat/completions", json=payload)
    if "error" in result:
        return result["error"]

    # OpenAI-compatible response shape
    try:
        return result["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return _fmt(result)


@mcp.tool()
def kubeintellect_approve(
    conversation_id: str,
    user_id: str,
    approved: bool,
    feedback: str = "",
) -> str:
    """
    Approve or reject a pending Human-in-the-Loop (HITL) code-generation request.

    When KubeIntellect's CodeGenerator agent creates a new Kubernetes tool, it
    pauses and asks for human approval before executing the generated code.
    Use this tool to resume the workflow.

    Args:
        conversation_id: The ID returned with the HITL breakpoint event.
        user_id: Must match the user_id used in the original kubeintellect_query.
        approved: True to approve and execute the generated code; False to reject.
        feedback: Optional message sent back to the agent (e.g. rejection reason).
    """
    decision = "approved" if approved else "rejected"
    message = feedback or decision
    payload = {
        "model": "kubeintellect",
        "messages": [{"role": "user", "content": message}],
        "stream": False,
        "conversation_id": conversation_id,
        "user_id": user_id,
        "resume": True,
    }
    result = _call_api("POST", "/v1/chat/completions", json=payload)
    if "error" in result:
        return result["error"]
    try:
        return result["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return _fmt(result)


# ===========================================================================
# ── POD TOOLS ───────────────────────────────────────────────────────────────
# ===========================================================================

@mcp.tool()
def list_pods(namespace: str = "") -> str:
    """
    List Kubernetes pods. Leave namespace empty to list pods across all namespaces.
    Returns pod name, namespace, status, restarts, and age.
    """
    if namespace:
        return _call_direct(_pod_fns, "list_pods_in_namespace", namespace)
    return _call_direct(_pod_fns, "list_all_pods")


@mcp.tool()
def describe_pod(pod_name: str, namespace: str) -> str:
    """
    Get full details about a Kubernetes pod: spec, status, conditions, events.
    """
    return _call_direct(_pod_fns, "describe_kubernetes_pod", namespace, pod_name)


@mcp.tool()
def get_pod_logs(
    pod_name: str,
    namespace: str,
    tail_lines: int = 50,
    previous: bool = False,
    container: str = "",
) -> str:
    """
    Fetch logs from a pod.

    Args:
        pod_name: Name of the pod.
        namespace: Namespace where the pod lives.
        tail_lines: How many lines from the end to return (default 50).
        previous: If True, fetch logs from the previous (crashed) container instance.
        container: Container name for multi-container pods.
    """
    kwargs: dict = {
        "name": pod_name,
        "namespace": namespace,
        "tail_lines": tail_lines,
        "previous": previous,
    }
    if container:
        kwargs["container"] = container
    return _call_direct(_pod_fns, "get_pod_logs", **kwargs)


@mcp.tool()
def get_pod_events(pod_name: str, namespace: str) -> str:
    """Get Kubernetes events associated with a specific pod."""
    return _call_direct(_pod_fns, "get_pod_events", namespace, pod_name)


@mcp.tool()
def get_pod_diagnostics(pod_name: str, namespace: str, tail_lines: int = 100) -> str:
    """
    Run a comprehensive diagnostic on a pod: status, recent events, and logs
    from the last container run. Useful for troubleshooting CrashLoopBackOff.
    """
    return _call_direct(_pod_fns, "get_pod_diagnostics", pod_name, namespace, tail_lines)


@mcp.tool()
def list_error_pods(namespace: str = "") -> str:
    """
    List pods that are in an error or non-Running state (CrashLoopBackOff,
    OOMKilled, Pending, etc.). Leave namespace empty to check all namespaces.
    """
    if namespace:
        return _call_direct(_pod_fns, "list_error_pods", namespace)
    return _call_direct(_pod_fns, "list_error_pods")


# ===========================================================================
# ── DEPLOYMENT TOOLS ────────────────────────────────────────────────────────
# ===========================================================================

@mcp.tool()
def list_deployments(namespace: str = "") -> str:
    """
    List Kubernetes deployments. Leave namespace empty for all namespaces.
    Shows name, namespace, desired/ready/available replicas, and age.
    """
    if namespace:
        return _call_direct(_deploy_fns, "list_deployments_in_namespace", namespace)
    return _call_direct(_deploy_fns, "list_all_deployments")


@mcp.tool()
def describe_deployment(deployment_name: str, namespace: str) -> str:
    """Get detailed spec, status, and rollout history for a deployment."""
    return _call_direct(_deploy_fns, "describe_kubernetes_deployment", namespace, deployment_name)


@mcp.tool()
def scale_deployment(deployment_name: str, namespace: str, replicas: int) -> str:
    """
    Scale a deployment to the specified number of replicas.

    Args:
        deployment_name: Name of the deployment to scale.
        namespace: Namespace of the deployment.
        replicas: Target replica count (0 to suspend).
    """
    return _call_direct(_deploy_fns, "scale_deployment", namespace, deployment_name, replicas)


@mcp.tool()
def restart_deployment(deployment_name: str, namespace: str) -> str:
    """
    Perform a rolling restart of a deployment (equivalent to kubectl rollout restart).
    Triggers a new rollout by updating the pod-template annotation.
    """
    return _call_direct(_deploy_fns, "rollout_restart_deployment", namespace, deployment_name)


@mcp.tool()
def get_rollout_status(deployment_name: str, namespace: str) -> str:
    """Check the current rollout status of a deployment (in-progress, complete, or failed)."""
    return _call_direct(_deploy_fns, "get_deployment_rollout_status", namespace, deployment_name)


# ===========================================================================
# ── SERVICE TOOLS ───────────────────────────────────────────────────────────
# ===========================================================================

@mcp.tool()
def list_services(namespace: str = "") -> str:
    """
    List Kubernetes services. Leave namespace empty for all namespaces.
    Shows name, type (ClusterIP/NodePort/LoadBalancer), cluster IP, and ports.
    """
    if namespace:
        return _call_direct(_svc_fns, "list_services_in_namespace", namespace)
    return _call_direct(_svc_fns, "list_all_services")


@mcp.tool()
def describe_service(service_name: str, namespace: str) -> str:
    """Get full details for a Kubernetes service: spec, selectors, ports, endpoints."""
    return _call_direct(_svc_fns, "describe_service", namespace, service_name)


@mcp.tool()
def check_service_endpoints(service_name: str, namespace: str) -> str:
    """
    Check whether a service has healthy backing endpoints.
    Useful for diagnosing services with no traffic or broken selectors.
    """
    return _call_direct(_svc_fns, "check_service_endpoints", namespace, service_name)


# ===========================================================================
# ── NAMESPACE TOOLS ─────────────────────────────────────────────────────────
# ===========================================================================

@mcp.tool()
def list_namespaces() -> str:
    """List all Kubernetes namespaces with their status and age."""
    return _call_direct(_ns_fns, "list_kubernetes_namespaces")


@mcp.tool()
def describe_namespace(namespace: str) -> str:
    """Get full details for a namespace: labels, annotations, and resource quotas."""
    return _call_direct(_ns_fns, "describe_namespace", namespace)


@mcp.tool()
def get_namespace_events(namespace: str) -> str:
    """List all events in a namespace, ordered by time."""
    return _call_direct(_ns_fns, "list_namespace_events", namespace)


@mcp.tool()
def get_namespace_warnings(namespace: str) -> str:
    """
    Get only Warning-level events in a namespace.
    Quicker triage view than get_namespace_events.
    """
    return _call_direct(_ns_fns, "get_namespace_warning_events", namespace)


@mcp.tool()
def get_namespace_resource_usage(namespace: str) -> str:
    """Show CPU and memory requests/limits for all pods in a namespace."""
    return _call_direct(_ns_fns, "get_namespace_resource_usage", namespace)


# ===========================================================================
# ── NODE TOOLS ──────────────────────────────────────────────────────────────
# ===========================================================================

@mcp.tool()
def list_nodes() -> str:
    """
    List all Kubernetes cluster nodes with status, roles, Kubernetes version,
    OS, and resource capacity.
    """
    return _call_direct(_node_fns, "list_kubernetes_nodes")


@mcp.tool()
def describe_node(node_name: str) -> str:
    """Get full details for a cluster node: capacity, allocatable, conditions, taints."""
    return _call_direct(_node_fns, "describe_node", node_name)


@mcp.tool()
def get_node_resource_usage() -> str:
    """Show current CPU and memory usage across all cluster nodes (requires metrics-server)."""
    return _call_direct(_node_fns, "get_node_resource_usage")


@mcp.tool()
def cordon_node(node_name: str) -> str:
    """
    Mark a node as unschedulable (cordon). New pods will not be scheduled on it,
    but existing pods continue running. Pair with drain_node to safely remove a node.
    """
    return _call_direct(_node_fns, "cordon_node", node_name)


@mcp.tool()
def uncordon_node(node_name: str) -> str:
    """Re-enable scheduling on a previously cordoned node."""
    return _call_direct(_node_fns, "uncordon_node", node_name)


# ===========================================================================
# ── RBAC TOOLS ──────────────────────────────────────────────────────────────
# ===========================================================================

@mcp.tool()
def list_roles(namespace: str) -> str:
    """List RBAC Roles in a namespace."""
    return _call_direct(_rbac_fns, "list_roles", namespace)


@mcp.tool()
def list_cluster_roles() -> str:
    """List all ClusterRoles in the cluster."""
    return _call_direct(_rbac_fns, "list_cluster_roles")


@mcp.tool()
def list_role_bindings(namespace: str) -> str:
    """List RoleBindings in a namespace — shows who has what role."""
    return _call_direct(_rbac_fns, "list_role_bindings", namespace)


@mcp.tool()
def list_service_accounts(namespace: str) -> str:
    """List ServiceAccounts in a namespace."""
    return _call_direct(_rbac_fns, "list_service_accounts", namespace)


@mcp.tool()
def check_who_can(verb: str, resource: str, namespace: str = "") -> str:
    """
    Check which subjects (users, groups, service accounts) can perform an action.

    Args:
        verb: Kubernetes verb — get, list, create, update, patch, delete, watch.
        resource: Kubernetes resource type — pods, deployments, secrets, etc.
        namespace: Namespace scope; leave empty for cluster-wide check.

    Example: check_who_can("delete", "pods", "production")
    """
    kwargs: dict = {"verb": verb, "resource": resource}
    if namespace:
        kwargs["namespace"] = namespace
    return _call_direct(_rbac_fns, "check_who_can", **kwargs)


# ===========================================================================
# ── METRICS TOOLS ───────────────────────────────────────────────────────────
# ===========================================================================

@mcp.tool()
def get_pod_cpu_usage(namespace: str) -> str:
    """Get current CPU usage for all pods in a namespace (requires metrics-server)."""
    return _call_direct(_metrics_fns, "get_pod_cpu_usage", namespace)


@mcp.tool()
def get_pod_memory_usage(namespace: str) -> str:
    """Get current memory usage for all pods in a namespace (requires metrics-server)."""
    return _call_direct(_metrics_fns, "get_pod_memory_usage", namespace)


@mcp.tool()
def get_cluster_resource_summary() -> str:
    """
    Get a cluster-wide resource summary: total/used CPU and memory across all nodes,
    plus the top resource-consuming namespaces.
    """
    return _call_direct(_metrics_fns, "get_cluster_resource_summary")


# ===========================================================================
# ── TOOL MANAGEMENT ─────────────────────────────────────────────────────────
# ===========================================================================

@mcp.tool()
def list_runtime_tools(status: str = "") -> str:
    """
    List dynamically generated runtime tools registered in KubeIntellect.

    These are Python tools created by the CodeGenerator agent and stored on the
    PVC. They extend KubeIntellect's capabilities beyond its built-in tool set.

    Args:
        status: Filter by status — active, pending, deprecated. Leave empty for all.
    """
    params = {}
    if status:
        params["status"] = status
    result = _call_api("GET", "/v1/tools", params=params)
    return _fmt(result)


@mcp.tool()
def get_runtime_tool(tool_id: str) -> str:
    """Get metadata and source code path for a specific runtime tool by ID."""
    result = _call_api("GET", f"/v1/tools/{tool_id}")
    return _fmt(result)


@mcp.tool()
def get_cluster_health() -> str:
    """
    Check KubeIntellect API health and Kubernetes cluster connectivity.
    Returns status of the API server, PostgreSQL, and Kubernetes API reachability.
    """
    result = _call_api("GET", "/health")
    return _fmt(result)


# ===========================================================================
# ── MCP RESOURCES ───────────────────────────────────────────────────────────
# ===========================================================================

@mcp.resource("k8s://namespaces")
def resource_namespaces() -> str:
    """All Kubernetes namespaces in the cluster."""
    return _call_direct(_ns_fns, "list_kubernetes_namespaces")


@mcp.resource("k8s://nodes")
def resource_nodes() -> str:
    """All Kubernetes nodes with status and capacity."""
    return _call_direct(_node_fns, "list_kubernetes_nodes")


@mcp.resource("k8s://pods/{namespace}")
def resource_pods(namespace: str) -> str:
    """Pods in the given namespace."""
    return _call_direct(_pod_fns, "list_pods_in_namespace", namespace)


@mcp.resource("k8s://deployments/{namespace}")
def resource_deployments(namespace: str) -> str:
    """Deployments in the given namespace."""
    return _call_direct(_deploy_fns, "list_deployments_in_namespace", namespace)


@mcp.resource("k8s://services/{namespace}")
def resource_services(namespace: str) -> str:
    """Services in the given namespace."""
    return _call_direct(_svc_fns, "list_services_in_namespace", namespace)


@mcp.resource("kubeintellect://tools")
def resource_runtime_tools() -> str:
    """Active runtime tools registered in KubeIntellect."""
    result = _call_api("GET", "/v1/tools", params={"status": "active"})
    return _fmt(result)


@mcp.resource("kubeintellect://health")
def resource_health() -> str:
    """KubeIntellect API and cluster connectivity status."""
    result = _call_api("GET", "/health")
    return _fmt(result)


# ===========================================================================
# ── MCP PROMPTS ─────────────────────────────────────────────────────────────
# ===========================================================================

@mcp.prompt()
def debug_pod(pod_name: str, namespace: str) -> str:
    """Generate a prompt to debug a specific pod."""
    return (
        f"Please investigate pod '{pod_name}' in namespace '{namespace}'. "
        f"Check its current status, recent events, and logs (including previous container logs "
        f"if it has restarted). Identify the root cause and suggest a fix."
    )


@mcp.prompt()
def investigate_namespace(namespace: str) -> str:
    """Generate a prompt to investigate the health of a namespace."""
    return (
        f"Please give me a health overview of namespace '{namespace}'. "
        f"Include: pod statuses (any errors/restarts?), recent warning events, "
        f"deployment rollout states, and resource usage. "
        f"Highlight anything that needs attention."
    )


@mcp.prompt()
def cluster_health_check() -> str:
    """Generate a prompt for a full cluster health check."""
    return (
        "Please perform a full cluster health check. Cover: "
        "1) Node status and resource pressure, "
        "2) Pods in error states across all namespaces, "
        "3) Deployments with unavailable replicas, "
        "4) Recent warning events cluster-wide, "
        "5) Overall resource utilization. "
        "Provide a summary and list the top issues."
    )


@mcp.prompt()
def scale_workload(deployment_name: str, namespace: str, replicas: int) -> str:
    """Generate a prompt to scale a deployment."""
    return (
        f"Scale the deployment '{deployment_name}' in namespace '{namespace}' "
        f"to {replicas} replicas. Confirm the current replica count first, "
        f"then perform the scale, and verify the rollout completes successfully."
    )


@mcp.prompt()
def audit_rbac(namespace: str) -> str:
    """Generate a prompt for an RBAC audit of a namespace."""
    return (
        f"Perform an RBAC audit for namespace '{namespace}'. "
        f"List all roles, role bindings, and service accounts. "
        f"Flag any overly-permissive bindings (e.g. cluster-admin, wildcard verbs), "
        f"unused service accounts, or bindings to non-existent subjects."
    )


# ===========================================================================
# ── Entry point ─────────────────────────────────────────────────────────────
# ===========================================================================

if __name__ == "__main__":
    mcp.run()
