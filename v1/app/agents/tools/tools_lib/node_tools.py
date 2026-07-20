import json
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from kubernetes.client.exceptions import ApiException

from app.agents.tools.tools_lib._base import (
    get_core_v1_api,
    get_custom_objects_api,
    _handle_k8s_exceptions,
    NoArgumentsInputSchema,
)
from app.services import kubernetes_service
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")


def get_metrics_v1beta1_api():
    """Get metrics client for node metrics (alias for get_custom_objects_api)."""
    return get_custom_objects_api()


def _calculate_node_age(creation_timestamp):
    """Calculate node age in days."""
    if not creation_timestamp:
        return "Unknown"
    
    if isinstance(creation_timestamp, str):
        creation_time = datetime.fromisoformat(creation_timestamp.replace('Z', '+00:00'))
    else:
        creation_time = creation_timestamp.replace(tzinfo=None)
    
    age_delta = datetime.utcnow() - creation_time.replace(tzinfo=None)
    return age_delta.days


def _parse_resource_quantity(quantity_str):
    """Parse Kubernetes resource quantities to human readable format."""
    if not quantity_str:
        return quantity_str
    
    # Handle memory units (Ki, Mi, Gi, Ti)
    if quantity_str.endswith('Ki'):
        return f"{int(quantity_str[:-2]) / 1024:.1f}Mi"
    elif quantity_str.endswith('Mi'):
        return quantity_str
    elif quantity_str.endswith('Gi'):
        return quantity_str
    elif quantity_str.endswith('Ti'):
        return quantity_str
    
    # Handle CPU units (m = millicores)
    if quantity_str.endswith('m'):
        return f"{int(quantity_str[:-1])}m"
    
    return quantity_str


# ===============================================================================
#                               INPUT SCHEMAS
# ===============================================================================

class NodeNameInputSchema(BaseModel):
    """Schema for tools that require a node name."""
    node_name: str = Field(description="The name of the Kubernetes node.")


class TimeRangeInputSchema(BaseModel):
    """Schema for tools that use time ranges."""
    last_n_days: int = Field(default=3, description="The number of days to look back. Defaults to 3 days.")


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
#                               NODE TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def list_kubernetes_nodes() -> Dict[str, Any]:
    """Lists all nodes in the Kubernetes cluster with basic information."""
    core_v1 = get_core_v1_api()
    node_list = core_v1.list_node(timeout_seconds=10)
    
    nodes = []
    for node in node_list.items:
        # Get the Ready condition status
        ready_condition = "Unknown"
        for condition in node.status.conditions or []:
            if condition.type == "Ready":
                ready_condition = condition.status
                break
        
        node_info = {
            "name": node.metadata.name,
            "status": ready_condition,
            "roles": [],
            "age_days": _calculate_node_age(node.metadata.creation_timestamp),
            "version": node.status.node_info.kubelet_version if node.status.node_info else "Unknown",
            "internal_ip": None,
            "external_ip": None
        }
        
        # Extract node roles from labels
        if node.metadata.labels:
            for label_key in node.metadata.labels:
                if label_key.startswith("node-role.kubernetes.io/"):
                    role = label_key.split("/")[-1]
                    if role:
                        node_info["roles"].append(role)
        
        # Extract IP addresses
        if node.status.addresses:
            for addr in node.status.addresses:
                if addr.type == "InternalIP":
                    node_info["internal_ip"] = addr.address
                elif addr.type == "ExternalIP":
                    node_info["external_ip"] = addr.address
        
        nodes.append(node_info)

    return {"status": "success", "total_count": len(nodes), "data": nodes}


list_kubernetes_nodes_tool = StructuredTool.from_function(
    func=list_kubernetes_nodes,
    name="list_kubernetes_nodes",
    description="Lists all nodes in the Kubernetes cluster with their status, roles, age, version, and IP addresses.",
    args_schema=NoArgumentsInputSchema
)


@_handle_k8s_exceptions
def get_kubernetes_nodes_info() -> Dict[str, Any]:
    """Fetches detailed information about all nodes in the Kubernetes cluster."""
    core_v1 = get_core_v1_api()
    node_list = core_v1.list_node(timeout_seconds=10)
    
    nodes_info = []
    for node in node_list.items:
        # Process node conditions
        conditions = []
        if node.status.conditions:
            for condition in node.status.conditions:
                conditions.append({
                    "type": condition.type,
                    "status": condition.status,
                    "reason": condition.reason or "N/A",
                    "message": condition.message or "N/A",
                    "last_heartbeat_time": condition.last_heartbeat_time.isoformat() if condition.last_heartbeat_time else None,
                    "last_transition_time": condition.last_transition_time.isoformat() if condition.last_transition_time else None
                })
        
        # Process resource capacity and allocatable
        capacity = {}
        allocatable = {}
        
        if node.status.capacity:
            for resource, quantity in node.status.capacity.items():
                capacity[resource] = _parse_resource_quantity(str(quantity))
        
        if node.status.allocatable:
            for resource, quantity in node.status.allocatable.items():
                allocatable[resource] = _parse_resource_quantity(str(quantity))
        
        # Get node addresses
        addresses = {}
        if node.status.addresses:
            for addr in node.status.addresses:
                addresses[addr.type.lower()] = addr.address
        
        # Get system info
        node_info_obj = node.status.node_info
        system_info = {}
        if node_info_obj:
            system_info = {
                "machine_id": node_info_obj.machine_id,
                "system_uuid": node_info_obj.system_uuid,
                "boot_id": node_info_obj.boot_id,
                "kernel_version": node_info_obj.kernel_version,
                "os_image": node_info_obj.os_image,
                "container_runtime_version": node_info_obj.container_runtime_version,
                "kubelet_version": node_info_obj.kubelet_version,
                "kube_proxy_version": node_info_obj.kube_proxy_version,
                "operating_system": node_info_obj.operating_system,
                "architecture": node_info_obj.architecture
            }
        
        node_info = {
            "name": node.metadata.name,
            "labels": node.metadata.labels or {},
            "annotations": node.metadata.annotations or {},
            "creation_timestamp": node.metadata.creation_timestamp.isoformat() if node.metadata.creation_timestamp else None,
            "age_days": _calculate_node_age(node.metadata.creation_timestamp),
            "capacity": capacity,
            "allocatable": allocatable,
            "addresses": addresses,
            "conditions": conditions,
            "system_info": system_info,
            "taints": [{"key": taint.key, "value": taint.value, "effect": taint.effect} for taint in (node.spec.taints or [])]
        }
        
        nodes_info.append(node_info)

    return {"status": "success", "total_count": len(nodes_info), "data": nodes_info}


get_kubernetes_nodes_info_tool = StructuredTool.from_function(
    func=get_kubernetes_nodes_info,
    name="get_kubernetes_nodes_info",
    description="Fetches comprehensive detailed information about all nodes in a Kubernetes cluster including resources, conditions, system info, and taints.",
    args_schema=NoArgumentsInputSchema
)


@_handle_k8s_exceptions
def describe_node(node_name: str) -> Dict[str, Any]:
    """Gets detailed information about a specific node."""
    core_v1 = get_core_v1_api()
    
    # Get node details
    node = core_v1.read_node(name=node_name)
    
    # Get pods running on this node
    pods_on_node = []
    try:
        pod_list = core_v1.list_pod_for_all_namespaces(
            field_selector=f"spec.nodeName={node_name}",
            timeout_seconds=10
        )
        
        for pod in pod_list.items:
            pod_info = {
                "name": pod.metadata.name,
                "namespace": pod.metadata.namespace,
                "status": pod.status.phase,
                "restart_count": sum(container_status.restart_count for container_status in (pod.status.container_statuses or [])),
                "cpu_requests": 0,
                "memory_requests": 0,
                "cpu_limits": 0,
                "memory_limits": 0
            }
            
            # Calculate resource requests and limits
            if pod.spec.containers:
                for container in pod.spec.containers:
                    if container.resources:
                        if container.resources.requests:
                            cpu_req = container.resources.requests.get('cpu', '0')
                            if cpu_req.endswith('m'):
                                pod_info["cpu_requests"] += int(cpu_req[:-1])
                            else:
                                pod_info["cpu_requests"] += int(float(cpu_req) * 1000)
                            
                            mem_req = container.resources.requests.get('memory', '0')
                            if mem_req.endswith('Mi'):
                                pod_info["memory_requests"] += int(mem_req[:-2])
                            elif mem_req.endswith('Gi'):
                                pod_info["memory_requests"] += int(mem_req[:-2]) * 1024
                        
                        if container.resources.limits:
                            cpu_lim = container.resources.limits.get('cpu', '0')
                            if cpu_lim.endswith('m'):
                                pod_info["cpu_limits"] += int(cpu_lim[:-1])
                            else:
                                pod_info["cpu_limits"] += int(float(cpu_lim) * 1000)
                            
                            mem_lim = container.resources.limits.get('memory', '0')
                            if mem_lim.endswith('Mi'):
                                pod_info["memory_limits"] += int(mem_lim[:-2])
                            elif mem_lim.endswith('Gi'):
                                pod_info["memory_limits"] += int(mem_lim[:-2]) * 1024
            
            pods_on_node.append(pod_info)
    
    except ApiException:
        # If we can't get pods, continue without them
        pass
    
    # Process the node information similar to get_kubernetes_nodes_info but for single node
    conditions = []
    if node.status.conditions:
        for condition in node.status.conditions:
            conditions.append({
                "type": condition.type,
                "status": condition.status,
                "reason": condition.reason or "N/A",
                "message": condition.message or "N/A",
                "last_heartbeat_time": condition.last_heartbeat_time.isoformat() if condition.last_heartbeat_time else None,
                "last_transition_time": condition.last_transition_time.isoformat() if condition.last_transition_time else None
            })
    
    # Calculate resource utilization
    total_cpu_requests = sum(pod["cpu_requests"] for pod in pods_on_node)
    total_memory_requests = sum(pod["memory_requests"] for pod in pods_on_node)
    total_cpu_limits = sum(pod["cpu_limits"] for pod in pods_on_node)
    total_memory_limits = sum(pod["memory_limits"] for pod in pods_on_node)
    
    # Get allocatable resources for percentage calculation
    allocatable_cpu = 0
    allocatable_memory = 0
    
    if node.status.allocatable:
        cpu_alloc = node.status.allocatable.get('cpu', '0')
        if cpu_alloc.endswith('m'):
            allocatable_cpu = int(cpu_alloc[:-1])
        else:
            allocatable_cpu = int(float(cpu_alloc) * 1000)
        
        mem_alloc = node.status.allocatable.get('memory', '0')
        if mem_alloc.endswith('Ki'):
            allocatable_memory = int(mem_alloc[:-2]) / 1024  # Convert to Mi
        elif mem_alloc.endswith('Mi'):
            allocatable_memory = int(mem_alloc[:-2])
        elif mem_alloc.endswith('Gi'):
            allocatable_memory = int(mem_alloc[:-2]) * 1024
    
    node_description = {
        "name": node.metadata.name,
        "labels": node.metadata.labels or {},
        "annotations": node.metadata.annotations or {},
        "creation_timestamp": node.metadata.creation_timestamp.isoformat() if node.metadata.creation_timestamp else None,
        "age_days": _calculate_node_age(node.metadata.creation_timestamp),
        "capacity": {k: _parse_resource_quantity(str(v)) for k, v in (node.status.capacity or {}).items()},
        "allocatable": {k: _parse_resource_quantity(str(v)) for k, v in (node.status.allocatable or {}).items()},
        "conditions": conditions,
        "taints": [{"key": taint.key, "value": taint.value, "effect": taint.effect} for taint in (node.spec.taints or [])],
        "pod_count": len(pods_on_node),
        "pods": pods_on_node,
        "resource_utilization": {
            "cpu_requests_millicores": total_cpu_requests,
            "memory_requests_mi": total_memory_requests,
            "cpu_limits_millicores": total_cpu_limits,
            "memory_limits_mi": total_memory_limits,
            "cpu_requests_percentage": round((total_cpu_requests / allocatable_cpu) * 100, 2) if allocatable_cpu > 0 else 0,
            "memory_requests_percentage": round((total_memory_requests / allocatable_memory) * 100, 2) if allocatable_memory > 0 else 0
        }
    }
    
    return {"status": "success", "data": node_description}


describe_node_tool = StructuredTool.from_function(
    func=describe_node,
    name="describe_kubernetes_node",
    description="Gets detailed information about a specific Kubernetes node including running pods, resource utilization, and system information.",
    args_schema=NodeNameInputSchema
)


@_handle_k8s_exceptions
def get_kubelet_not_ready_nodes(last_n_days: int = 3) -> Dict[str, Any]:
    """Checks which nodes had 'Kubelet not ready' events in the specified time range."""
    core_v1 = get_core_v1_api()
    
    now = datetime.utcnow()
    start_time = now - timedelta(days=last_n_days)
    
    # Fetch events from the Kubernetes API
    events = core_v1.list_event_for_all_namespaces(
        field_selector="reason=KubeletNotReady",
        timeout_seconds=10
    )
    
    affected_nodes = {}
    for event in events.items:
        # Parse the event timestamp
        event_time = event.last_timestamp or event.event_time or event.metadata.creation_timestamp
        
        if event_time:
            if isinstance(event_time, str):
                try:
                    event_time = datetime.fromisoformat(event_time.replace('Z', '+00:00'))
                except ValueError:
                    try:
                        event_time = datetime.strptime(event_time, "%Y-%m-%dT%H:%M:%SZ")
                    except ValueError:
                        continue
            
            event_time = event_time.replace(tzinfo=None)
            
            if event_time >= start_time:
                node_name = event.involved_object.name if event.involved_object else "Unknown"
                
                if node_name not in affected_nodes:
                    affected_nodes[node_name] = {
                        "events": [],
                        "event_count": 0,
                        "first_occurrence": event_time.isoformat(),
                        "last_occurrence": event_time.isoformat()
                    }
                
                affected_nodes[node_name]["events"].append({
                    "message": event.message,
                    "timestamp": event_time.isoformat(),
                    "count": event.count or 1
                })
                
                affected_nodes[node_name]["event_count"] += event.count or 1
                
                # Update first/last occurrence
                if event_time.isoformat() < affected_nodes[node_name]["first_occurrence"]:
                    affected_nodes[node_name]["first_occurrence"] = event_time.isoformat()
                if event_time.isoformat() > affected_nodes[node_name]["last_occurrence"]:
                    affected_nodes[node_name]["last_occurrence"] = event_time.isoformat()
    
    # Sort events by timestamp for each node
    for node_data in affected_nodes.values():
        node_data["events"].sort(key=lambda x: x["timestamp"], reverse=True)
    
    return {"status": "success", "data": affected_nodes, "time_range_days": last_n_days}


get_kubelet_not_ready_nodes_tool = StructuredTool.from_function(
    func=get_kubelet_not_ready_nodes,
    name="get_kubelet_not_ready_nodes",
    description="Checks which Kubernetes nodes had 'Kubelet not ready' events in the specified number of days with detailed event information and statistics.",
    args_schema=TimeRangeInputSchema
)


@_handle_k8s_exceptions
def get_node_events(node_name: str, last_n_days: int = 3) -> Dict[str, Any]:
    """Gets all events related to a specific node."""
    core_v1 = get_core_v1_api()
    
    now = datetime.utcnow()
    start_time = now - timedelta(days=last_n_days)
    
    # Get all events and filter for the specific node
    events = core_v1.list_event_for_all_namespaces(timeout_seconds=10)
    
    node_events = []
    for event in events.items:
        if (event.involved_object and 
            event.involved_object.kind == "Node" and 
            event.involved_object.name == node_name):
            
            event_time = event.last_timestamp or event.event_time or event.metadata.creation_timestamp
            
            if event_time:
                if isinstance(event_time, str):
                    try:
                        event_time = datetime.fromisoformat(event_time.replace('Z', '+00:00'))
                    except ValueError:
                        try:
                            event_time = datetime.strptime(event_time, "%Y-%m-%dT%H:%M:%SZ")
                        except ValueError:
                            continue
                
                event_time = event_time.replace(tzinfo=None)
                
                if event_time >= start_time:
                    node_events.append({
                        "name": event.metadata.name,
                        "reason": event.reason,
                        "message": event.message,
                        "type": event.type,
                        "count": event.count or 1,
                        "first_timestamp": event.first_timestamp.isoformat() if event.first_timestamp else None,
                        "last_timestamp": event.last_timestamp.isoformat() if event.last_timestamp else None,
                        "source": event.source.component if event.source else "Unknown"
                    })
    
    # Sort events by last timestamp (most recent first)
    node_events.sort(key=lambda x: x.get("last_timestamp") or x.get("first_timestamp") or "", reverse=True)
    
    return {
        "status": "success", 
        "data": {
            "node_name": node_name,
            "events": node_events,
            "event_count": len(node_events),
            "time_range_days": last_n_days
        }
    }


class NodeEventsInputSchema(BaseModel):
    """Schema for node events tool."""
    node_name: str = Field(description="The name of the Kubernetes node.")
    last_n_days: int = Field(default=3, description="The number of days to look back for events. Defaults to 3 days.")


get_node_events_tool = StructuredTool.from_function(
    func=get_node_events,
    name="get_node_events",
    description="Gets all events related to a specific Kubernetes node within the specified time range.",
    args_schema=NodeEventsInputSchema
)


@_handle_k8s_exceptions
def get_node_resource_usage() -> Dict[str, Any]:
    """Gets resource usage summary across all nodes."""
    core_v1 = get_core_v1_api()
    
    # Get all nodes
    node_list = core_v1.list_node(timeout_seconds=10)
    
    # Get all pods to calculate resource usage
    pod_list = core_v1.list_pod_for_all_namespaces(timeout_seconds=10)
    
    # Create a mapping of node resource usage
    node_usage = {}
    
    # Initialize node data
    for node in node_list.items:
        node_usage[node.metadata.name] = {
            "allocatable_cpu": 0,
            "allocatable_memory": 0,
            "requested_cpu": 0,
            "requested_memory": 0,
            "limited_cpu": 0,
            "limited_memory": 0,
            "pod_count": 0,
            "pods": []
        }
        
        # Get allocatable resources
        if node.status.allocatable:
            cpu_alloc = node.status.allocatable.get('cpu', '0')
            if cpu_alloc.endswith('m'):
                node_usage[node.metadata.name]["allocatable_cpu"] = int(cpu_alloc[:-1])
            else:
                node_usage[node.metadata.name]["allocatable_cpu"] = int(float(cpu_alloc) * 1000)
            
            mem_alloc = node.status.allocatable.get('memory', '0')
            if mem_alloc.endswith('Ki'):
                node_usage[node.metadata.name]["allocatable_memory"] = int(mem_alloc[:-2]) / 1024
            elif mem_alloc.endswith('Mi'):
                node_usage[node.metadata.name]["allocatable_memory"] = int(mem_alloc[:-2])
            elif mem_alloc.endswith('Gi'):
                node_usage[node.metadata.name]["allocatable_memory"] = int(mem_alloc[:-2]) * 1024
    
    # Calculate resource usage from pods
    for pod in pod_list.items:
        node_name = pod.spec.node_name
        if node_name and node_name in node_usage:
            node_usage[node_name]["pod_count"] += 1
            node_usage[node_name]["pods"].append({
                "name": pod.metadata.name,
                "namespace": pod.metadata.namespace,
                "status": pod.status.phase
            })
            
            # Calculate resource requests and limits
            if pod.spec.containers:
                for container in pod.spec.containers:
                    if container.resources:
                        if container.resources.requests:
                            cpu_req = container.resources.requests.get('cpu', '0')
                            if cpu_req.endswith('m'):
                                node_usage[node_name]["requested_cpu"] += int(cpu_req[:-1])
                            else:
                                node_usage[node_name]["requested_cpu"] += int(float(cpu_req) * 1000)
                            
                            mem_req = container.resources.requests.get('memory', '0')
                            if mem_req.endswith('Mi'):
                                node_usage[node_name]["requested_memory"] += int(mem_req[:-2])
                            elif mem_req.endswith('Gi'):
                                node_usage[node_name]["requested_memory"] += int(mem_req[:-2]) * 1024
                        
                        if container.resources.limits:
                            cpu_lim = container.resources.limits.get('cpu', '0')
                            if cpu_lim.endswith('m'):
                                node_usage[node_name]["limited_cpu"] += int(cpu_lim[:-1])
                            else:
                                node_usage[node_name]["limited_cpu"] += int(float(cpu_lim) * 1000)
                            
                            mem_lim = container.resources.limits.get('memory', '0')
                            if mem_lim.endswith('Mi'):
                                node_usage[node_name]["limited_memory"] += int(mem_lim[:-2])
                            elif mem_lim.endswith('Gi'):
                                node_usage[node_name]["limited_memory"] += int(mem_lim[:-2]) * 1024
    
    # Calculate percentages and create final output
    resource_summary = []
    for node_name, usage in node_usage.items():
        cpu_req_percentage = (usage["requested_cpu"] / usage["allocatable_cpu"]) * 100 if usage["allocatable_cpu"] > 0 else 0
        mem_req_percentage = (usage["requested_memory"] / usage["allocatable_memory"]) * 100 if usage["allocatable_memory"] > 0 else 0
        
        resource_summary.append({
            "node_name": node_name,
            "pod_count": usage["pod_count"],
            "cpu": {
                "allocatable_millicores": usage["allocatable_cpu"],
                "requested_millicores": usage["requested_cpu"],
                "limited_millicores": usage["limited_cpu"],
                "requested_percentage": round(cpu_req_percentage, 2)
            },
            "memory": {
                "allocatable_mi": usage["allocatable_memory"],
                "requested_mi": usage["requested_memory"],
                "limited_mi": usage["limited_memory"],
                "requested_percentage": round(mem_req_percentage, 2)
            }
        })
    
    return {"status": "success", "data": resource_summary}


get_node_resource_usage_tool = StructuredTool.from_function(
    func=get_node_resource_usage,
    name="get_node_resource_usage",
    description="Gets resource usage summary across all Kubernetes nodes showing CPU and memory allocation, requests, limits and utilization percentages.",
    args_schema=NoArgumentsInputSchema
)


# ===============================================================================
#                           CORDON / UNCORDON TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def cordon_node(node_name: str) -> Dict[str, Any]:
    """Mark a node as unschedulable (cordon) so no new pods are scheduled on it."""
    core_v1 = get_core_v1_api()
    patch_body = {"spec": {"unschedulable": True}}
    core_v1.patch_node(name=node_name, body=patch_body)
    return {
        "status": "success",
        "data": {
            "node": node_name,
            "unschedulable": True,
            "message": f"Node '{node_name}' has been cordoned. No new pods will be scheduled on it.",
        }
    }


cordon_node_tool = StructuredTool.from_function(
    func=cordon_node,
    name="cordon_node",
    description="Mark a Kubernetes node as unschedulable (cordon). Existing pods continue running but no new pods will be scheduled on it.",
    args_schema=NodeNameInputSchema,
)


@_handle_k8s_exceptions
def uncordon_node(node_name: str) -> Dict[str, Any]:
    """Mark a node as schedulable again (uncordon) after it was cordoned."""
    core_v1 = get_core_v1_api()
    patch_body = {"spec": {"unschedulable": False}}
    core_v1.patch_node(name=node_name, body=patch_body)
    return {
        "status": "success",
        "data": {
            "node": node_name,
            "unschedulable": False,
            "message": f"Node '{node_name}' has been uncordoned. New pods can now be scheduled on it.",
        }
    }


uncordon_node_tool = StructuredTool.from_function(
    func=uncordon_node,
    name="uncordon_node",
    description="Mark a Kubernetes node as schedulable again (uncordon) after it was previously cordoned.",
    args_schema=NodeNameInputSchema,
)


# ===============================================================================
#                           TOOL COLLECTION
# ===============================================================================

# Export all node tools for easy import
node_tools = [
    connectivity_check_tool,
    list_kubernetes_nodes_tool,
    get_kubernetes_nodes_info_tool,
    describe_node_tool,
    get_kubelet_not_ready_nodes_tool,
    get_node_events_tool,
    get_node_resource_usage_tool,
    cordon_node_tool,
    uncordon_node_tool,
]

