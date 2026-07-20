import json
from typing import Dict, Any, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.agents.tools.tools_lib._base import (
    get_core_v1_api,
    get_apps_v1_api,
    _handle_k8s_exceptions,
    NamespaceOptionalInputSchema,
    calculate_age as _calculate_age,
)
from app.services import kubernetes_service
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")


def _analyze_daemonset_status(daemonset) -> Dict[str, Any]:
    """Analyze DaemonSet status and provide insights."""
    status = daemonset.status
    
    analysis = {
        "desired_number_scheduled": status.desired_number_scheduled or 0,
        "current_number_scheduled": status.current_number_scheduled or 0,
        "number_ready": status.number_ready or 0,
        "number_available": status.number_available or 0,
        "number_unavailable": status.number_unavailable or 0,
        "updated_number_scheduled": status.updated_number_scheduled or 0,
        "number_misscheduled": status.number_misscheduled or 0,
        "observed_generation": status.observed_generation,
        "health_status": "Unknown",
        "rollout_status": "Unknown",
        "coverage_percentage": 0
    }
    
    # Determine health status
    desired = analysis["desired_number_scheduled"]
    ready = analysis["number_ready"]
    available = analysis["number_available"]
    
    if desired == 0:
        analysis["health_status"] = "No nodes selected"
    elif ready == desired and available == desired:
        analysis["health_status"] = "Healthy"
    elif ready == 0:
        analysis["health_status"] = "Critical - No pods ready"
    elif ready < desired:
        analysis["health_status"] = "Degraded - Some pods not ready"
    else:
        analysis["health_status"] = "Healthy"
    
    # Determine rollout status
    updated = analysis["updated_number_scheduled"]

    if desired == 0:
        analysis["rollout_status"] = "No rollout needed"
    elif updated == desired:
        analysis["rollout_status"] = "Rollout complete"
    elif updated < desired:
        analysis["rollout_status"] = "Rolling out"
    else:
        analysis["rollout_status"] = "Unknown"
    
    # Calculate coverage percentage
    if desired > 0:
        analysis["coverage_percentage"] = (ready / desired) * 100
    
    return analysis


def _get_node_distribution(daemonset, pods) -> Dict[str, Any]:
    """Analyze pod distribution across nodes for a DaemonSet."""
    node_distribution = {}
    total_nodes_with_pods = 0
    
    for pod in pods:
        if pod.spec.node_name:
            node_name = pod.spec.node_name
            if node_name not in node_distribution:
                node_distribution[node_name] = {
                    "pod_name": pod.metadata.name,
                    "pod_phase": pod.status.phase,
                    "pod_ready": False,
                    "container_statuses": [],
                    "conditions": []
                }
                total_nodes_with_pods += 1
            
            # Check if pod is ready
            if pod.status.conditions:
                for condition in pod.status.conditions:
                    if condition.type == "Ready":
                        node_distribution[node_name]["pod_ready"] = condition.status == "True"
                        break
            
            # Add container statuses
            if pod.status.container_statuses:
                for container_status in pod.status.container_statuses:
                    node_distribution[node_name]["container_statuses"].append({
                        "name": container_status.name,
                        "ready": container_status.ready,
                        "restart_count": container_status.restart_count,
                        "image": container_status.image,
                        "state": str(container_status.state)
                    })
            
            # Add pod conditions
            if pod.status.conditions:
                for condition in pod.status.conditions:
                    node_distribution[node_name]["conditions"].append({
                        "type": condition.type,
                        "status": condition.status,
                        "reason": getattr(condition, 'reason', None),
                        "message": getattr(condition, 'message', None)
                    })
    
    return {
        "nodes": node_distribution,
        "total_nodes_with_pods": total_nodes_with_pods,
        "nodes_with_ready_pods": sum(1 for node_info in node_distribution.values() if node_info["pod_ready"]),
        "nodes_with_issues": sum(1 for node_info in node_distribution.values() if not node_info["pod_ready"])
    }


# ===============================================================================
#                               INPUT SCHEMAS
# ===============================================================================

class DaemonSetInputSchema(BaseModel):
    """Schema for DaemonSet-specific operations."""
    namespace: str = Field(description="The Kubernetes namespace where the DaemonSet is located.")
    daemonset_name: str = Field(description="The name of the DaemonSet.")


class DaemonSetAnalysisInputSchema(BaseModel):
    """Schema for DaemonSet analysis tools."""
    namespace: Optional[str] = Field(default=None, description="The Kubernetes namespace to analyze. If not provided, analyzes all namespaces.")
    include_pod_details: Optional[bool] = Field(default=True, description="Include detailed pod information in the analysis.")


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
#                              DAEMONSET TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def list_daemonsets(namespace: Optional[str] = None) -> Dict[str, Any]:
    """Lists all DaemonSets in a namespace or across all namespaces."""
    apps_v1 = get_apps_v1_api()
    
    if namespace:
        daemonset_list = apps_v1.list_namespaced_daemon_set(namespace=namespace, timeout_seconds=10)
    else:
        daemonset_list = apps_v1.list_daemon_set_for_all_namespaces(timeout_seconds=10)
    
    daemonsets = []
    for ds in daemonset_list.items:
        # Analyze DaemonSet status
        status_analysis = _analyze_daemonset_status(ds)
        
        daemonset_info = {
            "name": ds.metadata.name,
            "namespace": ds.metadata.namespace,
            "creation_timestamp": ds.metadata.creation_timestamp.isoformat() if ds.metadata.creation_timestamp else None,
            "age": _calculate_age(ds.metadata.creation_timestamp),
            "labels": ds.metadata.labels or {},
            "annotations": ds.metadata.annotations or {},
            "selector": ds.spec.selector.match_labels if ds.spec.selector and ds.spec.selector.match_labels else {},
            "node_selector": ds.spec.template.spec.node_selector or {},
            "update_strategy": {
                "type": ds.spec.update_strategy.type if ds.spec.update_strategy else "RollingUpdate",
                "rolling_update": {
                    "max_unavailable": ds.spec.update_strategy.rolling_update.max_unavailable 
                    if ds.spec.update_strategy and ds.spec.update_strategy.rolling_update 
                    else None
                } if ds.spec.update_strategy and ds.spec.update_strategy.rolling_update else None
            },
            "status_analysis": status_analysis,
            "containers": []
        }
        
        # Add container information
        if ds.spec.template.spec.containers:
            for container in ds.spec.template.spec.containers:
                container_info = {
                    "name": container.name,
                    "image": container.image,
                    "ports": [{"container_port": port.container_port, "name": getattr(port, 'name', None)} 
                             for port in (container.ports or [])],
                    "resources": {
                        "requests": container.resources.requests if container.resources and container.resources.requests else {},
                        "limits": container.resources.limits if container.resources and container.resources.limits else {}
                    } if container.resources else {"requests": {}, "limits": {}},
                    "volume_mounts": len(container.volume_mounts) if container.volume_mounts else 0
                }
                daemonset_info["containers"].append(container_info)
        
        daemonsets.append(daemonset_info)
    
    # Sort by creation time (newest first)
    daemonsets.sort(key=lambda x: x["creation_timestamp"] or "", reverse=True)
    
    # Prepare result data
    if namespace:
        result_data = {
            "namespace": namespace,
            "daemonsets": daemonsets,
            "daemonset_count": len(daemonsets)
        }
    else:
        # Group by namespace
        daemonsets_by_namespace = {}
        for ds in daemonsets:
            ns = ds["namespace"]
            if ns not in daemonsets_by_namespace:
                daemonsets_by_namespace[ns] = []
            daemonsets_by_namespace[ns].append(ds)
        
        result_data = {
            "daemonsets_by_namespace": daemonsets_by_namespace,
            "total_daemonset_count": len(daemonsets),
            "namespace_count": len(daemonsets_by_namespace)
        }
    
    # Add health summary
    health_summary = {
        "healthy": len([ds for ds in daemonsets if ds["status_analysis"]["health_status"] == "Healthy"]),
        "degraded": len([ds for ds in daemonsets if ds["status_analysis"]["health_status"].startswith("Degraded")]),
        "critical": len([ds for ds in daemonsets if ds["status_analysis"]["health_status"].startswith("Critical")]),
        "no_nodes": len([ds for ds in daemonsets if ds["status_analysis"]["health_status"] == "No nodes selected"])
    }
    result_data["health_summary"] = health_summary
    
    return {"status": "success", "data": result_data}


list_daemonsets_tool = StructuredTool.from_function(
    func=list_daemonsets,
    name="list_daemonsets",
    description="Lists all Kubernetes DaemonSets in a namespace or across all namespaces with comprehensive status analysis, container information, and health summaries.",
    args_schema=NamespaceOptionalInputSchema
)


@_handle_k8s_exceptions
def describe_daemonset(namespace: str, daemonset_name: str) -> Dict[str, Any]:
    """Gets detailed information about a specific DaemonSet."""
    apps_v1 = get_apps_v1_api()
    core_v1 = get_core_v1_api()
    
    # Get DaemonSet details
    daemonset = apps_v1.read_namespaced_daemon_set(name=daemonset_name, namespace=namespace)
    
    # Analyze DaemonSet status
    status_analysis = _analyze_daemonset_status(daemonset)
    
    daemonset_details = {
        "name": daemonset.metadata.name,
        "namespace": daemonset.metadata.namespace,
        "uid": daemonset.metadata.uid,
        "resource_version": daemonset.metadata.resource_version,
        "generation": daemonset.metadata.generation,
        "creation_timestamp": daemonset.metadata.creation_timestamp.isoformat() if daemonset.metadata.creation_timestamp else None,
        "age": _calculate_age(daemonset.metadata.creation_timestamp),
        "labels": daemonset.metadata.labels or {},
        "annotations": daemonset.metadata.annotations or {},
        "owner_references": [
            {
                "api_version": ref.api_version,
                "kind": ref.kind,
                "name": ref.name,
                "uid": ref.uid
            } for ref in (daemonset.metadata.owner_references or [])
        ],
        "selector": daemonset.spec.selector.match_labels if daemonset.spec.selector and daemonset.spec.selector.match_labels else {},
        "node_selector": daemonset.spec.template.spec.node_selector or {},
        "tolerations": [
            {
                "key": toleration.key,
                "operator": toleration.operator,
                "value": toleration.value,
                "effect": toleration.effect,
                "toleration_seconds": toleration.toleration_seconds
            } for toleration in (daemonset.spec.template.spec.tolerations or [])
        ],
        "update_strategy": {
            "type": daemonset.spec.update_strategy.type if daemonset.spec.update_strategy else "RollingUpdate",
            "rolling_update": {
                "max_unavailable": daemonset.spec.update_strategy.rolling_update.max_unavailable
            } if daemonset.spec.update_strategy and daemonset.spec.update_strategy.rolling_update else None
        },
        "min_ready_seconds": getattr(daemonset.spec, 'min_ready_seconds', 0),
        "revision_history_limit": getattr(daemonset.spec, 'revision_history_limit', 10),
        "status_analysis": status_analysis,
        "containers": [],
        "volumes": [],
        "conditions": []
    }
    
    # Add container information
    if daemonset.spec.template.spec.containers:
        for container in daemonset.spec.template.spec.containers:
            container_info = {
                "name": container.name,
                "image": container.image,
                "image_pull_policy": getattr(container, 'image_pull_policy', 'Always'),
                "command": container.command or [],
                "args": container.args or [],
                "working_dir": getattr(container, 'working_dir', None),
                "ports": [
                    {
                        "name": getattr(port, 'name', None),
                        "container_port": port.container_port,
                        "protocol": getattr(port, 'protocol', 'TCP'),
                        "host_port": getattr(port, 'host_port', None)
                    } for port in (container.ports or [])
                ],
                "env": [
                    {
                        "name": env.name,
                        "value": env.value,
                        "value_from": str(env.value_from) if env.value_from else None
                    } for env in (container.env or [])
                ],
                "resources": {
                    "requests": container.resources.requests if container.resources and container.resources.requests else {},
                    "limits": container.resources.limits if container.resources and container.resources.limits else {}
                } if container.resources else {"requests": {}, "limits": {}},
                "volume_mounts": [
                    {
                        "name": mount.name,
                        "mount_path": mount.mount_path,
                        "sub_path": getattr(mount, 'sub_path', None),
                        "read_only": getattr(mount, 'read_only', False)
                    } for mount in (container.volume_mounts or [])
                ],
                "liveness_probe": str(container.liveness_probe) if container.liveness_probe else None,
                "readiness_probe": str(container.readiness_probe) if container.readiness_probe else None,
                "security_context": str(container.security_context) if container.security_context else None
            }
            daemonset_details["containers"].append(container_info)
    
    # Add volume information
    if daemonset.spec.template.spec.volumes:
        for volume in daemonset.spec.template.spec.volumes:
            volume_info = {
                "name": volume.name,
                "type": "Unknown"
            }
            
            # Determine volume type
            if volume.host_path:
                volume_info.update({
                    "type": "HostPath",
                    "path": volume.host_path.path,
                    "host_path_type": getattr(volume.host_path, 'type', None)
                })
            elif volume.empty_dir:
                volume_info.update({
                    "type": "EmptyDir",
                    "size_limit": getattr(volume.empty_dir, 'size_limit', None)
                })
            elif volume.config_map:
                volume_info.update({
                    "type": "ConfigMap",
                    "config_map_name": volume.config_map.name
                })
            elif volume.secret:
                volume_info.update({
                    "type": "Secret",
                    "secret_name": volume.secret.secret_name
                })
            elif volume.persistent_volume_claim:
                volume_info.update({
                    "type": "PersistentVolumeClaim",
                    "claim_name": volume.persistent_volume_claim.claim_name
                })
            
            daemonset_details["volumes"].append(volume_info)
    
    # Add conditions if available
    if hasattr(daemonset.status, 'conditions') and daemonset.status.conditions:
        for condition in daemonset.status.conditions:
            daemonset_details["conditions"].append({
                "type": condition.type,
                "status": condition.status,
                "reason": getattr(condition, 'reason', None),
                "message": getattr(condition, 'message', None),
                "last_transition_time": condition.last_transition_time.isoformat() if getattr(condition, 'last_transition_time', None) else None
            })
    
    # Get pods managed by this DaemonSet
    try:
        selector_labels = daemonset.spec.selector.match_labels if daemonset.spec.selector and daemonset.spec.selector.match_labels else {}
        label_selector = ','.join([f"{k}={v}" for k, v in selector_labels.items()])
        
        if label_selector:
            pods = core_v1.list_namespaced_pod(namespace=namespace, label_selector=label_selector, timeout_seconds=10)
            
            # Analyze node distribution
            node_distribution = _get_node_distribution(daemonset, pods.items)
            daemonset_details["node_distribution"] = node_distribution
            
            # Add pod summary
            pod_summary = {
                "total_pods": len(pods.items),
                "running_pods": len([pod for pod in pods.items if pod.status.phase == "Running"]),
                "pending_pods": len([pod for pod in pods.items if pod.status.phase == "Pending"]),
                "failed_pods": len([pod for pod in pods.items if pod.status.phase == "Failed"]),
                "succeeded_pods": len([pod for pod in pods.items if pod.status.phase == "Succeeded"])
            }
            daemonset_details["pod_summary"] = pod_summary
        else:
            daemonset_details["node_distribution"] = {"error": "No selector labels found"}
            daemonset_details["pod_summary"] = {"error": "No selector labels found"}
    
    except Exception as e:
        daemonset_details["node_distribution"] = {"error": str(e)}
        daemonset_details["pod_summary"] = {"error": str(e)}
    
    return {"status": "success", "data": daemonset_details}


describe_daemonset_tool = StructuredTool.from_function(
    func=describe_daemonset,
    name="describe_daemonset",
    description="Gets comprehensive detailed information about a specific Kubernetes DaemonSet including containers, volumes, node distribution, and pod status analysis.",
    args_schema=DaemonSetInputSchema
)


@_handle_k8s_exceptions
def analyze_daemonset_health(namespace: Optional[str] = None, include_pod_details: bool = True) -> Dict[str, Any]:
    """Analyzes the health and performance of DaemonSets across the cluster or namespace."""
    apps_v1 = get_apps_v1_api()
    core_v1 = get_core_v1_api()
    
    # Get all DaemonSets
    if namespace:
        daemonset_list = apps_v1.list_namespaced_daemon_set(namespace=namespace, timeout_seconds=10)
    else:
        daemonset_list = apps_v1.list_daemon_set_for_all_namespaces(timeout_seconds=10)
    
    analysis = {
        "total_daemonsets": len(daemonset_list.items),
        "health_breakdown": {
            "healthy": 0,
            "degraded": 0,
            "critical": 0,
            "no_nodes": 0
        },
        "coverage_analysis": {
            "total_desired_pods": 0,
            "total_ready_pods": 0,
            "total_available_pods": 0,
            "total_unavailable_pods": 0,
            "overall_coverage_percentage": 0
        },
        "update_analysis": {
            "rolling_updates_in_progress": 0,
            "updates_completed": 0,
            "updates_failed": 0
        },
        "node_analysis": {
            "total_nodes_targeted": 0,
            "nodes_with_ready_pods": 0,
            "nodes_with_issues": 0
        },
        "detailed_daemonsets": [],
        "problematic_daemonsets": []
    }
    
    if not namespace:
        analysis["namespace_breakdown"] = {}
    
    for ds in daemonset_list.items:
        status_analysis = _analyze_daemonset_status(ds)
        
        # Update health breakdown
        health_status = status_analysis["health_status"]
        if health_status == "Healthy":
            analysis["health_breakdown"]["healthy"] += 1
        elif "Degraded" in health_status:
            analysis["health_breakdown"]["degraded"] += 1
        elif "Critical" in health_status:
            analysis["health_breakdown"]["critical"] += 1
        elif "No nodes" in health_status:
            analysis["health_breakdown"]["no_nodes"] += 1
        
        # Update coverage analysis
        analysis["coverage_analysis"]["total_desired_pods"] += status_analysis["desired_number_scheduled"]
        analysis["coverage_analysis"]["total_ready_pods"] += status_analysis["number_ready"]
        analysis["coverage_analysis"]["total_available_pods"] += status_analysis["number_available"]
        analysis["coverage_analysis"]["total_unavailable_pods"] += status_analysis["number_unavailable"]
        
        # Update update analysis
        rollout_status = status_analysis["rollout_status"]
        if rollout_status == "Rolling out":
            analysis["update_analysis"]["rolling_updates_in_progress"] += 1
        elif rollout_status == "Rollout complete":
            analysis["update_analysis"]["updates_completed"] += 1
        
        # Namespace breakdown
        if not namespace:
            ns = ds.metadata.namespace
            if ns not in analysis["namespace_breakdown"]:
                analysis["namespace_breakdown"][ns] = {
                    "daemonsets": 0,
                    "healthy": 0,
                    "degraded": 0,
                    "critical": 0,
                    "desired_pods": 0,
                    "ready_pods": 0
                }
            
            analysis["namespace_breakdown"][ns]["daemonsets"] += 1
            analysis["namespace_breakdown"][ns]["desired_pods"] += status_analysis["desired_number_scheduled"]
            analysis["namespace_breakdown"][ns]["ready_pods"] += status_analysis["number_ready"]
            
            if health_status == "Healthy":
                analysis["namespace_breakdown"][ns]["healthy"] += 1
            elif "Degraded" in health_status:
                analysis["namespace_breakdown"][ns]["degraded"] += 1
            elif "Critical" in health_status:
                analysis["namespace_breakdown"][ns]["critical"] += 1
        
        # Detailed DaemonSet info
        ds_detail = {
            "name": ds.metadata.name,
            "namespace": ds.metadata.namespace,
            "age": _calculate_age(ds.metadata.creation_timestamp),
            "status_analysis": status_analysis,
            "containers": len(ds.spec.template.spec.containers) if ds.spec.template.spec.containers else 0,
            "node_selector": ds.spec.template.spec.node_selector or {},
            "tolerations_count": len(ds.spec.template.spec.tolerations) if ds.spec.template.spec.tolerations else 0
        }
        
        analysis["detailed_daemonsets"].append(ds_detail)
        
        # Track problematic DaemonSets
        if health_status != "Healthy" and "No nodes" not in health_status:
            problem_detail = ds_detail.copy()
            problem_detail["issue_type"] = health_status
            
            # Get pod details if requested and there are issues
            if include_pod_details:
                try:
                    selector_labels = ds.spec.selector.match_labels if ds.spec.selector and ds.spec.selector.match_labels else {}
                    label_selector = ','.join([f"{k}={v}" for k, v in selector_labels.items()])
                    
                    if label_selector:
                        pods = core_v1.list_namespaced_pod(namespace=ds.metadata.namespace, label_selector=label_selector, timeout_seconds=10)
                        
                        problem_pods = []
                        for pod in pods.items:
                            if pod.status.phase != "Running":
                                pod_issue = {
                                    "name": pod.metadata.name,
                                    "node": pod.spec.node_name,
                                    "phase": pod.status.phase,
                                    "conditions": []
                                }
                                
                                if pod.status.conditions:
                                    for condition in pod.status.conditions:
                                        if condition.status == "False":
                                            pod_issue["conditions"].append({
                                                "type": condition.type,
                                                "reason": getattr(condition, 'reason', None),
                                                "message": getattr(condition, 'message', None)
                                            })
                                
                                problem_pods.append(pod_issue)
                        
                        problem_detail["problematic_pods"] = problem_pods
                
                except Exception as e:
                    problem_detail["pod_analysis_error"] = str(e)
            
            analysis["problematic_daemonsets"].append(problem_detail)
    
    # Calculate overall coverage percentage
    if analysis["coverage_analysis"]["total_desired_pods"] > 0:
        analysis["coverage_analysis"]["overall_coverage_percentage"] = (
            analysis["coverage_analysis"]["total_ready_pods"] / 
            analysis["coverage_analysis"]["total_desired_pods"]
        ) * 100
    
    # Sort detailed DaemonSets by health (problematic first)
    analysis["detailed_daemonsets"].sort(
        key=lambda x: (
            0 if "Critical" in x["status_analysis"]["health_status"] else
            1 if "Degraded" in x["status_analysis"]["health_status"] else
            2 if "No nodes" in x["status_analysis"]["health_status"] else 3
        )
    )
    
    return {"status": "success", "data": analysis}


analyze_daemonset_health_tool = StructuredTool.from_function(
    func=analyze_daemonset_health,
    name="analyze_daemonset_health",
    description="Provides comprehensive health analysis of DaemonSets including coverage metrics, update status, node distribution, and identification of problematic DaemonSets with detailed issue analysis.",
    args_schema=DaemonSetAnalysisInputSchema
)


@_handle_k8s_exceptions
def get_daemonset_pods(namespace: str, daemonset_name: str) -> Dict[str, Any]:
    """Gets all pods managed by a specific DaemonSet with detailed status information."""
    apps_v1 = get_apps_v1_api()
    core_v1 = get_core_v1_api()
    
    # Get DaemonSet to find selector
    daemonset = apps_v1.read_namespaced_daemon_set(name=daemonset_name, namespace=namespace)
    
    selector_labels = daemonset.spec.selector.match_labels if daemonset.spec.selector and daemonset.spec.selector.match_labels else {}
    label_selector = ','.join([f"{k}={v}" for k, v in selector_labels.items()])
    
    if not label_selector:
        return {
            "status": "error",
            "message": "DaemonSet has no selector labels",
            "error_type": "NoSelector"
        }
    
    # Get pods
    pods = core_v1.list_namespaced_pod(namespace=namespace, label_selector=label_selector, timeout_seconds=10)
    
    pod_details = []
    for pod in pods.items:
        pod_info = {
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "node_name": pod.spec.node_name,
            "phase": pod.status.phase,
            "creation_timestamp": pod.metadata.creation_timestamp.isoformat() if pod.metadata.creation_timestamp else None,
            "age": _calculate_age(pod.metadata.creation_timestamp),
            "labels": pod.metadata.labels or {},
            "annotations": pod.metadata.annotations or {},
            "pod_ip": pod.status.pod_ip,
            "host_ip": pod.status.host_ip,
            "qos_class": getattr(pod.status, 'qos_class', None),
            "restart_policy": pod.spec.restart_policy,
            "service_account": pod.spec.service_account_name,
            "containers": [],
            "conditions": [],
            "volumes": len(pod.spec.volumes) if pod.spec.volumes else 0
        }
        
        # Add container status information
        if pod.status.container_statuses:
            for container_status in pod.status.container_statuses:
                container_info = {
                    "name": container_status.name,
                    "image": container_status.image,
                    "image_id": container_status.image_id,
                    "ready": container_status.ready,
                    "started": getattr(container_status, 'started', None),
                    "restart_count": container_status.restart_count,
                    "state": {},
                    "last_state": {}
                }
                
                # Parse container state
                if container_status.state:
                    if container_status.state.running:
                        container_info["state"] = {
                            "status": "running",
                            "started_at": container_status.state.running.started_at.isoformat() if container_status.state.running.started_at else None
                        }
                    elif container_status.state.waiting:
                        container_info["state"] = {
                            "status": "waiting",
                            "reason": container_status.state.waiting.reason,
                            "message": container_status.state.waiting.message
                        }
                    elif container_status.state.terminated:
                        container_info["state"] = {
                            "status": "terminated",
                            "reason": container_status.state.terminated.reason,
                            "message": container_status.state.terminated.message,
                            "exit_code": container_status.state.terminated.exit_code,
                            "started_at": container_status.state.terminated.started_at.isoformat() if container_status.state.terminated.started_at else None,
                            "finished_at": container_status.state.terminated.finished_at.isoformat() if container_status.state.terminated.finished_at else None
                        }
                
                pod_info["containers"].append(container_info)
        
        # Add pod conditions
        if pod.status.conditions:
            for condition in pod.status.conditions:
                pod_info["conditions"].append({
                    "type": condition.type,
                    "status": condition.status,
                    "reason": getattr(condition, 'reason', None),
                    "message": getattr(condition, 'message', None),
                    "last_probe_time": condition.last_probe_time.isoformat() if getattr(condition, 'last_probe_time', None) else None,
                    "last_transition_time": condition.last_transition_time.isoformat() if getattr(condition, 'last_transition_time', None) else None
                })
        
        pod_details.append(pod_info)
    
    # Sort pods by node name then by name
    pod_details.sort(key=lambda x: (x["node_name"] or "", x["name"]))
    
    # Create node distribution summary
    node_distribution = {}
    for pod in pod_details:
        node = pod["node_name"] or "unscheduled"
        if node not in node_distribution:
            node_distribution[node] = {"total": 0, "running": 0, "pending": 0, "failed": 0}
        
        node_distribution[node]["total"] += 1
        phase = pod["phase"].lower()
        if phase == "running":
            node_distribution[node]["running"] += 1
        elif phase == "pending":
            node_distribution[node]["pending"] += 1
        elif phase == "failed":
            node_distribution[node]["failed"] += 1
    
    result_data = {
        "daemonset_name": daemonset_name,
        "namespace": namespace,
        "selector_labels": selector_labels,
        "pods": pod_details,
        "pod_count": len(pod_details),
        "node_distribution": node_distribution,
        "phase_summary": {
            "running": len([pod for pod in pod_details if pod["phase"] == "Running"]),
            "pending": len([pod for pod in pod_details if pod["phase"] == "Pending"]),
            "succeeded": len([pod for pod in pod_details if pod["phase"] == "Succeeded"]),
            "failed": len([pod for pod in pod_details if pod["phase"] == "Failed"]),
            "unknown": len([pod for pod in pod_details if pod["phase"] == "Unknown"])
        }
    }
    
    return {"status": "success", "data": result_data}


get_daemonset_pods_tool = StructuredTool.from_function(
    func=get_daemonset_pods,
    name="get_daemonset_pods",
    description="Gets all pods managed by a specific DaemonSet with comprehensive status information, container details, and node distribution analysis.",
    args_schema=DaemonSetInputSchema
)


# ===============================================================================
#                           TOOL COLLECTION
# ===============================================================================

# Export all DaemonSet tools for easy import
daemonset_tools = [
    connectivity_check_tool,
    list_daemonsets_tool,
    describe_daemonset_tool,
    analyze_daemonset_health_tool,
    get_daemonset_pods_tool,
]
