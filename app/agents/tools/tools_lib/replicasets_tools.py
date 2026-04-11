import json
from typing import List, Dict, Any, Optional

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


def _analyze_replicaset_status(replicaset) -> Dict[str, Any]:
    """Analyze ReplicaSet status and provide insights."""
    spec = replicaset.spec
    status = replicaset.status
    
    desired_replicas = spec.replicas or 0
    current_replicas = status.replicas or 0
    ready_replicas = status.ready_replicas or 0
    available_replicas = status.available_replicas or 0
    fully_labeled_replicas = status.fully_labeled_replicas or 0
    
    analysis = {
        "desired_replicas": desired_replicas,
        "current_replicas": current_replicas,
        "ready_replicas": ready_replicas,
        "available_replicas": available_replicas,
        "fully_labeled_replicas": fully_labeled_replicas,
        "observed_generation": status.observed_generation,
        "health_status": "Unknown",
        "scaling_status": "Unknown",
        "availability_percentage": 0,
        "readiness_percentage": 0
    }
    
    # Determine health status
    if desired_replicas == 0:
        analysis["health_status"] = "Scaled to Zero"
        analysis["scaling_status"] = "Scaled Down"
    elif ready_replicas == desired_replicas and available_replicas == desired_replicas:
        analysis["health_status"] = "Healthy"
        analysis["scaling_status"] = "Stable"
    elif ready_replicas == 0:
        analysis["health_status"] = "Critical - No pods ready"
        analysis["scaling_status"] = "Failed"
    elif ready_replicas < desired_replicas:
        analysis["health_status"] = "Degraded - Some pods not ready"
        if current_replicas < desired_replicas:
            analysis["scaling_status"] = "Scaling Up"
        else:
            analysis["scaling_status"] = "Pods Starting"
    elif current_replicas > desired_replicas:
        analysis["health_status"] = "Scaling Down"
        analysis["scaling_status"] = "Scaling Down"
    else:
        analysis["health_status"] = "Healthy"
        analysis["scaling_status"] = "Stable"
    
    # Calculate percentages
    if desired_replicas > 0:
        analysis["availability_percentage"] = (available_replicas / desired_replicas) * 100
        analysis["readiness_percentage"] = (ready_replicas / desired_replicas) * 100
    
    return analysis


def _find_owning_deployment(replicaset) -> Optional[Dict[str, str]]:
    """Find the Deployment that owns this ReplicaSet."""
    if replicaset.metadata.owner_references:
        for owner_ref in replicaset.metadata.owner_references:
            if owner_ref.kind == "Deployment" and owner_ref.api_version.startswith("apps/"):
                return {
                    "name": owner_ref.name,
                    "uid": owner_ref.uid,
                    "api_version": owner_ref.api_version,
                    "controller": getattr(owner_ref, 'controller', False),
                    "block_owner_deletion": getattr(owner_ref, 'block_owner_deletion', False)
                }
    return None


def _get_replicaset_conditions(replicaset) -> List[Dict[str, Any]]:
    """Extract and format ReplicaSet conditions."""
    conditions = []
    if hasattr(replicaset.status, 'conditions') and replicaset.status.conditions:
        for condition in replicaset.status.conditions:
            conditions.append({
                "type": condition.type,
                "status": condition.status,
                "reason": getattr(condition, 'reason', None),
                "message": getattr(condition, 'message', None),
                "last_transition_time": condition.last_transition_time.isoformat() if getattr(condition, 'last_transition_time', None) else None,
                "last_update_time": getattr(condition, 'last_update_time', None).isoformat() if getattr(condition, 'last_update_time', None) else None
            })
    return conditions


# ===============================================================================
#                               INPUT SCHEMAS
# ===============================================================================

class ReplicaSetInputSchema(BaseModel):
    """Schema for ReplicaSet-specific operations."""
    namespace: str = Field(description="The Kubernetes namespace where the ReplicaSet is located.")
    replicaset_name: str = Field(description="The name of the ReplicaSet.")


class ReplicaSetAnalysisInputSchema(BaseModel):
    """Schema for ReplicaSet analysis tools."""
    namespace: Optional[str] = Field(default=None, description="The Kubernetes namespace to analyze. If not provided, analyzes all namespaces.")
    include_pod_details: Optional[bool] = Field(default=True, description="Include detailed pod information in the analysis.")
    deployment_filter: Optional[str] = Field(default=None, description="Filter ReplicaSets by owning Deployment name.")


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
#                              REPLICASET TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def list_replicasets(namespace: Optional[str] = None) -> Dict[str, Any]:
    """Lists all ReplicaSets in a namespace or across all namespaces."""
    apps_v1 = get_apps_v1_api()
    
    if namespace:
        replicaset_list = apps_v1.list_namespaced_replica_set(namespace=namespace, timeout_seconds=10)
    else:
        replicaset_list = apps_v1.list_replica_set_for_all_namespaces(timeout_seconds=10)
    
    replicasets = []
    for rs in replicaset_list.items:
        # Analyze ReplicaSet status
        status_analysis = _analyze_replicaset_status(rs)
        
        # Find owning Deployment
        owning_deployment = _find_owning_deployment(rs)
        
        replicaset_info = {
            "name": rs.metadata.name,
            "namespace": rs.metadata.namespace,
            "creation_timestamp": rs.metadata.creation_timestamp.isoformat() if rs.metadata.creation_timestamp else None,
            "age": _calculate_age(rs.metadata.creation_timestamp),
            "labels": rs.metadata.labels or {},
            "annotations": rs.metadata.annotations or {},
            "selector": rs.spec.selector.match_labels if rs.spec.selector and rs.spec.selector.match_labels else {},
            "template_hash": rs.metadata.labels.get("pod-template-hash") if rs.metadata.labels else None,
            "owning_deployment": owning_deployment,
            "status_analysis": status_analysis,
            "containers": []
        }
        
        # Add container information
        if rs.spec.template.spec.containers:
            for container in rs.spec.template.spec.containers:
                container_info = {
                    "name": container.name,
                    "image": container.image,
                    "ports": [{"container_port": port.container_port, "name": getattr(port, 'name', None)} 
                             for port in (container.ports or [])],
                    "resources": {
                        "requests": container.resources.requests if container.resources and container.resources.requests else {},
                        "limits": container.resources.limits if container.resources and container.resources.limits else {}
                    } if container.resources else {"requests": {}, "limits": {}},
                    "volume_mounts": len(container.volume_mounts) if container.volume_mounts else 0,
                    "env_vars": len(container.env) if container.env else 0
                }
                replicaset_info["containers"].append(container_info)
        
        replicasets.append(replicaset_info)
    
    # Sort by creation time (newest first)
    replicasets.sort(key=lambda x: x["creation_timestamp"] or "", reverse=True)
    
    # Prepare result data
    if namespace:
        result_data = {
            "namespace": namespace,
            "replicasets": replicasets,
            "replicaset_count": len(replicasets)
        }
    else:
        # Group by namespace
        replicasets_by_namespace = {}
        for rs in replicasets:
            ns = rs["namespace"]
            if ns not in replicasets_by_namespace:
                replicasets_by_namespace[ns] = []
            replicasets_by_namespace[ns].append(rs)
        
        result_data = {
            "replicasets_by_namespace": replicasets_by_namespace,
            "total_replicaset_count": len(replicasets),
            "namespace_count": len(replicasets_by_namespace)
        }
    
    # Add health summary
    health_summary = {
        "healthy": len([rs for rs in replicasets if rs["status_analysis"]["health_status"] == "Healthy"]),
        "degraded": len([rs for rs in replicasets if "Degraded" in rs["status_analysis"]["health_status"]]),
        "critical": len([rs for rs in replicasets if "Critical" in rs["status_analysis"]["health_status"]]),
        "scaled_to_zero": len([rs for rs in replicasets if rs["status_analysis"]["health_status"] == "Scaled to Zero"]),
        "scaling": len([rs for rs in replicasets if "Scaling" in rs["status_analysis"]["health_status"]])
    }
    result_data["health_summary"] = health_summary
    
    # Add deployment relationship summary
    deployment_summary = {
        "with_deployment": len([rs for rs in replicasets if rs["owning_deployment"]]),
        "orphaned": len([rs for rs in replicasets if not rs["owning_deployment"]]),
        "unique_deployments": len(set(rs["owning_deployment"]["name"] 
                                    for rs in replicasets 
                                    if rs["owning_deployment"]))
    }
    result_data["deployment_summary"] = deployment_summary
    
    return {"status": "success", "data": result_data}


list_replicasets_tool = StructuredTool.from_function(
    func=list_replicasets,
    name="list_replicasets",
    description="Lists all Kubernetes ReplicaSets in a namespace or across all namespaces with comprehensive status analysis, container information, deployment relationships, and health summaries.",
    args_schema=NamespaceOptionalInputSchema
)


@_handle_k8s_exceptions
def describe_replicaset(namespace: str, replicaset_name: str) -> Dict[str, Any]:
    """Gets detailed information about a specific ReplicaSet."""
    apps_v1 = get_apps_v1_api()
    core_v1 = get_core_v1_api()
    
    # Get ReplicaSet details
    replicaset = apps_v1.read_namespaced_replica_set(name=replicaset_name, namespace=namespace)
    
    # Analyze ReplicaSet status
    status_analysis = _analyze_replicaset_status(replicaset)
    
    # Find owning Deployment
    owning_deployment = _find_owning_deployment(replicaset)
    
    # Get conditions
    conditions = _get_replicaset_conditions(replicaset)
    
    replicaset_details = {
        "name": replicaset.metadata.name,
        "namespace": replicaset.metadata.namespace,
        "uid": replicaset.metadata.uid,
        "resource_version": replicaset.metadata.resource_version,
        "generation": replicaset.metadata.generation,
        "creation_timestamp": replicaset.metadata.creation_timestamp.isoformat() if replicaset.metadata.creation_timestamp else None,
        "age": _calculate_age(replicaset.metadata.creation_timestamp),
        "labels": replicaset.metadata.labels or {},
        "annotations": replicaset.metadata.annotations or {},
        "selector": replicaset.spec.selector.match_labels if replicaset.spec.selector and replicaset.spec.selector.match_labels else {},
        "template_hash": replicaset.metadata.labels.get("pod-template-hash") if replicaset.metadata.labels else None,
        "owning_deployment": owning_deployment,
        "min_ready_seconds": getattr(replicaset.spec, 'min_ready_seconds', 0),
        "status_analysis": status_analysis,
        "conditions": conditions,
        "containers": [],
        "volumes": [],
        "pod_template_spec": {}
    }
    
    # Add container information
    if replicaset.spec.template.spec.containers:
        for container in replicaset.spec.template.spec.containers:
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
                "startup_probe": str(getattr(container, 'startup_probe', None)) if getattr(container, 'startup_probe', None) else None,
                "security_context": str(container.security_context) if container.security_context else None
            }
            replicaset_details["containers"].append(container_info)
    
    # Add volume information
    if replicaset.spec.template.spec.volumes:
        for volume in replicaset.spec.template.spec.volumes:
            volume_info = {
                "name": volume.name,
                "type": "Unknown"
            }
            
            # Determine volume type
            if volume.empty_dir:
                volume_info.update({
                    "type": "EmptyDir",
                    "size_limit": getattr(volume.empty_dir, 'size_limit', None)
                })
            elif volume.host_path:
                volume_info.update({
                    "type": "HostPath",
                    "path": volume.host_path.path,
                    "host_path_type": getattr(volume.host_path, 'type', None)
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
            
            replicaset_details["volumes"].append(volume_info)
    
    # Add pod template spec summary
    pod_spec = replicaset.spec.template.spec
    replicaset_details["pod_template_spec"] = {
        "restart_policy": pod_spec.restart_policy,
        "dns_policy": getattr(pod_spec, 'dns_policy', None),
        "node_selector": pod_spec.node_selector or {},
        "service_account_name": pod_spec.service_account_name,
        "security_context": str(pod_spec.security_context) if pod_spec.security_context else None,
        "termination_grace_period_seconds": getattr(pod_spec, 'termination_grace_period_seconds', 30),
        "tolerations": [
            {
                "key": toleration.key,
                "operator": toleration.operator,
                "value": toleration.value,
                "effect": toleration.effect,
                "toleration_seconds": toleration.toleration_seconds
            } for toleration in (pod_spec.tolerations or [])
        ]
    }
    
    # Get pods managed by this ReplicaSet
    try:
        selector_labels = replicaset.spec.selector.match_labels if replicaset.spec.selector and replicaset.spec.selector.match_labels else {}
        label_selector = ','.join([f"{k}={v}" for k, v in selector_labels.items()])
        
        if label_selector:
            pods = core_v1.list_namespaced_pod(namespace=namespace, label_selector=label_selector, timeout_seconds=10)
            
            pod_summary = {
                "total_pods": len(pods.items),
                "running_pods": len([pod for pod in pods.items if pod.status.phase == "Running"]),
                "pending_pods": len([pod for pod in pods.items if pod.status.phase == "Pending"]),
                "failed_pods": len([pod for pod in pods.items if pod.status.phase == "Failed"]),
                "succeeded_pods": len([pod for pod in pods.items if pod.status.phase == "Succeeded"]),
                "pod_names": [pod.metadata.name for pod in pods.items]
            }
            replicaset_details["pod_summary"] = pod_summary
            
            # Node distribution
            node_distribution = {}
            for pod in pods.items:
                node = pod.spec.node_name or "unscheduled"
                if node not in node_distribution:
                    node_distribution[node] = 0
                node_distribution[node] += 1
            
            replicaset_details["node_distribution"] = node_distribution
        else:
            replicaset_details["pod_summary"] = {"error": "No selector labels found"}
            replicaset_details["node_distribution"] = {}
    
    except Exception as e:
        replicaset_details["pod_summary"] = {"error": str(e)}
        replicaset_details["node_distribution"] = {}
    
    # If owned by a Deployment, get Deployment information
    if owning_deployment:
        try:
            deployment = apps_v1.read_namespaced_deployment(name=owning_deployment["name"], namespace=namespace)
            replicaset_details["deployment_info"] = {
                "name": deployment.metadata.name,
                "generation": deployment.metadata.generation,
                "observed_generation": deployment.status.observed_generation,
                "replicas": deployment.spec.replicas,
                "strategy": deployment.spec.strategy.type if deployment.spec.strategy else "RollingUpdate",
                "ready_replicas": deployment.status.ready_replicas or 0,
                "updated_replicas": deployment.status.updated_replicas or 0,
                "available_replicas": deployment.status.available_replicas or 0,
                "unavailable_replicas": deployment.status.unavailable_replicas or 0
            }
        except Exception as e:
            replicaset_details["deployment_info"] = {"error": str(e)}
    
    return {"status": "success", "data": replicaset_details}


describe_replicaset_tool = StructuredTool.from_function(
    func=describe_replicaset,
    name="describe_replicaset",
    description="Gets comprehensive detailed information about a specific Kubernetes ReplicaSet including containers, volumes, pod template specs, deployment relationships, and pod distribution analysis.",
    args_schema=ReplicaSetInputSchema
)


@_handle_k8s_exceptions
def analyze_replicaset_health(namespace: Optional[str] = None, include_pod_details: bool = True, deployment_filter: Optional[str] = None) -> Dict[str, Any]:
    """Analyzes the health and scaling patterns of ReplicaSets across the cluster or namespace."""
    apps_v1 = get_apps_v1_api()
    core_v1 = get_core_v1_api()
    
    # Get all ReplicaSets
    if namespace:
        replicaset_list = apps_v1.list_namespaced_replica_set(namespace=namespace, timeout_seconds=10)
    else:
        replicaset_list = apps_v1.list_replica_set_for_all_namespaces(timeout_seconds=10)
    
    # Filter by deployment if specified
    filtered_replicasets = []
    for rs in replicaset_list.items:
        if deployment_filter:
            owning_deployment = _find_owning_deployment(rs)
            if owning_deployment and owning_deployment["name"] == deployment_filter:
                filtered_replicasets.append(rs)
        else:
            filtered_replicasets.append(rs)
    
    analysis = {
        "total_replicasets": len(filtered_replicasets),
        "health_breakdown": {
            "healthy": 0,
            "degraded": 0,
            "critical": 0,
            "scaled_to_zero": 0,
            "scaling": 0
        },
        "scaling_analysis": {
            "total_desired_replicas": 0,
            "total_ready_replicas": 0,
            "total_available_replicas": 0,
            "average_availability_percentage": 0,
            "average_readiness_percentage": 0
        },
        "deployment_analysis": {
            "managed_by_deployment": 0,
            "orphaned_replicasets": 0,
            "unique_deployments": set()
        },
        "resource_analysis": {
            "total_containers": 0,
            "total_volumes": 0,
            "resource_requests": {"cpu": 0, "memory": 0},
            "resource_limits": {"cpu": 0, "memory": 0}
        },
        "detailed_replicasets": [],
        "problematic_replicasets": []
    }
    
    if not namespace:
        analysis["namespace_breakdown"] = {}
    
    total_availability_percentage = 0
    total_readiness_percentage = 0
    
    for rs in filtered_replicasets:
        status_analysis = _analyze_replicaset_status(rs)
        owning_deployment = _find_owning_deployment(rs)
        
        # Update health breakdown
        health_status = status_analysis["health_status"]
        if health_status == "Healthy":
            analysis["health_breakdown"]["healthy"] += 1
        elif "Degraded" in health_status:
            analysis["health_breakdown"]["degraded"] += 1
        elif "Critical" in health_status:
            analysis["health_breakdown"]["critical"] += 1
        elif health_status == "Scaled to Zero":
            analysis["health_breakdown"]["scaled_to_zero"] += 1
        elif "Scaling" in health_status:
            analysis["health_breakdown"]["scaling"] += 1
        
        # Update scaling analysis
        analysis["scaling_analysis"]["total_desired_replicas"] += status_analysis["desired_replicas"]
        analysis["scaling_analysis"]["total_ready_replicas"] += status_analysis["ready_replicas"]
        analysis["scaling_analysis"]["total_available_replicas"] += status_analysis["available_replicas"]
        total_availability_percentage += status_analysis["availability_percentage"]
        total_readiness_percentage += status_analysis["readiness_percentage"]
        
        # Update deployment analysis
        if owning_deployment:
            analysis["deployment_analysis"]["managed_by_deployment"] += 1
            analysis["deployment_analysis"]["unique_deployments"].add(owning_deployment["name"])
        else:
            analysis["deployment_analysis"]["orphaned_replicasets"] += 1
        
        # Update resource analysis
        if rs.spec.template.spec.containers:
            analysis["resource_analysis"]["total_containers"] += len(rs.spec.template.spec.containers)
            
            for container in rs.spec.template.spec.containers:
                if container.resources:
                    if container.resources.requests:
                        cpu_req = container.resources.requests.get("cpu", "0")
                        mem_req = container.resources.requests.get("memory", "0")
                        # Basic parsing - could be enhanced
                        if cpu_req and cpu_req != "0":
                            analysis["resource_analysis"]["resource_requests"]["cpu"] += 1
                        if mem_req and mem_req != "0":
                            analysis["resource_analysis"]["resource_requests"]["memory"] += 1
                    
                    if container.resources.limits:
                        cpu_limit = container.resources.limits.get("cpu", "0")
                        mem_limit = container.resources.limits.get("memory", "0")
                        if cpu_limit and cpu_limit != "0":
                            analysis["resource_analysis"]["resource_limits"]["cpu"] += 1
                        if mem_limit and mem_limit != "0":
                            analysis["resource_analysis"]["resource_limits"]["memory"] += 1
        
        if rs.spec.template.spec.volumes:
            analysis["resource_analysis"]["total_volumes"] += len(rs.spec.template.spec.volumes)
        
        # Namespace breakdown
        if not namespace:
            ns = rs.metadata.namespace
            if ns not in analysis["namespace_breakdown"]:
                analysis["namespace_breakdown"][ns] = {
                    "replicasets": 0,
                    "healthy": 0,
                    "degraded": 0,
                    "critical": 0,
                    "desired_replicas": 0,
                    "ready_replicas": 0
                }
            
            analysis["namespace_breakdown"][ns]["replicasets"] += 1
            analysis["namespace_breakdown"][ns]["desired_replicas"] += status_analysis["desired_replicas"]
            analysis["namespace_breakdown"][ns]["ready_replicas"] += status_analysis["ready_replicas"]
            
            if health_status == "Healthy":
                analysis["namespace_breakdown"][ns]["healthy"] += 1
            elif "Degraded" in health_status:
                analysis["namespace_breakdown"][ns]["degraded"] += 1
            elif "Critical" in health_status:
                analysis["namespace_breakdown"][ns]["critical"] += 1
        
        # Detailed ReplicaSet info
        rs_detail = {
            "name": rs.metadata.name,
            "namespace": rs.metadata.namespace,
            "age": _calculate_age(rs.metadata.creation_timestamp),
            "status_analysis": status_analysis,
            "owning_deployment": owning_deployment,
            "containers": len(rs.spec.template.spec.containers) if rs.spec.template.spec.containers else 0,
            "template_hash": rs.metadata.labels.get("pod-template-hash") if rs.metadata.labels else None
        }
        
        analysis["detailed_replicasets"].append(rs_detail)
        
        # Track problematic ReplicaSets
        if health_status not in ["Healthy", "Scaled to Zero"]:
            problem_detail = rs_detail.copy()
            problem_detail["issue_type"] = health_status
            
            # Get pod details if requested and there are issues
            if include_pod_details:
                try:
                    selector_labels = rs.spec.selector.match_labels if rs.spec.selector and rs.spec.selector.match_labels else {}
                    label_selector = ','.join([f"{k}={v}" for k, v in selector_labels.items()])
                    
                    if label_selector:
                        pods = core_v1.list_namespaced_pod(namespace=rs.metadata.namespace, label_selector=label_selector, timeout_seconds=10)
                        
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
            
            analysis["problematic_replicasets"].append(problem_detail)
    
    # Calculate averages
    if analysis["total_replicasets"] > 0:
        analysis["scaling_analysis"]["average_availability_percentage"] = total_availability_percentage / analysis["total_replicasets"]
        analysis["scaling_analysis"]["average_readiness_percentage"] = total_readiness_percentage / analysis["total_replicasets"]
    
    # Convert set to count for JSON serialization
    analysis["deployment_analysis"]["unique_deployment_count"] = len(analysis["deployment_analysis"]["unique_deployments"])
    analysis["deployment_analysis"]["unique_deployments"] = list(analysis["deployment_analysis"]["unique_deployments"])
    
    # Sort detailed ReplicaSets by health (problematic first)
    analysis["detailed_replicasets"].sort(
        key=lambda x: (
            0 if "Critical" in x["status_analysis"]["health_status"] else
            1 if "Degraded" in x["status_analysis"]["health_status"] else
            2 if "Scaling" in x["status_analysis"]["health_status"] else
            3 if x["status_analysis"]["health_status"] == "Scaled to Zero" else 4
        )
    )
    
    return {"status": "success", "data": analysis}


analyze_replicaset_health_tool = StructuredTool.from_function(
    func=analyze_replicaset_health,
    name="analyze_replicaset_health",
    description="Provides comprehensive health analysis of ReplicaSets including scaling metrics, deployment relationships, resource usage, and identification of problematic ReplicaSets with detailed issue analysis.",
    args_schema=ReplicaSetAnalysisInputSchema
)


@_handle_k8s_exceptions
def get_replicaset_pods(namespace: str, replicaset_name: str) -> Dict[str, Any]:
    """Gets all pods managed by a specific ReplicaSet with detailed status information."""
    apps_v1 = get_apps_v1_api()
    core_v1 = get_core_v1_api()
    
    # Get ReplicaSet to find selector
    replicaset = apps_v1.read_namespaced_replica_set(name=replicaset_name, namespace=namespace)
    
    selector_labels = replicaset.spec.selector.match_labels if replicaset.spec.selector and replicaset.spec.selector.match_labels else {}
    label_selector = ','.join([f"{k}={v}" for k, v in selector_labels.items()])
    
    if not label_selector:
        return {
            "status": "error",
            "message": "ReplicaSet has no selector labels",
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
    
    # Get ReplicaSet status for context
    status_analysis = _analyze_replicaset_status(replicaset)
    owning_deployment = _find_owning_deployment(replicaset)
    
    result_data = {
        "replicaset_name": replicaset_name,
        "namespace": namespace,
        "selector_labels": selector_labels,
        "template_hash": replicaset.metadata.labels.get("pod-template-hash") if replicaset.metadata.labels else None,
        "owning_deployment": owning_deployment,
        "status_analysis": status_analysis,
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


get_replicaset_pods_tool = StructuredTool.from_function(
    func=get_replicaset_pods,
    name="get_replicaset_pods",
    description="Gets all pods managed by a specific ReplicaSet with comprehensive status information, container details, node distribution analysis, and deployment relationship context.",
    args_schema=ReplicaSetInputSchema
)


# ===============================================================================
#                           TOOL COLLECTION
# ===============================================================================

# Export all ReplicaSet tools for easy import
replicasets_tools = [
    connectivity_check_tool,
    list_replicasets_tool,
    describe_replicaset_tool,
    analyze_replicaset_health_tool,
    get_replicaset_pods_tool,
]
