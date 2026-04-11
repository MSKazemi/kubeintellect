import json
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

from kubernetes import client
from kubernetes.client.exceptions import ApiException
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.agents.tools.tools_lib._base import (
    get_core_v1_api,
    get_custom_objects_api,
    _handle_k8s_exceptions,
    _parse_cpu_to_millicores,
    _parse_memory_to_mib,
    NoArgumentsInputSchema,
    NamespaceInputSchema,
)
from app.services import kubernetes_service
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")


# ===============================================================================
#                              HELPER FUNCTIONS
# ===============================================================================

def _check_metrics_server_availability(custom_api: client.CustomObjectsApi) -> bool:
    """Check if metrics server is available."""
    try:
        # Try to list node metrics to check if metrics server is available
        custom_api.list_cluster_custom_object(
            group="metrics.k8s.io",
            version="v1beta1",
            plural="nodes"
        )
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise
    except Exception:
        return False


# ===============================================================================
#                               INPUT SCHEMAS
# ===============================================================================

class PodMetricsInputSchema(BaseModel):
    """Schema for pod-specific metrics tools."""
    namespace: str = Field(description="The Kubernetes namespace where the pod is located.")
    pod_name: str = Field(description="The name of the Kubernetes pod.")


class TimeRangeInputSchema(BaseModel):
    """Schema for tools that use time ranges."""
    duration_hours: int = Field(default=24, description="The time range in hours for which to fetch metrics. Defaults to 24 hours.")


class PodResourceTrendsInputSchema(BaseModel):
    """Schema for pod resource trend analysis."""
    pod_name: str = Field(description="The name of the Kubernetes pod.")
    namespace: str = Field(description="The Kubernetes namespace where the pod resides.")
    duration_hours: int = Field(default=24, description="The time range in hours for metrics analysis. Defaults to 24 hours.")


class ConnectivityCheckInputSchema(BaseModel):
    """Schema for connectivity check tool."""
    timeout_seconds: Optional[int] = Field(default=5, description="The timeout for the Kubernetes API call in seconds.")
    max_retries: Optional[int] = Field(default=3, description="The maximum number of retry attempts.")
    retry_delay: Optional[float] = Field(default=1.0, description="The delay between retries in seconds.")


# ===============================================================================
#                            CONNECTIVITY TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def _connectivity_check_func(tool_input: Optional[Dict[str, Any]] = None) -> str:
    """Check connectivity to the Kubernetes cluster by attempting to list nodes."""
    result_dict = kubernetes_service.check_kubernetes_connectivity()
    return json.dumps(result_dict, indent=2)


connectivity_check_tool = StructuredTool.from_function(
    name="kubernetes_connectivity_check",
    func=_connectivity_check_func,
    description="Checks connectivity to the configured Kubernetes cluster. Returns a JSON object with 'status' and 'message'. If successful, also includes 'nodes_count' and a sample of 'nodes'. Useful to verify if KubeIntellect can talk to the cluster before attempting other operations.",
    args_schema=ConnectivityCheckInputSchema
)


# ===============================================================================
#                              MEMORY ANALYSIS TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def analyze_memory_consumption() -> Dict[str, Any]:
    """Analyzes memory consumption across all namespaces in the Kubernetes cluster."""
    core_v1 = get_core_v1_api()
    metrics = {}

    # List all namespaces
    namespaces = core_v1.list_namespace(timeout_seconds=10).items
    
    for ns in namespaces:
        namespace_name = ns.metadata.name
        pod_list = core_v1.list_namespaced_pod(namespace=namespace_name, timeout_seconds=10)
        namespace_memory = 0

        # Calculate memory requests for each pod in the namespace
        for pod in pod_list.items:
            if not pod.spec.containers:
                continue
                
            for container in pod.spec.containers:
                if container.resources and container.resources.requests:
                    memory_request = container.resources.requests.get("memory")
                    if memory_request:
                        namespace_memory += _parse_memory_to_mib(memory_request)

        metrics[namespace_name] = namespace_memory

    # Sort namespaces by memory consumption in descending order
    sorted_metrics = sorted(metrics.items(), key=lambda x: x[1], reverse=True)
    
    # Calculate totals and percentages
    total_memory = sum(metrics.values())
    detailed_metrics = []
    
    for namespace, memory in sorted_metrics:
        percentage = (memory / total_memory * 100) if total_memory > 0 else 0
        detailed_metrics.append({
            "namespace": namespace,
            "memory_requests_mib": round(memory, 2),
            "percentage": round(percentage, 2)
        })
    
    return {
        "status": "success", 
        "data": {
            "namespace_memory_analysis": detailed_metrics,
            "total_memory_requests_mib": round(total_memory, 2),
            "namespace_count": len(namespaces)
        }
    }


analyze_memory_consumption_tool = StructuredTool.from_function(
    func=analyze_memory_consumption,
    name="analyze_memory_consumption",
    description="Analyzes memory requests across all namespaces in the Kubernetes cluster, providing detailed breakdown with percentages and totals.",
    args_schema=NoArgumentsInputSchema
)


@_handle_k8s_exceptions
def get_pod_memory_usage(namespace: str) -> Dict[str, Any]:
    """Fetches current memory usage for all pods in a specified namespace using metrics server."""
    custom_api = get_custom_objects_api()
    
    # Check if metrics server is available
    if not _check_metrics_server_availability(custom_api):
        return {
            "status": "error",
            "message": "Kubernetes Metrics Server is not available. Please install and configure metrics-server.",
            "error_type": "MetricsServerNotFound"
        }
    
    # Fetch pod metrics from metrics server
    metrics = custom_api.list_namespaced_custom_object(
        group="metrics.k8s.io",
        version="v1beta1",
        namespace=namespace,
        plural="pods",
        timeout_seconds=10
    )
    
    pod_memory_usage = {}
    for pod in metrics.get("items", []):
        pod_name = pod.get("metadata", {}).get("name", "unknown")
        total_memory = 0
        
        for container in pod.get("containers", []):
            memory_usage = container.get("usage", {}).get("memory", "0Mi")
            total_memory += _parse_memory_to_mib(memory_usage)
        
        pod_memory_usage[pod_name] = f"{total_memory:.2f}Mi"
    
    # Sort by memory usage
    sorted_pods = sorted(pod_memory_usage.items(), key=lambda x: float(x[1].replace("Mi", "")), reverse=True)
    
    return {
        "status": "success", 
        "data": {
            "namespace": namespace,
            "pod_memory_usage": dict(sorted_pods),
            "pod_count": len(sorted_pods),
            "total_memory_usage_mib": round(sum(float(usage.replace("Mi", "")) for _, usage in sorted_pods), 2)
        }
    }


get_pod_memory_usage_tool = StructuredTool.from_function(
    func=get_pod_memory_usage,
    name="get_pod_memory_usage",
    description="Fetches current memory usage for all pods in a specified namespace from the metrics server, sorted by usage.",
    args_schema=NamespaceInputSchema
)


@_handle_k8s_exceptions
def get_pod_container_memory_usage(namespace: str, pod_name: str) -> Dict[str, Any]:
    """Fetches memory usage for each container in a specific pod."""
    custom_api = get_custom_objects_api()
    
    # Check if metrics server is available
    if not _check_metrics_server_availability(custom_api):
        return {
            "status": "error",
            "message": "Kubernetes Metrics Server is not available. Please install and configure metrics-server.",
            "error_type": "MetricsServerNotFound"
        }
    
    # Fetch specific pod metrics
    metrics = custom_api.get_namespaced_custom_object(
        group="metrics.k8s.io",
        version="v1beta1",
        namespace=namespace,
        plural="pods",
        name=pod_name,
        timeout_seconds=10
    )
    
    containers = metrics.get("containers", [])
    memory_usage = {}
    total_memory = 0
    
    for container in containers:
        container_name = container.get("name")
        memory_str = container.get("usage", {}).get("memory", "0Mi")
        memory_mib = _parse_memory_to_mib(memory_str)
        
        memory_usage[container_name] = {
            "memory_usage": memory_str,
            "memory_usage_mib": round(memory_mib, 2)
        }
        total_memory += memory_mib
    
    return {
        "status": "success", 
        "data": {
            "pod_name": pod_name,
            "namespace": namespace,
            "container_memory_usage": memory_usage,
            "total_pod_memory_mib": round(total_memory, 2),
            "container_count": len(containers)
        }
    }


get_pod_container_memory_usage_tool = StructuredTool.from_function(
    func=get_pod_container_memory_usage,
    name="get_pod_container_memory_usage",
    description="Fetches detailed memory usage metrics for each container in a specific pod with totals and breakdowns.",
    args_schema=PodMetricsInputSchema
)


# ===============================================================================
#                              CPU ANALYSIS TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def get_pod_cpu_usage(namespace: str) -> Dict[str, Any]:
    """Fetches current CPU usage for all pods in a specified namespace."""
    custom_api = get_custom_objects_api()
    
    # Check if metrics server is available
    if not _check_metrics_server_availability(custom_api):
        return {
            "status": "error",
            "message": "Kubernetes Metrics Server is not available. Please install and configure metrics-server.",
            "error_type": "MetricsServerNotFound"
        }
    
    # Fetch pod metrics from metrics server
    metrics = custom_api.list_namespaced_custom_object(
        group="metrics.k8s.io",
        version="v1beta1",
        namespace=namespace,
        plural="pods",
        timeout_seconds=10
    )
    
    pod_cpu_usage = {}
    for pod in metrics.get("items", []):
        pod_name = pod.get("metadata", {}).get("name", "unknown")
        total_cpu_millicores = 0
        
        for container in pod.get("containers", []):
            cpu_usage = container.get("usage", {}).get("cpu", "0")
            total_cpu_millicores += _parse_cpu_to_millicores(cpu_usage)
        
        pod_cpu_usage[pod_name] = round(total_cpu_millicores, 2)
    
    # Sort by CPU usage
    sorted_pods = sorted(pod_cpu_usage.items(), key=lambda x: x[1], reverse=True)
    
    return {
        "status": "success", 
        "data": {
            "namespace": namespace,
            "pod_cpu_usage_millicores": dict(sorted_pods),
            "pod_count": len(sorted_pods),
            "total_cpu_usage_millicores": round(sum(usage for _, usage in sorted_pods), 2)
        }
    }


get_pod_cpu_usage_tool = StructuredTool.from_function(
    func=get_pod_cpu_usage,
    name="get_pod_cpu_usage",
    description="Fetches current CPU usage in millicores for all pods in a specified namespace, sorted by usage with totals.",
    args_schema=NamespaceInputSchema
)


@_handle_k8s_exceptions
def get_pod_container_cpu_usage(namespace: str, pod_name: str) -> Dict[str, Any]:
    """Fetches CPU usage for each container in a specific pod."""
    custom_api = get_custom_objects_api()
    
    # Check if metrics server is available
    if not _check_metrics_server_availability(custom_api):
        return {
            "status": "error",
            "message": "Kubernetes Metrics Server is not available. Please install and configure metrics-server.",
            "error_type": "MetricsServerNotFound"
        }
    
    # Fetch specific pod metrics
    metrics = custom_api.get_namespaced_custom_object(
        group="metrics.k8s.io",
        version="v1beta1",
        namespace=namespace,
        plural="pods",
        name=pod_name,
        timeout_seconds=10
    )
    
    containers = metrics.get("containers", [])
    cpu_usage = {}
    total_cpu = 0
    
    for container in containers:
        container_name = container.get("name")
        cpu_str = container.get("usage", {}).get("cpu", "0")
        cpu_millicores = _parse_cpu_to_millicores(cpu_str)
        
        cpu_usage[container_name] = {
            "cpu_usage": cpu_str,
            "cpu_usage_millicores": round(cpu_millicores, 2)
        }
        total_cpu += cpu_millicores
    
    return {
        "status": "success", 
        "data": {
            "pod_name": pod_name,
            "namespace": namespace,
            "container_cpu_usage": cpu_usage,
            "total_pod_cpu_millicores": round(total_cpu, 2),
            "container_count": len(containers)
        }
    }


get_pod_container_cpu_usage_tool = StructuredTool.from_function(
    func=get_pod_container_cpu_usage,
    name="get_pod_container_cpu_usage",
    description="Fetches detailed CPU usage metrics for each container in a specific pod with totals and breakdowns.",
    args_schema=PodMetricsInputSchema
)


# ===============================================================================
#                            RESOURCE TREND ANALYSIS
# ===============================================================================

@_handle_k8s_exceptions
def get_pod_resource_usage_trends(pod_name: str, namespace: str, duration_hours: int = 24) -> Dict[str, Any]:
    """Fetches current resource usage snapshot for a specific pod (trends require external monitoring)."""
    custom_api = get_custom_objects_api()
    
    # Check if metrics server is available
    if not _check_metrics_server_availability(custom_api):
        return {
            "status": "error",
            "message": "Kubernetes Metrics Server is not available. Please install and configure metrics-server.",
            "error_type": "MetricsServerNotFound"
        }
    
    # Calculate time range
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(hours=duration_hours)
    
    # Fetch current pod metrics (snapshot)
    metrics = custom_api.get_namespaced_custom_object(
        group="metrics.k8s.io",
        version="v1beta1",
        namespace=namespace,
        plural="pods",
        name=pod_name,
        timeout_seconds=10
    )
    
    # Extract resource usage for each container
    container_metrics = []
    total_cpu = 0
    total_memory = 0
    
    for container in metrics.get("containers", []):
        cpu_str = container.get("usage", {}).get("cpu", "0")
        memory_str = container.get("usage", {}).get("memory", "0Mi")
        
        cpu_millicores = _parse_cpu_to_millicores(cpu_str)
        memory_mib = _parse_memory_to_mib(memory_str)
        
        total_cpu += cpu_millicores
        total_memory += memory_mib
        
        container_metrics.append({
            "container_name": container.get("name"),
            "cpu_usage": cpu_str,
            "cpu_usage_millicores": round(cpu_millicores, 2),
            "memory_usage": memory_str,
            "memory_usage_mib": round(memory_mib, 2)
        })
    
    return {
        "status": "success",
        "data": {
            "pod_name": pod_name,
            "namespace": namespace,
            "duration_hours": duration_hours,
            "time_range": {
                "start_time": start_time.isoformat() + "Z",
                "end_time": end_time.isoformat() + "Z"
            },
            "current_usage": {
                "total_cpu_millicores": round(total_cpu, 2),
                "total_memory_mib": round(total_memory, 2),
                "container_count": len(container_metrics)
            },
            "container_metrics": container_metrics,
            "note": "This provides current usage snapshot. Historical trends require external monitoring solutions like Prometheus."
        }
    }


get_pod_resource_usage_trends_tool = StructuredTool.from_function(
    func=get_pod_resource_usage_trends,
    name="get_pod_resource_usage_trends",
    description="Fetches current resource usage snapshot for a specific pod with detailed container breakdowns. Note: Historical trends require external monitoring systems.",
    args_schema=PodResourceTrendsInputSchema
)


# ===============================================================================
#                            NETWORK ANALYSIS TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def measure_network_bandwidth_usage(namespace: str) -> Dict[str, Any]:
    """Measures network bandwidth usage for pods in a namespace (limited by metrics server capabilities)."""
    core_v1 = get_core_v1_api()
    custom_api = get_custom_objects_api()
    
    # Get pods in the specified namespace
    pod_list = core_v1.list_namespaced_pod(namespace=namespace, timeout_seconds=10)
    
    if not pod_list.items:
        return {
            "status": "error", 
            "message": f"No pods found in namespace '{namespace}'", 
            "error_type": "NoPodsFound"
        }
    
    # Check if metrics server is available
    if not _check_metrics_server_availability(custom_api):
        return {
            "status": "warning",
            "message": "Kubernetes Metrics Server is not available. Returning pod information without network metrics.",
            "data": {
                "namespace": namespace,
                "pod_count": len(pod_list.items),
                "pods": [{"name": pod.metadata.name, "status": pod.status.phase} for pod in pod_list.items],
                "network_metrics": "Not available - requires metrics server with network monitoring capability"
            }
        }
    
    bandwidth_usage = {}
    
    for pod in pod_list.items:
        pod_name = pod.metadata.name
        
        try:
            # Try to fetch pod metrics
            metrics = custom_api.get_namespaced_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                namespace=namespace,
                plural="pods",
                name=pod_name,
                timeout_seconds=10
            )
            
            # Standard metrics server doesn't provide network metrics
            # This would require additional monitoring like Prometheus with network plugins
            pod_info = {
                "status": pod.status.phase,
                "containers": len(pod.spec.containers),
                "network_metrics": "Not available in standard metrics server",
                "note": "Network bandwidth monitoring requires specialized monitoring solutions"
            }
            
            # If metrics contain network data (custom metrics server), extract it
            containers = metrics.get("containers", [])
            if containers:
                container_info = []
                for container in containers:
                    usage = container.get("usage", {})
                    container_info.append({
                        "name": container.get("name"),
                        "cpu": usage.get("cpu", "N/A"),
                        "memory": usage.get("memory", "N/A"),
                        "network_rx": usage.get("rx_bytes", "N/A"),
                        "network_tx": usage.get("tx_bytes", "N/A")
                    })
                pod_info["container_metrics"] = container_info
            
            bandwidth_usage[pod_name] = pod_info
            
        except ApiException as e:
            bandwidth_usage[pod_name] = {
                "error": f"Failed to fetch metrics: {e.reason}",
                "status": pod.status.phase
            }
    
    return {
        "status": "success", 
        "data": {
            "namespace": namespace,
            "pod_count": len(pod_list.items),
            "bandwidth_usage": bandwidth_usage,
            "note": "Standard Kubernetes metrics server doesn't provide network bandwidth metrics. Consider Prometheus with network monitoring for detailed bandwidth analysis."
        }
    }


measure_network_bandwidth_usage_tool = StructuredTool.from_function(
    func=measure_network_bandwidth_usage,
    name="measure_network_bandwidth_usage",
    description="Attempts to measure network bandwidth usage for pods in a namespace. Note: Standard metrics server has limited network metrics support.",
    args_schema=NamespaceInputSchema
)


# ===============================================================================
#                            CLUSTER-WIDE ANALYSIS
# ===============================================================================

@_handle_k8s_exceptions
def get_cluster_resource_summary() -> Dict[str, Any]:
    """Provides comprehensive resource summary across the entire cluster."""
    core_v1 = get_core_v1_api()
    custom_api = get_custom_objects_api()
    
    # Get all namespaces
    namespaces = core_v1.list_namespace(timeout_seconds=10).items
    
    cluster_summary = {
        "namespace_count": len(namespaces),
        "total_pods": 0,
        "total_memory_requests_mib": 0,
        "total_cpu_requests_millicores": 0,
        "namespace_breakdown": []
    }
    
    metrics_available = _check_metrics_server_availability(custom_api)
    
    for ns in namespaces:
        namespace_name = ns.metadata.name
        pod_list = core_v1.list_namespaced_pod(namespace=namespace_name, timeout_seconds=10)
        
        namespace_info = {
            "namespace": namespace_name,
            "pod_count": len(pod_list.items),
            "memory_requests_mib": 0,
            "cpu_requests_millicores": 0,
            "memory_usage_mib": 0,
            "cpu_usage_millicores": 0
        }
        
        # Calculate resource requests
        for pod in pod_list.items:
            if not pod.spec.containers:
                continue
                
            for container in pod.spec.containers:
                if container.resources and container.resources.requests:
                    memory_request = container.resources.requests.get("memory")
                    cpu_request = container.resources.requests.get("cpu")
                    
                    if memory_request:
                        memory_mib = _parse_memory_to_mib(memory_request)
                        namespace_info["memory_requests_mib"] += memory_mib
                    
                    if cpu_request:
                        cpu_millicores = _parse_cpu_to_millicores(cpu_request)
                        namespace_info["cpu_requests_millicores"] += cpu_millicores
        
        # Get actual usage if metrics server is available
        if metrics_available:
            try:
                metrics = custom_api.list_namespaced_custom_object(
                    group="metrics.k8s.io",
                    version="v1beta1",
                    namespace=namespace_name,
                    plural="pods",
                    timeout_seconds=5
                )
                
                for pod in metrics.get("items", []):
                    for container in pod.get("containers", []):
                        cpu_usage = container.get("usage", {}).get("cpu", "0")
                        memory_usage = container.get("usage", {}).get("memory", "0Mi")
                        
                        namespace_info["cpu_usage_millicores"] += _parse_cpu_to_millicores(cpu_usage)
                        namespace_info["memory_usage_mib"] += _parse_memory_to_mib(memory_usage)
                        
            except ApiException:
                # If we can't get metrics for this namespace, continue without them
                pass
        
        # Round values
        namespace_info["memory_requests_mib"] = round(namespace_info["memory_requests_mib"], 2)
        namespace_info["cpu_requests_millicores"] = round(namespace_info["cpu_requests_millicores"], 2)
        namespace_info["memory_usage_mib"] = round(namespace_info["memory_usage_mib"], 2)
        namespace_info["cpu_usage_millicores"] = round(namespace_info["cpu_usage_millicores"], 2)
        
        cluster_summary["namespace_breakdown"].append(namespace_info)
        cluster_summary["total_pods"] += namespace_info["pod_count"]
        cluster_summary["total_memory_requests_mib"] += namespace_info["memory_requests_mib"]
        cluster_summary["total_cpu_requests_millicores"] += namespace_info["cpu_requests_millicores"]
    
    # Sort namespaces by resource usage
    cluster_summary["namespace_breakdown"].sort(
        key=lambda x: x["memory_requests_mib"] + x["cpu_requests_millicores"], 
        reverse=True
    )
    
    # Round totals
    cluster_summary["total_memory_requests_mib"] = round(cluster_summary["total_memory_requests_mib"], 2)
    cluster_summary["total_cpu_requests_millicores"] = round(cluster_summary["total_cpu_requests_millicores"], 2)
    cluster_summary["metrics_server_available"] = metrics_available
    
    return {"status": "success", "data": cluster_summary}


get_cluster_resource_summary_tool = StructuredTool.from_function(
    func=get_cluster_resource_summary,
    name="get_cluster_resource_summary",
    description="Provides comprehensive resource analysis across the entire Kubernetes cluster including requests, usage, and namespace-level breakdowns.",
    args_schema=NoArgumentsInputSchema
)


# ===============================================================================
#                           TOOL COLLECTION
# ===============================================================================

# Export all metrics tools for easy import
metrics_tools = [
    connectivity_check_tool,
    analyze_memory_consumption_tool,
    get_pod_memory_usage_tool,
    get_pod_container_memory_usage_tool,
    get_pod_cpu_usage_tool,
    get_pod_container_cpu_usage_tool,
    get_pod_resource_usage_trends_tool,
    measure_network_bandwidth_usage_tool,
    get_cluster_resource_summary_tool,
]
