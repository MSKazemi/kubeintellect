import json
from typing import List, Dict, Any, Optional

from kubernetes import client
from kubernetes.client.exceptions import ApiException
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.agents.tools.tools_lib._base import (
    get_core_v1_api,
    _handle_k8s_exceptions,
    NoArgumentsInputSchema,
    NamespaceInputSchema,
    NamespaceOptionalInputSchema,
)
from app.core.config import settings
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")


def resolve_pod(core_v1, namespace: str, pod_name: str):
    """Resolve a pod name to an actual pod object, with prefix fallback.

    Users often provide a deployment/workload name (e.g. "my-app") rather than
    the full pod name ("my-app-7bf6fd8cd-zcxsd"). This helper tries an exact
    lookup first, then falls back to a prefix search so callers work regardless
    of whether the user gave the deployment name or the full pod name.

    Returns the V1Pod object, or raises ApiException(404) if nothing matches.
    """
    try:
        return core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
    except ApiException as e:
        if e.status == 404:
            pods = core_v1.list_namespaced_pod(namespace=namespace)
            prefix = pod_name + "-"
            matches = [p for p in pods.items if p.metadata.name.startswith(prefix)]
            if matches:
                return matches[0]
        raise


# ===============================================================================
#                               INPUT SCHEMAS
# ===============================================================================

class PodInputSchema(BaseModel):
    """Schema for tools that require pod name and namespace."""
    namespace: str = Field(description="The Kubernetes namespace where the pod is located.")
    pod_name: str = Field(description="The name of the Kubernetes pod.")


class CreatePodInputSchema(BaseModel):
    """Schema for creating Kubernetes pods."""
    namespace: str = Field(description="The Kubernetes namespace where the pod will be created.")
    pod_name: str = Field(description="The name of the pod to be created. It will be converted to a valid RFC 1123 compliant name.")
    image: str = Field(description="The container image to use for the pod.")
    command: Optional[List[str]] = Field(default=None, description="Optional command to run in the container, e.g. ['sleep', '3600'].")
    ports: Optional[List[int]] = Field(default=None, description="Optional list of container ports to expose, e.g. [80, 443].")


class DeletePodInputSchema(BaseModel):
    """Schema for deleting pods from multiple namespaces."""
    pod_name: str = Field(description="The name of the Kubernetes pod to delete.")
    namespaces: List[str] = Field(description="A list of Kubernetes namespaces from which the pod should be deleted.")


class LabelSelectorInputSchema(BaseModel):
    """Schema for tools that use label selectors."""
    label_selector: str = Field(description="The label selector to filter Kubernetes resources, specified as a key-value pair (e.g., 'k8s-app=kube-dns').")
    namespace: Optional[str] = Field(default=None, description="Optional namespace to scope the search. If omitted, searches all namespaces.")


class PodLogsInputSchema(BaseModel):
    """Schema for getting pod logs."""
    name: str = Field(description="The name of the pod to get logs for.")
    namespace: str = Field(description="The namespace of the pod.")
    tail_lines: Optional[int] = Field(default=50, description="The number of lines from the end of the logs to show.")
    previous: Optional[bool] = Field(default=False, description="If True, fetch logs from the previous instance of the container.")
    container: Optional[str] = Field(default=None, description="The name of the container to fetch logs from.")


class PodDiagnosticsInputSchema(BaseModel):
    """Schema for pod diagnostics."""
    pod_name: str = Field(description="The name of the pod to get diagnostics for.")
    namespace: str = Field(description="The namespace of the pod.")
    tail_lines: Optional[int] = Field(default=100, description="The number of lines from the end of the logs to show.")


# ===============================================================================
#                                POD TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def list_all_pods() -> Dict[str, Any]:
    """Lists all pods across all namespaces."""
    core_v1 = get_core_v1_api()
    pods = core_v1.list_pod_for_all_namespaces(timeout_seconds=10)
    pod_list = [
        {
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "status": pod.status.phase,
            "node": pod.spec.node_name,
        }
        for pod in pods.items
    ]
    return {"status": "success", "total_count": len(pod_list), "data": pod_list}


list_all_pods_tool = StructuredTool.from_function(
    func=list_all_pods,
    name="list_all_pods_across_namespaces",
    description="Lists all Kubernetes pods across all namespaces with their basic information including name, namespace, status, and node.",
    args_schema=NoArgumentsInputSchema
)


@_handle_k8s_exceptions
def list_pods_in_namespace(namespace: str) -> Dict[str, Any]:
    """Lists all pods in a specific namespace."""
    core_v1 = get_core_v1_api()
    pods = core_v1.list_namespaced_pod(namespace=namespace, timeout_seconds=10)
    pod_names = [pod.metadata.name for pod in pods.items]
    return {"status": "success", "total_count": len(pod_names), "data": pod_names}


list_pods_in_namespace_tool = StructuredTool.from_function(
    func=list_pods_in_namespace,
    name="list_pods_in_namespace",
    description="Lists all pod names in a specified Kubernetes namespace.",
    args_schema=NamespaceInputSchema
)


@_handle_k8s_exceptions
def describe_kubernetes_pod(namespace: str, pod_name: str) -> Dict[str, Any]:
    """Retrieves detailed information about a specific Kubernetes pod."""
    core_v1 = get_core_v1_api()
    pod = resolve_pod(core_v1, namespace, pod_name)

    # Extract container information
    containers = []
    for container in pod.spec.containers:
        containers.append({
            "name": container.name,
            "image": container.image,
            "resources": container.resources.to_dict() if container.resources else {},
            "ports": [{"name": port.name, "port": port.container_port} for port in container.ports] if container.ports else []
        })
    
    def _extract_container_state(state_obj):
        """Extract a structured dict from a V1ContainerState."""
        if state_obj is None:
            return {}
        result = {}
        if state_obj.running:
            result["running"] = {"started_at": str(state_obj.running.started_at)}
        if state_obj.waiting:
            result["waiting"] = {
                "reason": state_obj.waiting.reason,
                "message": state_obj.waiting.message,
            }
        if state_obj.terminated:
            result["terminated"] = {
                "exit_code": state_obj.terminated.exit_code,
                "reason": state_obj.terminated.reason,
                "message": state_obj.terminated.message,
                "started_at": str(state_obj.terminated.started_at),
                "finished_at": str(state_obj.terminated.finished_at),
            }
        return result

    # Extract status information
    container_statuses = []
    if pod.status.container_statuses:
        for status in pod.status.container_statuses:
            container_statuses.append({
                "name": status.name,
                "ready": status.ready,
                "restart_count": status.restart_count,
                "state": _extract_container_state(status.state),
                "last_state": _extract_container_state(status.last_state),
            })
    
    pod_description = {
        "name": pod.metadata.name,
        "namespace": pod.metadata.namespace,
        "labels": pod.metadata.labels or {},
        "annotations": pod.metadata.annotations or {},
        "node_name": pod.spec.node_name,
        "phase": pod.status.phase,
        "conditions": [{"type": c.type, "status": c.status} for c in pod.status.conditions] if pod.status.conditions else [],
        "containers": containers,
        "container_statuses": container_statuses,
        "creation_timestamp": pod.metadata.creation_timestamp.isoformat() if pod.metadata.creation_timestamp else None
    }
    
    return {"status": "success", "data": pod_description}


describe_kubernetes_pod_tool = StructuredTool.from_function(
    func=describe_kubernetes_pod,
    name="describe_kubernetes_pod",
    description="Retrieves detailed information about a specific Kubernetes pod including containers, status, and metadata.",
    args_schema=PodInputSchema
)


@_handle_k8s_exceptions
def create_pod(namespace: str, pod_name: str, image: str, command: Optional[List[str]] = None, ports: Optional[List[int]] = None) -> Dict[str, Any]:
    """Creates a Kubernetes pod with a single container, optionally with a command and exposed ports."""
    # Ensure pod name is RFC 1123 compliant
    safe_name = pod_name.replace("_", "-").lower()

    container_ports = [client.V1ContainerPort(container_port=p) for p in ports] if ports else None

    # Define the container
    container = client.V1Container(
        name=safe_name,
        image=image,
        command=command if command else None,
        ports=container_ports,
    )

    # Define the pod spec
    pod_spec = client.V1PodSpec(containers=[container])

    # Define the pod
    pod = client.V1Pod(
        api_version="v1",
        kind="Pod",
        metadata=client.V1ObjectMeta(name=safe_name, namespace=namespace),
        spec=pod_spec
    )

    # Create the pod
    core_v1 = get_core_v1_api()
    response = core_v1.create_namespaced_pod(namespace=namespace, body=pod)

    return {"status": "success", "data": {"name": response.metadata.name, "namespace": response.metadata.namespace}}


create_pod_tool = StructuredTool.from_function(
    func=create_pod,
    name="create_kubernetes_pod",
    description="Creates a Kubernetes pod with a single container. Supports optional command override (e.g. ['sleep', '3600']) and port exposure.",
    args_schema=CreatePodInputSchema
)


@_handle_k8s_exceptions
def delete_pod_from_namespaces(pod_name: str, namespaces: List[str]) -> Dict[str, Any]:
    """Deletes a pod from multiple namespaces."""
    if pod_name.strip() in ("*", "all", "ALL"):
        return {
            "status": "error",
            "message": (
                "Wildcard deletion is not supported. "
                "List pods first with list_pods_in_namespace, then provide explicit pod names."
            ),
        }

    core_v1 = get_core_v1_api()
    results = []

    for namespace in namespaces:
        try:
            core_v1.delete_namespaced_pod(name=pod_name, namespace=namespace)
            results.append({"namespace": namespace, "status": "deleted"})
        except ApiException as e:
            if e.status == 404:
                results.append({"namespace": namespace, "status": "not_found"})
            else:
                results.append({"namespace": namespace, "status": "error", "error": str(e)})

    deleted = [r for r in results if r["status"] == "deleted"]
    not_found = [r for r in results if r["status"] == "not_found"]
    if not deleted and not_found:
        ns_list = ", ".join(r["namespace"] for r in not_found)
        return {
            "status": "error",
            "message": (
                f"No pod named '{pod_name}' found in namespace(s): {ns_list}. "
                "List pods first to verify the exact name."
            ),
        }

    return {"status": "success", "data": results}


delete_pod_from_namespaces_tool = StructuredTool.from_function(
    func=delete_pod_from_namespaces,
    name="delete_pod_from_namespaces",
    description="Deletes a specified pod from multiple Kubernetes namespaces.",
    args_schema=DeletePodInputSchema
)


@_handle_k8s_exceptions
def list_error_pods(namespace: Optional[str] = None) -> Dict[str, Any]:
    """Lists pods that are in error states (Failed, CrashLoopBackOff, etc.)."""
    core_v1 = get_core_v1_api()
    
    if namespace:
        pods = core_v1.list_namespaced_pod(namespace=namespace, timeout_seconds=10)
    else:
        pods = core_v1.list_pod_for_all_namespaces(timeout_seconds=10)
    
    error_pods = []
    error_states = ["Failed", "Pending", "Unknown"]

    for pod in pods.items:
        pod_phase = pod.status.phase
        issues = []

        if pod_phase in error_states:
            issues.append({"type": "phase_error", "reason": f"Pod in error phase: {pod_phase}"})

        if pod.status.container_statuses:
            for container_status in pod.status.container_statuses:
                if container_status.restart_count > 0:
                    issues.append({
                        "type": "restart",
                        "container": container_status.name,
                        "restart_count": container_status.restart_count,
                        "reason": "Container has restarted",
                    })
                if not container_status.ready:
                    issues.append({
                        "type": "not_ready",
                        "container": container_status.name,
                        "reason": "Container not ready",
                    })

        if issues:
            error_pods.append({
                "name": pod.metadata.name,
                "namespace": pod.metadata.namespace,
                "phase": pod_phase,
                "issues": issues,
            })

    return {"status": "success", "total_count": len(error_pods), "data": error_pods}


list_error_pods_tool = StructuredTool.from_function(
    func=list_error_pods,
    name="list_error_pods",
    description="Lists pods that are in error states including failed, pending, crashed, or not ready containers.",
    args_schema=NamespaceOptionalInputSchema
)


@_handle_k8s_exceptions
def list_pods_with_two_containers(namespace: str) -> Dict[str, Any]:
    """Lists pods that have exactly two containers."""
    core_v1 = get_core_v1_api()
    pods = core_v1.list_namespaced_pod(namespace=namespace, timeout_seconds=10)
    
    two_container_pods = []
    for pod in pods.items:
        if len(pod.spec.containers) == 2:
            container_names = [container.name for container in pod.spec.containers]
            two_container_pods.append({
                "name": pod.metadata.name,
                "containers": container_names
            })
    
    return {"status": "success", "total_count": len(two_container_pods), "data": two_container_pods}


list_pods_with_two_containers_tool = StructuredTool.from_function(
    func=list_pods_with_two_containers,
    name="list_pods_with_two_containers",
    description="Lists pods in a namespace that have exactly two containers.",
    args_schema=NamespaceInputSchema
)


@_handle_k8s_exceptions
def get_pod_events(namespace: str, pod_name: str) -> Dict[str, Any]:
    """Gets events related to a specific pod."""
    core_v1 = get_core_v1_api()
    
    # Get all events in the namespace and filter for the pod
    events = core_v1.list_namespaced_event(namespace=namespace, timeout_seconds=10)
    
    pod_events = []
    for event in events.items:
        if (event.involved_object.name == pod_name and 
            event.involved_object.kind == "Pod"):
            pod_events.append({
                "type": event.type,
                "reason": event.reason,
                "message": event.message,
                "first_timestamp": event.first_timestamp.isoformat() if event.first_timestamp else None,
                "last_timestamp": event.last_timestamp.isoformat() if event.last_timestamp else None,
                "count": event.count
            })
    
    return {"status": "success", "data": pod_events}


get_pod_events_tool = StructuredTool.from_function(
    func=get_pod_events,
    name="get_pod_events",
    description="Gets events related to a specific pod for troubleshooting purposes.",
    args_schema=PodInputSchema
)


@_handle_k8s_exceptions
def get_pod_logs(name: str, namespace: str, tail_lines: int = 50, previous: bool = False, 
                 container: Optional[str] = None) -> str:
    """Gets logs from a specific pod."""
    core_v1 = get_core_v1_api()
    
    try:
        logs = core_v1.read_namespaced_pod_log(
            name=name,
            namespace=namespace,
            tail_lines=tail_lines,
            previous=previous,
            container=container,
            _request_timeout=settings.K8S_API_TIMEOUT_SECONDS,
        )
        return json.dumps({
            "status": "success",
            "data": {
                "pod": name,
                "namespace": namespace,
                "container": container or "default",
                "logs": logs,
                "previous": previous,
            }
        }, indent=2)
    except Exception as e:
        err_str = str(e)
        # Containerd GC can remove the previous container's log buffer before it is
        # read.  When previous=True fails with a CRI/containerd error, retry once
        # with previous=False to capture the current container's logs — for fast-
        # crashing pods (CrashLoopBackOff), the current container's stdout will also
        # contain the fatal error message.
        if previous and ("unable to retrieve container logs" in err_str or "containerd" in err_str.lower()):
            try:
                logs = core_v1.read_namespaced_pod_log(
                    name=name,
                    namespace=namespace,
                    tail_lines=tail_lines,
                    previous=False,
                    container=container,
                    _request_timeout=settings.K8S_API_TIMEOUT_SECONDS,
                )
                return json.dumps({
                    "status": "success",
                    "data": {
                        "pod": name,
                        "namespace": namespace,
                        "container": container or "default",
                        "logs": logs,
                        "previous": False,
                        "note": "previous=True failed (containerd GC); showing current container logs",
                    }
                }, indent=2)
            except Exception as e2:
                return json.dumps({
                    "status": "error",
                    "message": f"Failed to get logs (previous=True: {err_str}; current: {e2})",
                    "pod": name,
                    "namespace": namespace,
                }, indent=2)
        return json.dumps({
            "status": "error",
            "message": f"Failed to get logs: {err_str}",
            "pod": name,
            "namespace": namespace,
        }, indent=2)


get_pod_logs_tool = StructuredTool.from_function(
    name="get_pod_logs",
    func=get_pod_logs,
    description="Gets logs from a specific Kubernetes pod, with options for tail lines, previous container, and specific container selection.",
    args_schema=PodLogsInputSchema
)


@_handle_k8s_exceptions
def get_pod_diagnostics(pod_name: str, namespace: str, tail_lines: int = 100) -> str:
    """Gets comprehensive diagnostics for a pod including status, events, and logs."""
    result = {
        "pod_name": pod_name,
        "namespace": namespace,
        "diagnostics": {}
    }
    
    # Get pod description
    pod_desc_result = describe_kubernetes_pod(namespace, pod_name)
    if pod_desc_result["status"] == "success":
        result["diagnostics"]["pod_info"] = pod_desc_result["data"]
    
    # Get pod events
    events_result = get_pod_events(namespace, pod_name)
    if events_result["status"] == "success":
        result["diagnostics"]["events"] = events_result["data"]
    
    # Get pod logs (attempt for main container)
    logs_result = json.loads(get_pod_logs(pod_name, namespace, tail_lines))
    if logs_result["status"] == "success":
        result["diagnostics"]["logs"] = logs_result["data"]["logs"]
    
    return json.dumps({"status": "success", "data": result}, indent=2)


get_pod_diagnostics_tool = StructuredTool.from_function(
    name="get_pod_diagnostics",
    func=get_pod_diagnostics,
    description="Gets comprehensive diagnostics for a pod including detailed information, events, and recent logs.",
    args_schema=PodDiagnosticsInputSchema
)


@_handle_k8s_exceptions
def check_pods_using_deprecated_apis(namespace: str) -> Dict[str, Any]:
    """Checks for pods that might be using deprecated API versions."""
    core_v1 = get_core_v1_api()
    pods = core_v1.list_namespaced_pod(namespace=namespace, timeout_seconds=10)
    
    deprecated_pods = []
    
    for pod in pods.items:
        # Check annotations for deprecated API usage
        annotations = pod.metadata.annotations or {}
        
        # Look for kubectl last-applied-configuration
        if "kubectl.kubernetes.io/last-applied-configuration" in annotations:
            try:
                config = json.loads(annotations["kubectl.kubernetes.io/last-applied-configuration"])
                api_version = config.get("apiVersion", "")
                
                # Check for older API versions
                if "v1beta1" in api_version or "v1alpha1" in api_version:
                    deprecated_pods.append({
                        "name": pod.metadata.name,
                        "api_version": api_version,
                        "reason": "Using deprecated API version"
                    })
            except json.JSONDecodeError:
                pass
        
        # Check labels for deprecated patterns
        labels = pod.metadata.labels or {}
        if "version" in labels and labels["version"] in ["beta", "alpha"]:
            deprecated_pods.append({
                "name": pod.metadata.name,
                "version_label": labels["version"],
                "reason": "Using deprecated version label"
            })
    
    return {"status": "success", "data": deprecated_pods}


check_pods_using_deprecated_apis_tool = StructuredTool.from_function(
    func=check_pods_using_deprecated_apis,
    name="check_pods_using_deprecated_apis",
    description="Checks for pods that might be using deprecated Kubernetes API versions or patterns.",
    args_schema=NamespaceInputSchema
)


@_handle_k8s_exceptions
def list_resources_with_label(label_selector: str, namespace: Optional[str] = None) -> Dict[str, Any]:
    """Lists pods that match a specific label selector, optionally scoped to a namespace."""
    core_v1 = get_core_v1_api()

    if namespace:
        pods = core_v1.list_namespaced_pod(
            namespace=namespace,
            label_selector=label_selector,
            timeout_seconds=10,
        )
    else:
        pods = core_v1.list_pod_for_all_namespaces(
            label_selector=label_selector,
            timeout_seconds=10,
        )

    matching_pods = [
        {
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "labels": pod.metadata.labels or {},
            "status": pod.status.phase,
        }
        for pod in pods.items
    ]
    return {"status": "success", "total_count": len(matching_pods), "data": matching_pods}


list_resources_with_label_tool = StructuredTool.from_function(
    func=list_resources_with_label,
    name="list_pods_with_label",
    description="Lists pods that match a specific label selector. Pass an optional namespace to scope the search; omit to search all namespaces.",
    args_schema=LabelSelectorInputSchema
)


class CheckPodExistsInput(BaseModel):
    namespace: str = Field(description="Kubernetes namespace")
    pod_name: str = Field(description="Pod name to check")


@_handle_k8s_exceptions
def check_pod_exists(namespace: str, pod_name: str) -> str:
    """Checks whether a specific pod exists and returns its phase."""
    core_v1 = get_core_v1_api()
    try:
        pod = resolve_pod(core_v1, namespace, pod_name)
        note = (
            f"Exact name '{pod_name}' not found; matched pod '{pod.metadata.name}' by prefix"
            if pod.metadata.name != pod_name
            else None
        )
        result = {"status": "exists", "phase": pod.status.phase, "pod_name": pod.metadata.name}
        if note:
            result["note"] = note
        return json.dumps(result)
    except ApiException as e:
        if e.status == 404:
            return json.dumps({"status": "not_found"})
        raise


check_pod_exists_tool = StructuredTool.from_function(
    func=check_pod_exists,
    name="check_pod_exists",
    description=(
        "Checks whether a named pod exists in a namespace. Accepts either an exact pod name or a "
        "deployment/workload name — if the exact name is not found, automatically searches for pods "
        "whose names start with the given name (handles ReplicaSet hash suffixes). "
        "Returns {status: exists, phase: ..., pod_name: <actual-pod-name>} or {status: not_found}. "
        "Call this first when a pod or workload name is provided before fetching logs or events."
    ),
    args_schema=CheckPodExistsInput,
)


# ===============================================================================
#                           TOOL COLLECTION
# ===============================================================================

# Export all pod tools for easy import
pod_tools = [
    list_all_pods_tool,
    list_pods_in_namespace_tool,
    describe_kubernetes_pod_tool,
    create_pod_tool,
    delete_pod_from_namespaces_tool,
    list_error_pods_tool,
    list_pods_with_two_containers_tool,
    get_pod_events_tool,
    get_pod_logs_tool,
    get_pod_diagnostics_tool,
    check_pods_using_deprecated_apis_tool,
    list_resources_with_label_tool,
    check_pod_exists_tool,
]
