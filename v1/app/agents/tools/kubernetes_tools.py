"""
Centralized Kubernetes Tools Collection

This module serves as the main entry point for all Kubernetes-related tools.
It imports and aggregates tools from various specialized modules to provide
a single source of truth for Kubernetes operations.

Tool Categories:
- Pod Management: Creation, listing, diagnostics, logs, events
- Deployment Management: CRUD operations, health checks, scaling
- Service Management: Discovery, endpoint checking, external access
- StatefulSet & DaemonSet Management: Persistent workload operations
- Node Management: Cluster node information and health
- Namespace Management: Multi-tenancy operations
- Network Management: Ingress, network policies, connectivity
- Storage Management: PVs, PVCs, storage classes
- Configuration Management: ConfigMaps, secrets, configurations
- Job Management: Batch processing, CronJobs
- Metrics & Monitoring: Resource usage, performance metrics
- Execution Tools: Command execution in pods
"""

from typing import List
from langchain_core.tools import StructuredTool

# Import tool collections from cleaned and organized modules
try:
    from .tools_lib.pod_tools import pod_tools
    POD_TOOLS_AVAILABLE = True
except ImportError as e:
    print(f"Warning: Pod tools not available: {e}")
    pod_tools = []
    POD_TOOLS_AVAILABLE = False

try:
    from .tools_lib.deployment_tools import deployment_tools
    DEPLOYMENT_TOOLS_AVAILABLE = True
except ImportError as e:
    print(f"Warning: Deployment tools not available: {e}")
    deployment_tools = []
    DEPLOYMENT_TOOLS_AVAILABLE = False

try:
    from .tools_lib.services_tools import service_tools
    SERVICE_TOOLS_AVAILABLE = True
except ImportError as e:
    print(f"Warning: Service tools not available: {e}")
    service_tools = []
    SERVICE_TOOLS_AVAILABLE = False

try:
    from .tools_lib.statefulsets_tools import statefulset_daemonset_tools
    STATEFULSET_TOOLS_AVAILABLE = True
except ImportError as e:
    print(f"Warning: StatefulSet/DaemonSet tools not available: {e}")
    statefulset_daemonset_tools = []
    STATEFULSET_TOOLS_AVAILABLE = False

# Import additional tool collections (these may need cleaning up later)
try:
    from .tools_lib.node_tools import node_tools
    NODE_TOOLS_AVAILABLE = True
except ImportError:
    NODE_TOOLS_AVAILABLE = False
    node_tools = []

try:
    from .tools_lib.namespace_tools import namespace_tools  
    NAMESPACE_TOOLS_AVAILABLE = True
except ImportError:
    NAMESPACE_TOOLS_AVAILABLE = False
    namespace_tools = []

try:
    from .tools_lib.networking_tools import networking_tools
    NETWORKING_TOOLS_AVAILABLE = True
except ImportError:
    NETWORKING_TOOLS_AVAILABLE = False
    networking_tools = []

try:
    from .tools_lib.pv_tools import pv_tools
    PV_TOOLS_AVAILABLE = True
except ImportError:
    PV_TOOLS_AVAILABLE = False
    pv_tools = []

try:
    from .tools_lib.configmaps_tools import configmap_tools
    CONFIGMAP_TOOLS_AVAILABLE = True
except ImportError:
    CONFIGMAP_TOOLS_AVAILABLE = False
    configmap_tools = []

try:
    from .tools_lib.execution_tools import execution_tools
    EXECUTION_TOOLS_AVAILABLE = True
except ImportError:
    EXECUTION_TOOLS_AVAILABLE = False
    execution_tools = []


try:
    from .tools_lib.jobs_tools import jobs_tools
    JOB_TOOLS_AVAILABLE = True
except ImportError:
    JOB_TOOLS_AVAILABLE = False
    jobs_tools = []

try:
    from .tools_lib.daemonsets_tools import daemonset_tools
    DAEMONSET_TOOLS_AVAILABLE = True
except ImportError:
    DAEMONSET_TOOLS_AVAILABLE = False
    daemonset_tools = []

try:
    from .tools_lib.metrics_tools import metrics_tools
    METRICS_TOOLS_AVAILABLE = True
except ImportError:
    METRICS_TOOLS_AVAILABLE = False
    metrics_tools = []


try:
    from .tools_lib.replicasets_tools import replicasets_tools
    REPLICASETS_TOOLS_AVAILABLE = True
except ImportError:
    REPLICASETS_TOOLS_AVAILABLE = False
    replicasets_tools = []

try:
    from .tools_lib.apply_tools import apply_tools
    APPLY_TOOLS_AVAILABLE = True
except ImportError:
    APPLY_TOOLS_AVAILABLE = False
    apply_tools = []

try:
    from .tools_lib.prometheus_query_tools import prometheus_query_tools
    PROMETHEUS_QUERY_TOOLS_AVAILABLE = True
except ImportError:
    PROMETHEUS_QUERY_TOOLS_AVAILABLE = False
    prometheus_query_tools = []

try:
    from .tools_lib.log_store_tools import log_store_tools
    LOG_STORE_TOOLS_AVAILABLE = True
except ImportError:
    LOG_STORE_TOOLS_AVAILABLE = False
    log_store_tools = []

try:
    from .tools_lib.rbac_tools import rbac_tools
    RBAC_TOOLS_AVAILABLE = True
except ImportError:
    RBAC_TOOLS_AVAILABLE = False
    rbac_tools = []

try:
    from .tools_lib.security_tools import security_tools
    SECURITY_TOOLS_AVAILABLE = True
except ImportError:
    SECURITY_TOOLS_AVAILABLE = False
    security_tools = []

try:
    from .tools_lib.hpa_tools import hpa_tools
    HPA_TOOLS_AVAILABLE = True
except ImportError:
    HPA_TOOLS_AVAILABLE = False
    hpa_tools = []

try:
    from .tools_lib.quota_tools import quota_tools
    QUOTA_TOOLS_AVAILABLE = True
except ImportError:
    QUOTA_TOOLS_AVAILABLE = False
    quota_tools = []

try:
    from .tools_lib.rollout_tools import rollout_tools
    ROLLOUT_TOOLS_AVAILABLE = True
except ImportError:
    ROLLOUT_TOOLS_AVAILABLE = False
    rollout_tools = []

try:
    from .tools_lib.patch_tools import patch_tools
    PATCH_TOOLS_AVAILABLE = True
except ImportError:
    PATCH_TOOLS_AVAILABLE = False
    patch_tools = []

try:
    from .tools_lib.diagnostics_tools import diagnostics_tools
    DIAGNOSTICS_TOOLS_AVAILABLE = True
except ImportError:
    DIAGNOSTICS_TOOLS_AVAILABLE = False
    diagnostics_tools = []

try:
    from .tools_lib.risk_score_tools import risk_score_tools
    RISK_SCORE_TOOLS_AVAILABLE = True
except ImportError:
    RISK_SCORE_TOOLS_AVAILABLE = False
    risk_score_tools = []


# ===============================================================================
#                           TOOL COLLECTIONS BY CATEGORY
# ===============================================================================

def get_pod_tools() -> List[StructuredTool]:
    """Get all pod-related tools."""
    if POD_TOOLS_AVAILABLE:
        return pod_tools
    return []

def get_deployment_tools() -> List[StructuredTool]:
    """Get all deployment-related tools."""
    if DEPLOYMENT_TOOLS_AVAILABLE:
        return deployment_tools
    return []

def get_service_tools() -> List[StructuredTool]:
    """Get all service-related tools."""
    if SERVICE_TOOLS_AVAILABLE:
        return service_tools
    return []

def get_statefulset_daemonset_tools() -> List[StructuredTool]:
    """Get all StatefulSet and DaemonSet tools."""
    if STATEFULSET_TOOLS_AVAILABLE:
        return statefulset_daemonset_tools
    return []

def get_node_tools() -> List[StructuredTool]:
    """Get all node-related tools."""
    if NODE_TOOLS_AVAILABLE:
        return node_tools
    return []

def get_namespace_tools() -> List[StructuredTool]:
    """Get all namespace-related tools."""
    if NAMESPACE_TOOLS_AVAILABLE:
        return namespace_tools
    return []

def get_networking_tools() -> List[StructuredTool]:
    """Get all networking-related tools."""
    if NETWORKING_TOOLS_AVAILABLE:
        return networking_tools
    return []

def get_storage_tools() -> List[StructuredTool]:
    """Get all storage-related tools (PVs, PVCs)."""
    if PV_TOOLS_AVAILABLE:
        return pv_tools
    return []

def get_config_tools() -> List[StructuredTool]:
    """Get all configuration-related tools (ConfigMaps, Secrets)."""
    if CONFIGMAP_TOOLS_AVAILABLE:
        return configmap_tools
    return []

def get_execution_tools() -> List[StructuredTool]:
    """Get all execution-related tools."""
    if EXECUTION_TOOLS_AVAILABLE:
        return execution_tools
    return []


def get_job_tools() -> List[StructuredTool]:
    """Get all job and batch processing tools."""
    if JOB_TOOLS_AVAILABLE:
        return jobs_tools
    return []


def get_daemonset_tools() -> List[StructuredTool]:
    """Get all DaemonSet management tools."""
    if DAEMONSET_TOOLS_AVAILABLE:
        return daemonset_tools
    return []


def get_replicaset_tools() -> List[StructuredTool]:
    """Get all ReplicaSet management tools."""
    if REPLICASETS_TOOLS_AVAILABLE:
        return replicasets_tools
    return []

def get_metrics_tools() -> List[StructuredTool]:
    """Get all metrics and monitoring tools."""
    if METRICS_TOOLS_AVAILABLE:
        return metrics_tools
    return []

def get_apply_tools() -> List[StructuredTool]:
    """Get all apply-related tools."""
    if APPLY_TOOLS_AVAILABLE:
        return apply_tools
    return []


# ===============================================================================
#                           COMPREHENSIVE TOOL COLLECTION
# ===============================================================================

def get_core_k8s_tools() -> List[StructuredTool]:
    """
    Get core Kubernetes tools that are cleaned and ready for production use.
    These are the essential tools that have been organized and optimized.
    """
    tools = []
    
    # Add cleaned and organized tools
    tools.extend(get_pod_tools())                    # 12 pod management tools
    tools.extend(get_deployment_tools())             # 8 deployment management tools  
    tools.extend(get_service_tools())                # 6 service management tools
    tools.extend(get_statefulset_daemonset_tools())  # 9 StatefulSet/DaemonSet tools
    
    return tools

def get_extended_k8s_tools() -> List[StructuredTool]:
    """
    Get extended Kubernetes tools including additional functionality.
    These tools may need further organization and cleanup.
    """
    tools = []
    
    # Start with core tools
    tools.extend(get_core_k8s_tools())
    
    # Add extended tools (these may need cleanup)
    tools.extend(get_node_tools())
    tools.extend(get_namespace_tools())
    tools.extend(get_networking_tools())
    tools.extend(get_storage_tools())
    tools.extend(get_config_tools())
    tools.extend(get_execution_tools())
    tools.extend(get_daemonset_tools())
    tools.extend(get_replicaset_tools())
    tools.extend(get_apply_tools())
    return tools

def get_all_k8s_tools() -> List[StructuredTool]:
    """
    Get ALL available Kubernetes tools including experimental and monitoring tools.
    This is the complete collection for advanced use cases.
    """
    tools = []

    # Start with extended tools
    tools.extend(get_extended_k8s_tools())

    # Add remaining tools (apply_tools already included via get_extended_k8s_tools)
    tools.extend(get_job_tools())
    tools.extend(get_metrics_tools())
    tools.extend(hpa_tools)
    tools.extend(quota_tools)
    return tools


# ===============================================================================
#                           TOOL AVAILABILITY STATUS
# ===============================================================================

def get_tool_availability_status() -> dict:
    """Get the availability status of all tool categories."""
    return {
        "pod_tools": POD_TOOLS_AVAILABLE,
        "deployment_tools": DEPLOYMENT_TOOLS_AVAILABLE,
        "service_tools": SERVICE_TOOLS_AVAILABLE,
        "statefulset_tools": STATEFULSET_TOOLS_AVAILABLE,
        "node_tools": NODE_TOOLS_AVAILABLE,
        "namespace_tools": NAMESPACE_TOOLS_AVAILABLE,
        "networking_tools": NETWORKING_TOOLS_AVAILABLE,
        "storage_tools": PV_TOOLS_AVAILABLE,
        "config_tools": CONFIGMAP_TOOLS_AVAILABLE,
        "execution_tools": EXECUTION_TOOLS_AVAILABLE,
        "daemonset_tools": DAEMONSET_TOOLS_AVAILABLE,
        "replicaset_tools": REPLICASETS_TOOLS_AVAILABLE,
        "job_tools": JOB_TOOLS_AVAILABLE,
        "metrics_tools": METRICS_TOOLS_AVAILABLE,
        "apply_tools": APPLY_TOOLS_AVAILABLE,
    }

def print_tool_summary():
    """Print a summary of available tools."""
    status = get_tool_availability_status()
    core_tools = get_core_k8s_tools()
    extended_tools = get_extended_k8s_tools()
    all_k8s_tools = get_all_k8s_tools()
    
    print("\n" + "="*60)
    print("         KUBERNETES TOOLS SUMMARY")
    print("="*60)
    
    print("\n📊 Tool Collections Status:")
    for category, available in status.items():
        status_icon = "✅" if available else "❌"
        print(f"  {status_icon} {category.replace('_', ' ').title()}")
    
    print("\n🛠️  Tool Counts:")
    print(f"  • Core K8s Tools (cleaned & ready): {len(core_tools)}")
    print(f"  • Extended K8s Tools: {len(extended_tools)}")
    print(f"  • All K8s Tools: {len(all_k8s_tools)}")
    
    print("\n🎯 Recommended Usage:")
    print("  • For production: use get_core_k8s_tools()")
    print("  • For comprehensive: use get_extended_k8s_tools()")
    print("  • For everything: use get_all_k8s_tools()")
    
    print("="*60 + "\n")


# ===============================================================================
#                           DEFAULT EXPORTS
# ===============================================================================


core_kubernetes_tools = get_core_k8s_tools()
extended_kubernetes_tools = get_extended_k8s_tools()
all_kubernetes_tools = get_all_k8s_tools()
all_k8s_tools = all_kubernetes_tools
k8s_config_tools = get_config_tools()
k8s_apply_tools = get_apply_tools()


def _only_delete_tools(tool_list: list) -> list:
    """Return only tools whose names begin with 'delete_' or 'cleanup_'.

    This scopes the Deletion agent to delete-only operations, preventing it
    from inadvertently calling create/scale/describe tools that happen to live
    in the same module-level list as the delete tools.
    """
    return [t for t in tool_list if t.name.startswith(("delete_", "cleanup_"))]


# Namespace tools that are relevant to log/event queries (no create/patch/delete ops).
_LOGS_NAMESPACE_TOOL_NAMES = {
    "list_kubernetes_namespaces",
    "list_namespace_events",
    "get_namespace_warning_events",
    "count_pods_in_namespaces",
}
_logs_namespace_tools = [t for t in namespace_tools if t.name in _LOGS_NAMESPACE_TOOL_NAMES]

# Namespace tools relevant to lifecycle (deployment-scoped event queries + namespace creation).
# create_kubernetes_namespace is required so Lifecycle can follow the suggested_action hint
# returned by create_kubernetes_deployment on NamespaceNotFound — without it, the agent
# cannot act on the hint and retries blindly, causing a dispatch loop.
_LIFECYCLE_NAMESPACE_TOOL_NAMES = {"get_deployment_events", "create_kubernetes_namespace"}
_lifecycle_namespace_tools = [t for t in namespace_tools if t.name in _LIFECYCLE_NAMESPACE_TOOL_NAMES]

# Diagnostics tools split by destination agent
_METRICS_DIAGNOSTICS_TOOL_NAMES = {"top_nodes", "top_pods"}
_LOGS_DIAGNOSTICS_TOOL_NAMES = {"events_watch"}
_LIFECYCLE_DIAGNOSTICS_TOOL_NAMES = {"describe_resource"}
_diagnostics_metrics = [t for t in diagnostics_tools if t.name in _METRICS_DIAGNOSTICS_TOOL_NAMES]
_diagnostics_logs = [t for t in diagnostics_tools if t.name in _LOGS_DIAGNOSTICS_TOOL_NAMES]
_diagnostics_lifecycle = [t for t in diagnostics_tools if t.name in _LIFECYCLE_DIAGNOSTICS_TOOL_NAMES]

# Pod tools subset for the Logs agent: excludes list_all_pods_across_namespaces
# because listing all pods is a lifecycle/enumeration operation, not a log-retrieval
# operation.  Giving Logs agent access to it causes it to enumerate pods instead of
# fetching logs, producing noisy results.
_LOGS_POD_TOOL_NAMES = {
    t.name for t in pod_tools
    if t.name != "list_all_pods_across_namespaces"
}
_logs_pod_tools = [t for t in pod_tools if t.name in _LOGS_POD_TOOL_NAMES]

# Specialized agent tool sets
k8s_logs_tools = _logs_pod_tools + log_store_tools + _logs_namespace_tools + _diagnostics_logs
k8s_metrics_tools = metrics_tools + prometheus_query_tools + _diagnostics_metrics
k8s_rbac_tools = rbac_tools
k8s_security_tools = security_tools
# Lifecycle agent: exclude delete/cleanup tools — those belong exclusively to the Deletion agent.
# Lifecycle should only verify, scale, rollout, and wait — never delete.
k8s_lifecycle_tools = [
    t for t in (
        pod_tools + deployment_tools + statefulset_daemonset_tools + daemonset_tools
        + hpa_tools + node_tools + rollout_tools + patch_tools
        + _lifecycle_namespace_tools + _diagnostics_lifecycle + risk_score_tools
    )
    if not t.name.startswith(("delete_", "cleanup_"))
]
k8s_execution_tools = execution_tools

# Deletion agent: ONLY delete/cleanup tools — no create, scale, or describe tools.
k8s_deletion_tools = (
    _only_delete_tools(pod_tools)
    + _only_delete_tools(deployment_tools)
    + _only_delete_tools(service_tools)
    + _only_delete_tools(statefulset_daemonset_tools)
    + _only_delete_tools(daemonset_tools)
    + _only_delete_tools(jobs_tools)
    + _only_delete_tools(configmap_tools)
    + _only_delete_tools(rbac_tools)
    + _only_delete_tools(namespace_tools)
    + _only_delete_tools(networking_tools)
    + _only_delete_tools(pv_tools)
    + _only_delete_tools(hpa_tools)
    + _only_delete_tools(quota_tools)
    + _only_delete_tools(apply_tools)   # delete_from_yaml for bulk manifest removal
)

# Read-only pod tools for Infrastructure: describe_pod and list_pods so Infrastructure can
# check pod labels during NetworkPolicy connectivity investigations (e.g. "client missing role=frontend").
_INFRA_POD_TOOL_NAMES = {"describe_kubernetes_pod", "list_pods_in_namespace", "check_pod_exists"}
_infra_pod_tools = [t for t in pod_tools if t.name in _INFRA_POD_TOOL_NAMES]

# Infrastructure agent: exclude delete/cleanup tools — those belong exclusively to the Deletion agent.
# Infrastructure should create/describe/check services, networking, storage, namespaces — never delete.
k8s_advancedops_tools = [
    t for t in (
        service_tools + networking_tools + pv_tools + namespace_tools + node_tools + jobs_tools + quota_tools
    )
    if not t.name.startswith(("delete_", "cleanup_"))
] + _infra_pod_tools


# ===============================================================================
#                           MODULE INITIALIZATION
# ===============================================================================

if __name__ == "__main__":
    # Print summary when run directly
    print_tool_summary()
