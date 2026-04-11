import json
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime
from typing import Dict, Any, Optional

from langchain_core.tools import StructuredTool
from opentelemetry import trace
from pydantic import BaseModel, Field

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from app.agents.tools.tools_lib._base import (
    get_core_v1_api,
    get_apps_v1_api,
    get_batch_v1_api,
    get_networking_v1_api,
    _handle_k8s_exceptions,
    NoArgumentsInputSchema,
    NamespaceInputSchema,
)
from app.core.config import settings
from app.services import kubernetes_service
from app.utils.logger_config import setup_logging
from app.utils.metrics import tool_calls_total

_tracer = trace.get_tracer("kubeintellect.tools")

logger = setup_logging(app_name="kubeintellect")


# ===============================================================================
#                               INPUT SCHEMAS
# ===============================================================================

class CreateNamespaceInputSchema(BaseModel):
    """Schema for creating Kubernetes namespaces."""
    namespace_name: str = Field(description="The name of the Kubernetes namespace to be created.")


class NamespaceWithQuotaInputSchema(BaseModel):
    """Schema for creating namespaces with resource quotas."""
    namespace: str = Field(description="The name of the Kubernetes namespace to create.")
    cpu_limit: str = Field(description="The CPU limit for the resource quota, specified as a string (e.g., '1').")
    memory_limit: str = Field(description="The memory limit for the resource quota, specified as a string (e.g., '1Gi').")


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
#                            NAMESPACE TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def list_kubernetes_namespaces() -> Dict[str, Any]:
    """Lists all namespaces in the Kubernetes cluster."""
    core_v1 = get_core_v1_api()
    namespace_list = core_v1.list_namespace(timeout_seconds=10)
    
    namespaces = []
    for ns in namespace_list.items:
        # Extract namespace information
        namespace_info = {
            "name": ns.metadata.name,
            "status": ns.status.phase,
            "labels": ns.metadata.labels or {},
            "creation_timestamp": ns.metadata.creation_timestamp.isoformat() if ns.metadata.creation_timestamp else None
        }
        
        # Calculate age
        if ns.metadata.creation_timestamp:
            age_delta = datetime.utcnow() - ns.metadata.creation_timestamp.replace(tzinfo=None)
            namespace_info["age_days"] = age_delta.days
        else:
            namespace_info["age_days"] = "Unknown"
        
        namespaces.append(namespace_info)
    
    return {"status": "success", "total_count": len(namespaces), "data": namespaces}


list_kubernetes_namespaces_tool = StructuredTool.from_function(
    func=list_kubernetes_namespaces,
    name="list_kubernetes_namespaces",
    description="Lists all namespaces in a Kubernetes cluster with detailed information including status, labels, annotations, and age.",
    args_schema=NoArgumentsInputSchema
)


@_handle_k8s_exceptions
def create_kubernetes_namespace(namespace_name: str) -> Dict[str, Any]:
    """Creates a Kubernetes namespace. If the namespace already exists, returns a success message."""
    core_v1 = get_core_v1_api()
    
    # Check if namespace already exists
    try:
        existing = core_v1.read_namespace(name=namespace_name, _request_timeout=settings.K8S_API_TIMEOUT_SECONDS)
        if existing:
            return {"status": "success", "message": f"Namespace '{namespace_name}' already exists."}
    except ApiException as e:
        if e.status != 404:
            # Re-raise if it's not a "not found" error
            raise
    
    # Create the namespace
    namespace = client.V1Namespace(
        metadata=client.V1ObjectMeta(name=namespace_name)
    )
    core_v1.create_namespace(body=namespace)

    message = f"Namespace '{namespace_name}' created successfully."

    # Advisory tip for production-tier namespaces: warn if no ResourceQuota is present.
    PRODUCTION_TIER_NAMES = {"production", "prod", "staging", "live", "release"}
    if namespace_name.lower() in PRODUCTION_TIER_NAMES:
        try:
            quotas = core_v1.list_namespaced_resource_quota(
                namespace=namespace_name,
                _request_timeout=settings.K8S_API_TIMEOUT_SECONDS,
            )
            if not quotas.items:
                message += (
                    f" Tip: namespace '{namespace_name}' was created without a ResourceQuota. "
                    "Consider using create_namespace_with_resource_quota if this will run production workloads."
                )
        except Exception:
            pass  # advisory only — never block the success response

    return {"status": "success", "message": message}


create_kubernetes_namespace_tool = StructuredTool.from_function(
    func=create_kubernetes_namespace,
    name="create_kubernetes_namespace",
    description="Creates a Kubernetes namespace. If the namespace already exists, it returns a success message. Otherwise, it creates the namespace and confirms.",
    args_schema=CreateNamespaceInputSchema
)


@_handle_k8s_exceptions
def describe_namespace(namespace: str) -> Dict[str, Any]:
    """Retrieves detailed information about a specific namespace."""
    core_v1 = get_core_v1_api()
    
    # Get namespace details
    namespace_obj = core_v1.read_namespace(name=namespace, _request_timeout=settings.K8S_API_TIMEOUT_SECONDS)

    # Get resource quotas in the namespace
    resource_quotas = []
    try:
        quotas = core_v1.list_namespaced_resource_quota(namespace=namespace, _request_timeout=settings.K8S_API_TIMEOUT_SECONDS)
        for quota in quotas.items:
            quota_info = {
                "name": quota.metadata.name,
                "hard_limits": quota.spec.hard or {},
                "used": quota.status.used or {}
            }
            resource_quotas.append(quota_info)
    except ApiException:
        # If we can't get quotas, continue without them
        pass

    # Get limit ranges in the namespace
    limit_ranges = []
    try:
        limits = core_v1.list_namespaced_limit_range(namespace=namespace, _request_timeout=settings.K8S_API_TIMEOUT_SECONDS)
        for limit in limits.items:
            limit_info = {
                "name": limit.metadata.name,
                "limits": []
            }
            if limit.spec.limits:
                for item in limit.spec.limits:
                    limit_info["limits"].append({
                        "type": item.type,
                        "default": item.default or {},
                        "default_request": item.default_request or {},
                        "max": item.max or {},
                        "min": item.min or {}
                    })
            limit_ranges.append(limit_info)
    except ApiException:
        # If we can't get limits, continue without them
        pass
    
    namespace_description = {
        "name": namespace_obj.metadata.name,
        "status": namespace_obj.status.phase,
        "labels": namespace_obj.metadata.labels or {},
        "annotations": namespace_obj.metadata.annotations or {},
        "creation_timestamp": namespace_obj.metadata.creation_timestamp.isoformat() if namespace_obj.metadata.creation_timestamp else None,
        "resource_quotas": resource_quotas,
        "limit_ranges": limit_ranges
    }
    
    return {"status": "success", "data": namespace_description}


describe_namespace_tool = StructuredTool.from_function(
    func=describe_namespace,
    name="describe_kubernetes_namespace",
    description="Retrieves detailed information about a specific Kubernetes namespace including resource quotas and limit ranges.",
    args_schema=NamespaceInputSchema
)


@_handle_k8s_exceptions
def create_namespace_with_resource_quota(namespace: str, cpu_limit: str, memory_limit: str) -> Dict[str, Any]:
    """Creates a Kubernetes namespace with resource quotas."""
    core_v1 = get_core_v1_api()
    
    # Create the namespace
    namespace_body = client.V1Namespace(
        metadata=client.V1ObjectMeta(name=namespace)
    )
    
    try:
        core_v1.create_namespace(namespace_body)
    except ApiException as e:
        if e.status == 409:  # Namespace already exists
            pass
        else:
            raise
    
    # Apply the resource quota
    resource_quota_body = client.V1ResourceQuota(
        metadata=client.V1ObjectMeta(name="resource-quota", namespace=namespace),
        spec=client.V1ResourceQuotaSpec(
            hard={
                "limits.cpu": cpu_limit,
                "limits.memory": memory_limit,
                "requests.cpu": cpu_limit,
                "requests.memory": memory_limit
            }
        )
    )
    
    try:
        core_v1.create_namespaced_resource_quota(namespace=namespace, body=resource_quota_body)
        return {"status": "success", "message": f"Namespace '{namespace}' created with resource quota."}
    except ApiException as e:
        if e.status == 409:  # Resource quota already exists
            return {"status": "success", "message": f"Namespace '{namespace}' exists with existing resource quota."}
        else:
            raise


create_namespace_with_resource_quota_tool = StructuredTool.from_function(
    func=create_namespace_with_resource_quota,
    name="create_namespace_with_resource_quota",
    description="Creates a Kubernetes namespace and applies a resource quota to it with CPU and memory limits.",
    args_schema=NamespaceWithQuotaInputSchema
)


@_handle_k8s_exceptions
def delete_namespace(namespace: str) -> Dict[str, Any]:
    """Deletes a Kubernetes namespace."""
    core_v1 = get_core_v1_api()
    
    try:
        # Check if namespace exists first
        core_v1.read_namespace(name=namespace, _request_timeout=settings.K8S_API_TIMEOUT_SECONDS)
        
        # Delete the namespace
        core_v1.delete_namespace(name=namespace)
        
        return {"status": "success", "message": f"Namespace '{namespace}' deletion initiated."}
    except ApiException as e:
        if e.status == 404:
            return {"status": "success", "message": f"Namespace '{namespace}' does not exist."}
        else:
            raise


delete_namespace_tool = StructuredTool.from_function(
    func=delete_namespace,
    name="delete_kubernetes_namespace",
    description="Deletes a Kubernetes namespace. Note that this operation may take time to complete as it deletes all resources within the namespace.",
    args_schema=NamespaceInputSchema
)


@_handle_k8s_exceptions
def list_namespace_events(namespace: str) -> Dict[str, Any]:
    """Lists all events in a specific namespace."""
    core_v1 = get_core_v1_api()
    
    # Validate input
    if not namespace:
        return {"status": "error", "message": "Namespace cannot be empty.", "error_type": "ValueError"}
    
    # Fetch events in the specified namespace
    events = core_v1.list_namespaced_event(namespace=namespace, timeout_seconds=10)
    
    # Process events into a JSON-serializable format
    event_list = []
    for event in events.items:
        event_list.append({
            "name": event.metadata.name,
            "namespace": event.metadata.namespace,
            "reason": event.reason,
            "message": event.message,
            "type": event.type,
            "involved_object": {
                "kind": event.involved_object.kind,
                "name": event.involved_object.name
            } if event.involved_object else {},
            "first_timestamp": event.first_timestamp.isoformat() if event.first_timestamp else None,
            "last_timestamp": event.last_timestamp.isoformat() if event.last_timestamp else None,
            "count": event.count
        })
    
    # Sort events by last timestamp (most recent first)
    event_list.sort(key=lambda x: x.get("last_timestamp") or x.get("first_timestamp") or "", reverse=True)
    
    return {"status": "success", "data": event_list}


list_namespace_events_tool = StructuredTool.from_function(
    func=list_namespace_events,
    name="list_namespace_events",
    description="Lists all Kubernetes events in a specified namespace with details about involved objects and timestamps.",
    args_schema=NamespaceInputSchema
)


@_handle_k8s_exceptions
def get_namespace_warning_events(namespace: str) -> Dict[str, Any]:
    """Lists only Warning-type events in a namespace, grouped and counted by reason.

    Use this to quickly identify what is failing and how many times each failure
    type has occurred — ideal for incident triage and prioritization.
    """
    core_v1 = get_core_v1_api()
    events = core_v1.list_namespaced_event(namespace=namespace, timeout_seconds=10)

    warning_events = []
    counts_by_reason: Dict[str, int] = {}

    for event in events.items:
        if event.type != "Warning":
            continue
        reason = event.reason or "Unknown"
        counts_by_reason[reason] = counts_by_reason.get(reason, 0) + (event.count or 1)
        warning_events.append({
            "reason": reason,
            "message": event.message,
            "involved_object": {
                "kind": event.involved_object.kind if event.involved_object else None,
                "name": event.involved_object.name if event.involved_object else None,
            },
            "count": event.count or 1,
            "last_timestamp": event.last_timestamp.isoformat() if event.last_timestamp else None,
        })

    warning_events.sort(key=lambda x: x.get("last_timestamp") or "", reverse=True)

    return {
        "status": "success",
        "data": {
            "total_warning_events": len(warning_events),
            "counts_by_reason": dict(sorted(counts_by_reason.items(), key=lambda x: x[1], reverse=True)),
            "events": warning_events,
        },
    }


get_namespace_warning_events_tool = StructuredTool.from_function(
    func=get_namespace_warning_events,
    name="get_namespace_warning_events",
    description="Lists only Warning events in a namespace and counts them by reason. Use for incident triage — shows which failure type is most frequent.",
    args_schema=NamespaceInputSchema,
)


@_handle_k8s_exceptions
def get_namespace_resource_usage(namespace: str) -> Dict[str, Any]:
    """Gets resource usage information for a namespace."""
    core_v1 = get_core_v1_api()
    
    # Get all pods in the namespace
    pods = core_v1.list_namespaced_pod(namespace=namespace, _request_timeout=settings.K8S_API_TIMEOUT_SECONDS)

    resource_usage = {
        "namespace": namespace,
        "pod_count": len(pods.items),
        "pods": []
    }
    
    for pod in pods.items:
        pod_info = {
            "name": pod.metadata.name,
            "status": pod.status.phase,
            "containers": []
        }
        
        # Get container resource requests and limits
        for container in pod.spec.containers:
            container_info = {
                "name": container.name,
                "image": container.image,
                "requests": {},
                "limits": {}
            }
            
            if container.resources:
                if container.resources.requests:
                    container_info["requests"] = {
                        k: str(v) for k, v in container.resources.requests.items()
                    }
                if container.resources.limits:
                    container_info["limits"] = {
                        k: str(v) for k, v in container.resources.limits.items()
                    }
            
            pod_info["containers"].append(container_info)
        
        resource_usage["pods"].append(pod_info)
    
    return {"status": "success", "data": resource_usage}


get_namespace_resource_usage_tool = StructuredTool.from_function(
    func=get_namespace_resource_usage,
    name="get_namespace_resource_usage",
    description="Gets resource usage information for a namespace including pod counts and container resource requests/limits.",
    args_schema=NamespaceInputSchema
)


# ===============================================================================
#                            CLUSTER-WIDE POD COUNTS
# ===============================================================================

@_handle_k8s_exceptions
def count_pods_in_namespaces() -> Dict[str, Any]:
    """Counts the number of pods in every namespace across the cluster."""
    core_v1 = get_core_v1_api()
    namespaces = core_v1.list_namespace(timeout_seconds=10)
    counts = {}
    for ns in namespaces.items:
        ns_name = ns.metadata.name
        try:
            pods = core_v1.list_namespaced_pod(namespace=ns_name, timeout_seconds=10)
            counts[ns_name] = len(pods.items)
        except ApiException as e:
            counts[ns_name] = f"Error: {e.reason} (status: {e.status})"
    return {"status": "success", "data": counts}


count_pods_in_namespaces_tool = StructuredTool.from_function(
    func=count_pods_in_namespaces,
    name="count_pods_in_namespaces",
    description="Counts the number of pods in every namespace across the cluster. Useful for a quick capacity and workload distribution overview.",
    args_schema=NoArgumentsInputSchema,
)


# ===============================================================================
#                         CROSS-RESOURCE QUERY TOOLS
# ===============================================================================

class LabelSelectorInputSchema(BaseModel):
    """Schema for tools that use label selectors."""
    label_selector: str = Field(description="The label selector to filter Kubernetes resources (e.g., 'app=nginx').")
    namespace: Optional[str] = Field(default=None, description="Optional namespace to scope the search. If omitted, searches all namespaces.")


@_handle_k8s_exceptions
def list_resources_with_label(label_selector: str, namespace: Optional[str] = None) -> Dict[str, Any]:
    """Lists Pods, Services, and ConfigMaps that match a label selector, optionally scoped to a namespace."""
    core_v1 = get_core_v1_api()
    resources = []

    if namespace:
        pods = core_v1.list_namespaced_pod(namespace=namespace, label_selector=label_selector, timeout_seconds=10)
        services = core_v1.list_namespaced_service(namespace=namespace, label_selector=label_selector, timeout_seconds=10)
        config_maps = core_v1.list_namespaced_config_map(namespace=namespace, label_selector=label_selector, timeout_seconds=10)
    else:
        pods = core_v1.list_pod_for_all_namespaces(label_selector=label_selector, timeout_seconds=10)
        services = core_v1.list_service_for_all_namespaces(label_selector=label_selector, timeout_seconds=10)
        config_maps = core_v1.list_config_map_for_all_namespaces(label_selector=label_selector, timeout_seconds=10)

    resources.extend([
        {"type": "Pod", "namespace": pod.metadata.namespace, "name": pod.metadata.name, "labels": pod.metadata.labels}
        for pod in pods.items
    ])
    resources.extend([
        {"type": "Service", "namespace": svc.metadata.namespace, "name": svc.metadata.name, "labels": svc.metadata.labels}
        for svc in services.items
    ])
    resources.extend([
        {"type": "ConfigMap", "namespace": cm.metadata.namespace, "name": cm.metadata.name, "labels": cm.metadata.labels}
        for cm in config_maps.items
    ])

    return {"status": "success", "data": resources}


list_resources_with_label_tool = StructuredTool.from_function(
    func=list_resources_with_label,
    name="list_kubernetes_resources_with_label",
    description="Lists Kubernetes resources (Pods, Services, ConfigMaps) that match a label selector. Pass an optional namespace to scope the search; omit to search all namespaces.",
    args_schema=LabelSelectorInputSchema
)


class PatchNamespaceLabelsInputSchema(BaseModel):
    """Schema for adding/updating labels on a Kubernetes namespace."""
    namespace: str = Field(description="The name of the namespace to label.")
    labels: Dict[str, str] = Field(
        description="Labels to add or update on the namespace as key-value pairs, e.g. environment=production, team=backend. Existing labels not in this dict are preserved."
    )


@_handle_k8s_exceptions
def patch_namespace_labels(namespace: str, labels: Dict[str, str]) -> Dict[str, Any]:
    """Adds or updates labels on an existing Kubernetes namespace.
    Existing labels not included in the request are preserved (merge-patch semantics).
    Use this to apply governance labels, environment tags, or team ownership labels.
    """
    core_v1 = get_core_v1_api()
    try:
        response = core_v1.patch_namespace(
            name=namespace,
            body={"metadata": {"labels": labels}},
        )
        return {
            "status": "success",
            "data": {
                "namespace": response.metadata.name,
                "labels": response.metadata.labels,
                "message": f"Labels applied to namespace '{namespace}': {labels}",
            },
        }
    except ApiException as e:
        if e.status == 404:
            return {
                "status": "error",
                "message": f"Namespace '{namespace}' not found",
                "error_type": "NotFound",
            }
        raise


patch_namespace_labels_tool = StructuredTool.from_function(
    func=patch_namespace_labels,
    name="patch_namespace_labels",
    description=(
        "Adds or updates labels on an existing Kubernetes namespace. Existing labels are preserved "
        "(merge semantics). Use this to apply governance labels (environment, team, managed-by), "
        "or any other metadata labels to a namespace. Accepts namespace name and a labels dict."
    ),
    args_schema=PatchNamespaceLabelsInputSchema,
)


# ===============================================================================
#                          GAP 2 — DEPLOYMENT EVENT TOOL
# ===============================================================================

class GetDeploymentEventsInput(BaseModel):
    namespace: str = Field(description="Kubernetes namespace where the deployment lives.")
    deployment_name: str = Field(description="Name of the deployment to fetch events for.")


@_handle_k8s_exceptions
def get_deployment_events(namespace: str, deployment_name: str) -> Dict[str, Any]:
    """Fetches Kubernetes events for a specific Deployment.

    Uses involvedObject.kind=Deployment field selector so only events directly
    attached to the named Deployment object are returned.  ReplicaSet-level
    scaling events (involvedObject.kind=ReplicaSet) require a separate query.
    """
    with _tracer.start_as_current_span("get_deployment_events") as span:
        span.set_attribute("k8s.namespace", namespace)
        span.set_attribute("k8s.deployment", deployment_name)
        core_v1 = get_core_v1_api()
        field_selector = (
            f"involvedObject.name={deployment_name},"
            f"involvedObject.kind=Deployment"
        )
        events = core_v1.list_namespaced_event(
            namespace=namespace,
            field_selector=field_selector,
            timeout_seconds=10,
        )

        event_list = []
        for event in events.items:
            event_list.append({
                "reason": event.reason,
                "message": event.message,
                "type": event.type,
                "count": event.count or 1,
                "first_timestamp": event.first_timestamp.isoformat() if event.first_timestamp else None,
                "last_timestamp": event.last_timestamp.isoformat() if event.last_timestamp else None,
            })

        event_list.sort(
            key=lambda x: x.get("last_timestamp") or x.get("first_timestamp") or "",
            reverse=True,
        )

        status = "success"
        tool_calls_total.labels(tool="get_deployment_events", status=status).inc()
        return {
            "status": status,
            "data": {
                "deployment": deployment_name,
                "namespace": namespace,
                "event_count": len(event_list),
                "events": event_list,
            },
        }


get_deployment_events_tool = StructuredTool.from_function(
    func=get_deployment_events,
    name="get_deployment_events",
    description=(
        "Fetches events for a specific Deployment (involvedObject.kind=Deployment). "
        "Use this to see ScalingReplicaSet, FailedCreate, SuccessfulCreate, and other "
        "deployment-level events. Provide namespace and deployment_name. "
        "Note: ReplicaSet-level scaling events require a separate query."
    ),
    args_schema=GetDeploymentEventsInput,
)


# ===============================================================================
#                       GAP 3 — NAMESPACE RESOURCE AGGREGATION
# ===============================================================================

class ListAllNamespaceResourcesInput(BaseModel):
    namespace: str = Field(description="The Kubernetes namespace to summarise.")


@_handle_k8s_exceptions
def list_all_namespace_resources(namespace: str) -> Dict[str, Any]:
    """Returns a compact summary of all major resource types in a namespace.

    Fans out to 8 resource-type APIs concurrently.  Each resource type is
    fetched independently; if one call fails (e.g. 403 for Secrets) the others
    still return.  Secrets: names + count only, never values.
    Token budget target: < 500 tokens for a typical namespace.
    """
    with _tracer.start_as_current_span("list_all_namespace_resources") as span:
        span.set_attribute("k8s.namespace", namespace)
        core_v1 = get_core_v1_api()
        apps_v1 = get_apps_v1_api()
        batch_v1 = get_batch_v1_api()
        networking_v1 = get_networking_v1_api()

        def _fetch_pods():
            items = core_v1.list_namespaced_pod(namespace=namespace, timeout_seconds=10).items
            return [{"name": p.metadata.name, "phase": p.status.phase} for p in items]

        def _fetch_deployments():
            items = apps_v1.list_namespaced_deployment(namespace=namespace, timeout_seconds=10).items
            return [
                {
                    "name": d.metadata.name,
                    "ready": f"{d.status.ready_replicas or 0}/{d.spec.replicas or 0}",
                }
                for d in items
            ]

        def _fetch_statefulsets():
            items = apps_v1.list_namespaced_stateful_set(namespace=namespace, timeout_seconds=10).items
            return [
                {
                    "name": s.metadata.name,
                    "ready": f"{s.status.ready_replicas or 0}/{s.spec.replicas or 0}",
                }
                for s in items
            ]

        def _fetch_services():
            items = core_v1.list_namespaced_service(namespace=namespace, timeout_seconds=10).items
            return [{"name": s.metadata.name, "type": s.spec.type} for s in items]

        def _fetch_configmaps():
            items = core_v1.list_namespaced_config_map(namespace=namespace, timeout_seconds=10).items
            return len(items)  # count only

        def _fetch_secrets():
            items = core_v1.list_namespaced_secret(namespace=namespace, timeout_seconds=10).items
            return {"count": len(items), "names": [s.metadata.name for s in items]}

        def _fetch_ingresses():
            items = networking_v1.list_namespaced_ingress(namespace=namespace, timeout_seconds=10).items
            return [{"name": i.metadata.name} for i in items]

        def _fetch_jobs():
            items = batch_v1.list_namespaced_job(namespace=namespace, timeout_seconds=10).items
            return [
                {
                    "name": j.metadata.name,
                    "succeeded": j.status.succeeded or 0,
                    "failed": j.status.failed or 0,
                }
                for j in items
            ]

        fetchers = {
            "pods": _fetch_pods,
            "deployments": _fetch_deployments,
            "statefulsets": _fetch_statefulsets,
            "services": _fetch_services,
            "configmaps": _fetch_configmaps,
            "secrets": _fetch_secrets,
            "ingresses": _fetch_ingresses,
            "jobs": _fetch_jobs,
        }

        results: Dict[str, Any] = {}
        errors: Dict[str, str] = {}

        with ThreadPoolExecutor(max_workers=8) as executor:
            future_to_key: Dict[Future, str] = {
                executor.submit(fn): key for key, fn in fetchers.items()
            }
            for future, key in future_to_key.items():
                exc = future.exception()
                if exc is not None:
                    errors[key] = str(exc)
                else:
                    results[key] = future.result()

        status = "success"
        tool_calls_total.labels(tool="list_all_namespace_resources", status=status).inc()
        return {
            "status": status,
            "data": {
                "namespace": namespace,
                "resources": results,
                **({"errors": errors} if errors else {}),
            },
        }


list_all_namespace_resources_tool = StructuredTool.from_function(
    func=list_all_namespace_resources,
    name="list_all_namespace_resources",
    description=(
        "Returns a compact summary of all major resource types in a namespace: "
        "Pods, Deployments, StatefulSets, Services, ConfigMaps (count), Secrets (names+count), "
        "Ingresses, and Jobs. Single tool call replaces 5–8 individual list calls. "
        "Partial failures are isolated per resource type — other types still return on error. "
        "Use for first-pass namespace triage."
    ),
    args_schema=ListAllNamespaceResourcesInput,
)


# ===============================================================================
#                           TOOL COLLECTION
# ===============================================================================

# Export all namespace tools for easy import
namespace_tools = [
    connectivity_check_tool,
    list_kubernetes_namespaces_tool,
    create_kubernetes_namespace_tool,
    describe_namespace_tool,
    create_namespace_with_resource_quota_tool,
    delete_namespace_tool,
    list_namespace_events_tool,
    get_namespace_warning_events_tool,
    get_namespace_resource_usage_tool,
    count_pods_in_namespaces_tool,
    list_resources_with_label_tool,
    patch_namespace_labels_tool,
    get_deployment_events_tool,
    list_all_namespace_resources_tool,
]


