import json
from typing import List, Dict, Any, Optional

from kubernetes import client
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.agents.tools.tools_lib._base import (
    get_core_v1_api,
    _handle_k8s_exceptions,
    _parse_storage_size,
    NoArgumentsInputSchema,
    NamespaceOptionalInputSchema,
    calculate_age as _calculate_age,
)
from app.services import kubernetes_service
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")


def _format_storage_size(size_bytes: int) -> str:
    """Format storage size in bytes to human-readable format."""
    if size_bytes == 0:
        return "0B"
    
    units = ['B', 'Ki', 'Mi', 'Gi', 'Ti', 'Pi']
    size = float(size_bytes)
    unit_index = 0
    
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    
    if size == int(size):
        return f"{int(size)}{units[unit_index]}"
    else:
        return f"{size:.2f}{units[unit_index]}"


def _analyze_storage_usage(pvs: List[Dict], pvcs: List[Dict]) -> Dict[str, Any]:
    """Analyze storage usage patterns across PVs and PVCs."""
    analysis = {
        "total_pvs": len(pvs),
        "total_pvcs": len(pvcs),
        "bound_pvs": 0,
        "available_pvs": 0,
        "failed_pvs": 0,
        "bound_pvcs": 0,
        "pending_pvcs": 0,
        "lost_pvcs": 0,
        "total_capacity_bytes": 0,
        "used_capacity_bytes": 0,
        "storage_classes": {},
        "access_modes": {},
        "reclaim_policies": {}
    }
    
    # Analyze PVs
    for pv in pvs:
        capacity_str = pv.get("capacity", {}).get("storage", "0")
        capacity_bytes = _parse_storage_size(capacity_str)
        analysis["total_capacity_bytes"] += capacity_bytes
        
        status = pv.get("status", "Unknown")
        if status == "Bound":
            analysis["bound_pvs"] += 1
            analysis["used_capacity_bytes"] += capacity_bytes
        elif status == "Available":
            analysis["available_pvs"] += 1
        elif status == "Failed":
            analysis["failed_pvs"] += 1
        
        # Storage class analysis
        storage_class = pv.get("storage_class") or "default"
        if storage_class not in analysis["storage_classes"]:
            analysis["storage_classes"][storage_class] = {
                "pv_count": 0,
                "capacity_bytes": 0
            }
        analysis["storage_classes"][storage_class]["pv_count"] += 1
        analysis["storage_classes"][storage_class]["capacity_bytes"] += capacity_bytes
        
        # Access modes analysis
        access_modes = pv.get("access_modes", [])
        for mode in access_modes:
            analysis["access_modes"][mode] = analysis["access_modes"].get(mode, 0) + 1
        
        # Reclaim policy analysis
        reclaim_policy = pv.get("reclaim_policy", "Unknown")
        analysis["reclaim_policies"][reclaim_policy] = analysis["reclaim_policies"].get(reclaim_policy, 0) + 1
    
    # Analyze PVCs
    for pvc in pvcs:
        status = pvc.get("status", "Unknown")
        if status == "Bound":
            analysis["bound_pvcs"] += 1
        elif status == "Pending":
            analysis["pending_pvcs"] += 1
        elif status == "Lost":
            analysis["lost_pvcs"] += 1
    
    # Calculate efficiency metrics
    if analysis["total_capacity_bytes"] > 0:
        analysis["utilization_percentage"] = (analysis["used_capacity_bytes"] / analysis["total_capacity_bytes"]) * 100
    else:
        analysis["utilization_percentage"] = 0
    
    # Format sizes for readability
    analysis["total_capacity_formatted"] = _format_storage_size(analysis["total_capacity_bytes"])
    analysis["used_capacity_formatted"] = _format_storage_size(analysis["used_capacity_bytes"])
    analysis["available_capacity_bytes"] = analysis["total_capacity_bytes"] - analysis["used_capacity_bytes"]
    analysis["available_capacity_formatted"] = _format_storage_size(analysis["available_capacity_bytes"])
    
    return analysis


# ===============================================================================
#                               INPUT SCHEMAS
# ===============================================================================

class PVInputSchema(BaseModel):
    """Schema for PersistentVolume-specific operations."""
    pv_name: str = Field(description="The name of the PersistentVolume.")


class PVCInputSchema(BaseModel):
    """Schema for PersistentVolumeClaim-specific operations."""
    namespace: str = Field(description="The Kubernetes namespace where the PVC is located.")
    pvc_name: str = Field(description="The name of the PersistentVolumeClaim.")


class StorageAnalysisInputSchema(BaseModel):
    """Schema for storage analysis tools."""
    include_usage_details: Optional[bool] = Field(default=True, description="Include detailed usage information in the analysis.")
    namespace: Optional[str] = Field(default=None, description="Limit analysis to a specific namespace. If not provided, analyzes all namespaces.")


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
#                      PERSISTENT VOLUME TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def list_persistent_volumes() -> Dict[str, Any]:
    """Lists all PersistentVolumes in the cluster with detailed information."""
    core_v1 = get_core_v1_api()
    
    pv_list = core_v1.list_persistent_volume(timeout_seconds=10)
    
    pvs = []
    for pv in pv_list.items:
        pv_info = {
            "name": pv.metadata.name,
            "capacity": pv.spec.capacity or {},
            "access_modes": pv.spec.access_modes or [],
            "storage_class": pv.spec.storage_class_name,
            "status": pv.status.phase,
            "reclaim_policy": pv.spec.persistent_volume_reclaim_policy,
            "creation_timestamp": pv.metadata.creation_timestamp.isoformat() if pv.metadata.creation_timestamp else None,
            "age": _calculate_age(pv.metadata.creation_timestamp),
            "labels": pv.metadata.labels or {},
            "annotations": pv.metadata.annotations or {},
            "volume_mode": getattr(pv.spec, 'volume_mode', 'Filesystem'),
            "mount_options": getattr(pv.spec, 'mount_options', []),
            "claim_ref": {
                "namespace": pv.spec.claim_ref.namespace if pv.spec.claim_ref else None,
                "name": pv.spec.claim_ref.name if pv.spec.claim_ref else None,
                "uid": pv.spec.claim_ref.uid if pv.spec.claim_ref else None
            } if pv.spec.claim_ref else None
        }
        
        # Add volume source information
        if pv.spec.host_path:
            pv_info["volume_source"] = {
                "type": "HostPath",
                "path": pv.spec.host_path.path
            }
        elif pv.spec.nfs:
            pv_info["volume_source"] = {
                "type": "NFS",
                "server": pv.spec.nfs.server,
                "path": pv.spec.nfs.path
            }
        elif pv.spec.csi:
            pv_info["volume_source"] = {
                "type": "CSI",
                "driver": pv.spec.csi.driver,
                "volume_handle": pv.spec.csi.volume_handle
            }
        else:
            pv_info["volume_source"] = {"type": "Other"}
        
        pvs.append(pv_info)
    
    # Sort by creation time (newest first)
    pvs.sort(key=lambda x: x["creation_timestamp"] or "", reverse=True)
    
    return {
        "status": "success", 
        "data": {
            "persistent_volumes": pvs,
            "total_count": len(pvs),
            "status_summary": {
                "available": len([pv for pv in pvs if pv["status"] == "Available"]),
                "bound": len([pv for pv in pvs if pv["status"] == "Bound"]),
                "released": len([pv for pv in pvs if pv["status"] == "Released"]),
                "failed": len([pv for pv in pvs if pv["status"] == "Failed"])
            }
        }
    }


list_persistent_volumes_tool = StructuredTool.from_function(
    func=list_persistent_volumes,
    name="list_persistent_volumes",
    description="Lists all PersistentVolumes in the cluster with comprehensive information including capacity, access modes, storage classes, and volume sources.",
    args_schema=NoArgumentsInputSchema
)


@_handle_k8s_exceptions
def describe_persistent_volume(pv_name: str) -> Dict[str, Any]:
    """Gets detailed information about a specific PersistentVolume."""
    core_v1 = get_core_v1_api()
    
    pv = core_v1.read_persistent_volume(name=pv_name)
    
    pv_details = {
        "name": pv.metadata.name,
        "uid": pv.metadata.uid,
        "resource_version": pv.metadata.resource_version,
        "creation_timestamp": pv.metadata.creation_timestamp.isoformat() if pv.metadata.creation_timestamp else None,
        "age": _calculate_age(pv.metadata.creation_timestamp),
        "labels": pv.metadata.labels or {},
        "annotations": pv.metadata.annotations or {},
        "finalizers": pv.metadata.finalizers or [],
        "capacity": pv.spec.capacity or {},
        "access_modes": pv.spec.access_modes or [],
        "storage_class": pv.spec.storage_class_name,
        "status": pv.status.phase,
        "message": pv.status.message if hasattr(pv.status, 'message') else None,
        "reason": pv.status.reason if hasattr(pv.status, 'reason') else None,
        "reclaim_policy": pv.spec.persistent_volume_reclaim_policy,
        "volume_mode": getattr(pv.spec, 'volume_mode', 'Filesystem'),
        "mount_options": getattr(pv.spec, 'mount_options', []),
        "node_affinity": None,
        "claim_ref": None,
        "volume_source": {}
    }
    
    # Add claim reference details
    if pv.spec.claim_ref:
        pv_details["claim_ref"] = {
            "namespace": pv.spec.claim_ref.namespace,
            "name": pv.spec.claim_ref.name,
            "uid": pv.spec.claim_ref.uid,
            "resource_version": pv.spec.claim_ref.resource_version,
            "api_version": pv.spec.claim_ref.api_version,
            "kind": pv.spec.claim_ref.kind
        }
    
    # Add node affinity
    if pv.spec.node_affinity:
        pv_details["node_affinity"] = {
            "required": pv.spec.node_affinity.required.node_selector_terms if pv.spec.node_affinity.required else None
        }
    
    # Add detailed volume source information
    if pv.spec.host_path:
        pv_details["volume_source"] = {
            "type": "HostPath",
            "path": pv.spec.host_path.path,
            "type_field": getattr(pv.spec.host_path, 'type', None)
        }
    elif pv.spec.nfs:
        pv_details["volume_source"] = {
            "type": "NFS",
            "server": pv.spec.nfs.server,
            "path": pv.spec.nfs.path,
            "read_only": getattr(pv.spec.nfs, 'read_only', False)
        }
    elif pv.spec.csi:
        pv_details["volume_source"] = {
            "type": "CSI",
            "driver": pv.spec.csi.driver,
            "volume_handle": pv.spec.csi.volume_handle,
            "read_only": getattr(pv.spec.csi, 'read_only', False),
            "fs_type": getattr(pv.spec.csi, 'fs_type', None),
            "volume_attributes": getattr(pv.spec.csi, 'volume_attributes', {}) or {}
        }
    elif pv.spec.aws_elastic_block_store:
        ebs = pv.spec.aws_elastic_block_store
        pv_details["volume_source"] = {
            "type": "AWSElasticBlockStore",
            "volume_id": ebs.volume_id,
            "fs_type": getattr(ebs, 'fs_type', 'ext4'),
            "partition": getattr(ebs, 'partition', 0),
            "read_only": getattr(ebs, 'read_only', False)
        }
    elif pv.spec.gce_persistent_disk:
        gce = pv.spec.gce_persistent_disk
        pv_details["volume_source"] = {
            "type": "GCEPersistentDisk",
            "pd_name": gce.pd_name,
            "fs_type": getattr(gce, 'fs_type', 'ext4'),
            "partition": getattr(gce, 'partition', 0),
            "read_only": getattr(gce, 'read_only', False)
        }
    else:
        pv_details["volume_source"] = {"type": "Other/Unknown"}
    
    # If bound, try to get PVC information
    if pv_details["claim_ref"] and pv_details["status"] == "Bound":
        try:
            pvc = core_v1.read_namespaced_persistent_volume_claim(
                name=pv_details["claim_ref"]["name"],
                namespace=pv_details["claim_ref"]["namespace"]
            )
            pv_details["bound_pvc_details"] = {
                "name": pvc.metadata.name,
                "namespace": pvc.metadata.namespace,
                "status": pvc.status.phase,
                "requested_capacity": pvc.spec.resources.requests if pvc.spec.resources else {},
                "access_modes": pvc.spec.access_modes or [],
                "storage_class": pvc.spec.storage_class_name,
                "creation_timestamp": pvc.metadata.creation_timestamp.isoformat() if pvc.metadata.creation_timestamp else None
            }
        except Exception as e:
            pv_details["bound_pvc_details"] = f"Error retrieving PVC details: {str(e)}"
    
    return {"status": "success", "data": pv_details}


describe_persistent_volume_tool = StructuredTool.from_function(
    func=describe_persistent_volume,
    name="describe_persistent_volume",
    description="Gets comprehensive detailed information about a specific PersistentVolume including volume source, claim references, and bound PVC details.",
    args_schema=PVInputSchema
)


# ===============================================================================
#                  PERSISTENT VOLUME CLAIM TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def list_persistent_volume_claims(namespace: Optional[str] = None) -> Dict[str, Any]:
    """Lists all PersistentVolumeClaims in a namespace or across all namespaces."""
    core_v1 = get_core_v1_api()
    
    if namespace:
        pvc_list = core_v1.list_namespaced_persistent_volume_claim(namespace=namespace, timeout_seconds=10)
    else:
        pvc_list = core_v1.list_persistent_volume_claim_for_all_namespaces(timeout_seconds=10)
    
    pvcs = []
    for pvc in pvc_list.items:
        pvc_info = {
            "name": pvc.metadata.name,
            "namespace": pvc.metadata.namespace,
            "status": pvc.status.phase,
            "volume_name": pvc.spec.volume_name,
            "storage_class": pvc.spec.storage_class_name,
            "access_modes": pvc.spec.access_modes or [],
            "requested_storage": pvc.spec.resources.requests.get("storage") if pvc.spec.resources and pvc.spec.resources.requests else None,
            "allocated_storage": pvc.status.capacity.get("storage") if pvc.status.capacity else None,
            "creation_timestamp": pvc.metadata.creation_timestamp.isoformat() if pvc.metadata.creation_timestamp else None,
            "age": _calculate_age(pvc.metadata.creation_timestamp),
            "labels": pvc.metadata.labels or {},
            "annotations": pvc.metadata.annotations or {},
            "volume_mode": getattr(pvc.spec, 'volume_mode', 'Filesystem'),
            "conditions": []
        }
        
        # Add conditions if available
        if hasattr(pvc.status, 'conditions') and pvc.status.conditions:
            for condition in pvc.status.conditions:
                pvc_info["conditions"].append({
                    "type": condition.type,
                    "status": condition.status,
                    "reason": getattr(condition, 'reason', None),
                    "message": getattr(condition, 'message', None),
                    "last_probe_time": condition.last_probe_time.isoformat() if getattr(condition, 'last_probe_time', None) else None,
                    "last_transition_time": condition.last_transition_time.isoformat() if getattr(condition, 'last_transition_time', None) else None
                })
        
        pvcs.append(pvc_info)
    
    # Sort by creation time (newest first)
    pvcs.sort(key=lambda x: x["creation_timestamp"] or "", reverse=True)
    
    # Prepare result data
    if namespace:
        result_data = {
            "namespace": namespace,
            "persistent_volume_claims": pvcs,
            "total_count": len(pvcs)
        }
    else:
        # Group by namespace
        pvcs_by_namespace = {}
        for pvc in pvcs:
            ns = pvc["namespace"]
            if ns not in pvcs_by_namespace:
                pvcs_by_namespace[ns] = []
            pvcs_by_namespace[ns].append(pvc)
        
        result_data = {
            "persistent_volume_claims_by_namespace": pvcs_by_namespace,
            "total_count": len(pvcs),
            "namespace_count": len(pvcs_by_namespace)
        }
    
    # Add status summary
    status_summary = {
        "bound": len([pvc for pvc in pvcs if pvc["status"] == "Bound"]),
        "pending": len([pvc for pvc in pvcs if pvc["status"] == "Pending"]),
        "lost": len([pvc for pvc in pvcs if pvc["status"] == "Lost"])
    }
    result_data["status_summary"] = status_summary
    
    return {"status": "success", "data": result_data}


list_persistent_volume_claims_tool = StructuredTool.from_function(
    func=list_persistent_volume_claims,
    name="list_persistent_volume_claims",
    description="Lists all PersistentVolumeClaims in a namespace or across all namespaces with comprehensive status and storage information.",
    args_schema=NamespaceOptionalInputSchema
)


@_handle_k8s_exceptions
def describe_persistent_volume_claim(namespace: str, pvc_name: str) -> Dict[str, Any]:
    """Gets detailed information about a specific PersistentVolumeClaim."""
    core_v1 = get_core_v1_api()
    
    pvc = core_v1.read_namespaced_persistent_volume_claim(name=pvc_name, namespace=namespace)
    
    pvc_details = {
        "name": pvc.metadata.name,
        "namespace": pvc.metadata.namespace,
        "uid": pvc.metadata.uid,
        "resource_version": pvc.metadata.resource_version,
        "creation_timestamp": pvc.metadata.creation_timestamp.isoformat() if pvc.metadata.creation_timestamp else None,
        "age": _calculate_age(pvc.metadata.creation_timestamp),
        "labels": pvc.metadata.labels or {},
        "annotations": pvc.metadata.annotations or {},
        "finalizers": pvc.metadata.finalizers or [],
        "status": pvc.status.phase,
        "volume_name": pvc.spec.volume_name,
        "storage_class": pvc.spec.storage_class_name,
        "access_modes": pvc.spec.access_modes or [],
        "volume_mode": getattr(pvc.spec, 'volume_mode', 'Filesystem'),
        "requested_resources": pvc.spec.resources.requests if pvc.spec.resources and pvc.spec.resources.requests else {},
        "allocated_capacity": pvc.status.capacity if pvc.status.capacity else {},
        "conditions": [],
        "selector": None,
        "data_source": None
    }
    
    # Add selector information
    if pvc.spec.selector:
        pvc_details["selector"] = {
            "match_labels": pvc.spec.selector.match_labels if pvc.spec.selector.match_labels else {},
            "match_expressions": pvc.spec.selector.match_expressions if hasattr(pvc.spec.selector, 'match_expressions') else []
        }
    
    # Add data source information  
    if hasattr(pvc.spec, 'data_source') and pvc.spec.data_source:
        pvc_details["data_source"] = {
            "name": pvc.spec.data_source.name,
            "kind": pvc.spec.data_source.kind,
            "api_group": getattr(pvc.spec.data_source, 'api_group', None)
        }
    
    # Add conditions
    if hasattr(pvc.status, 'conditions') and pvc.status.conditions:
        for condition in pvc.status.conditions:
            pvc_details["conditions"].append({
                "type": condition.type,
                "status": condition.status,
                "reason": getattr(condition, 'reason', None),
                "message": getattr(condition, 'message', None),
                "last_probe_time": condition.last_probe_time.isoformat() if getattr(condition, 'last_probe_time', None) else None,
                "last_transition_time": condition.last_transition_time.isoformat() if getattr(condition, 'last_transition_time', None) else None
            })
    
    # If bound to a PV, get PV details
    if pvc_details["volume_name"] and pvc_details["status"] == "Bound":
        try:
            pv = core_v1.read_persistent_volume(name=pvc_details["volume_name"])
            pvc_details["bound_pv_details"] = {
                "name": pv.metadata.name,
                "capacity": pv.spec.capacity or {},
                "access_modes": pv.spec.access_modes or [],
                "storage_class": pv.spec.storage_class_name,
                "reclaim_policy": pv.spec.persistent_volume_reclaim_policy,
                "status": pv.status.phase,
                "volume_source_type": "HostPath" if pv.spec.host_path else 
                                     "NFS" if pv.spec.nfs else
                                     "CSI" if pv.spec.csi else
                                     "AWS EBS" if pv.spec.aws_elastic_block_store else
                                     "GCE PD" if pv.spec.gce_persistent_disk else
                                     "Other"
            }
        except Exception as e:
            pvc_details["bound_pv_details"] = f"Error retrieving PV details: {str(e)}"
    
    # Find pods using this PVC
    try:
        pods = core_v1.list_namespaced_pod(namespace=namespace, timeout_seconds=10)
        using_pods = []
        
        for pod in pods.items:
            pod_uses_pvc = False
            volume_mounts = []
            
            if pod.spec.volumes:
                for volume in pod.spec.volumes:
                    if volume.persistent_volume_claim and volume.persistent_volume_claim.claim_name == pvc_name:
                        pod_uses_pvc = True
                        
                        # Find which containers mount this volume
                        if pod.spec.containers:
                            for container in pod.spec.containers:
                                if container.volume_mounts:
                                    for mount in container.volume_mounts:
                                        if mount.name == volume.name:
                                            volume_mounts.append({
                                                "container": container.name,
                                                "mount_path": mount.mount_path,
                                                "read_only": getattr(mount, 'read_only', False),
                                                "sub_path": getattr(mount, 'sub_path', None)
                                            })
            
            if pod_uses_pvc:
                using_pods.append({
                    "name": pod.metadata.name,
                    "phase": pod.status.phase,
                    "node_name": pod.spec.node_name,
                    "volume_mounts": volume_mounts
                })
        
        pvc_details["used_by_pods"] = using_pods
        pvc_details["usage_count"] = len(using_pods)
    
    except Exception as e:
        pvc_details["used_by_pods"] = []
        pvc_details["usage_count"] = 0
        pvc_details["usage_check_error"] = str(e)
    
    return {"status": "success", "data": pvc_details}


describe_persistent_volume_claim_tool = StructuredTool.from_function(
    func=describe_persistent_volume_claim,
    name="describe_persistent_volume_claim",
    description="Gets comprehensive detailed information about a specific PersistentVolumeClaim including bound PV details and pod usage information.",
    args_schema=PVCInputSchema
)


# ===============================================================================
#                          STORAGE ANALYSIS TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def analyze_storage_usage(include_usage_details: bool = True, namespace: Optional[str] = None) -> Dict[str, Any]:
    """Analyzes storage usage patterns across the cluster or namespace."""
    core_v1 = get_core_v1_api()
    
    # Get PVs and PVCs
    pv_list = core_v1.list_persistent_volume(timeout_seconds=10)
    
    if namespace:
        pvc_list = core_v1.list_namespaced_persistent_volume_claim(namespace=namespace, timeout_seconds=10)
    else:
        pvc_list = core_v1.list_persistent_volume_claim_for_all_namespaces(timeout_seconds=10)
    
    # Convert to dictionaries for analysis
    pvs = []
    for pv in pv_list.items:
        pvs.append({
            "name": pv.metadata.name,
            "capacity": pv.spec.capacity or {},
            "access_modes": pv.spec.access_modes or [],
            "storage_class": pv.spec.storage_class_name,
            "status": pv.status.phase,
            "reclaim_policy": pv.spec.persistent_volume_reclaim_policy,
            "claim_ref": {
                "namespace": pv.spec.claim_ref.namespace if pv.spec.claim_ref else None,
                "name": pv.spec.claim_ref.name if pv.spec.claim_ref else None
            } if pv.spec.claim_ref else None
        })
    
    pvcs = []
    for pvc in pvc_list.items:
        pvcs.append({
            "name": pvc.metadata.name,
            "namespace": pvc.metadata.namespace,
            "status": pvc.status.phase,
            "volume_name": pvc.spec.volume_name,
            "storage_class": pvc.spec.storage_class_name,
            "requested_storage": pvc.spec.resources.requests.get("storage") if pvc.spec.resources and pvc.spec.resources.requests else None,
            "allocated_storage": pvc.status.capacity.get("storage") if pvc.status.capacity else None
        })
    
    # Perform analysis
    analysis = _analyze_storage_usage(pvs, pvcs)
    
    # Add namespace-specific analysis if not filtering by namespace
    if not namespace:
        namespace_analysis = {}
        
        for pvc in pvcs:
            ns = pvc["namespace"]
            if ns not in namespace_analysis:
                namespace_analysis[ns] = {
                    "pvc_count": 0,
                    "bound_pvcs": 0,
                    "pending_pvcs": 0,
                    "requested_storage_bytes": 0,
                    "allocated_storage_bytes": 0
                }
            
            namespace_analysis[ns]["pvc_count"] += 1
            
            if pvc["status"] == "Bound":
                namespace_analysis[ns]["bound_pvcs"] += 1
            elif pvc["status"] == "Pending":
                namespace_analysis[ns]["pending_pvcs"] += 1
            
            if pvc["requested_storage"]:
                namespace_analysis[ns]["requested_storage_bytes"] += _parse_storage_size(pvc["requested_storage"])
            
            if pvc["allocated_storage"]:
                namespace_analysis[ns]["allocated_storage_bytes"] += _parse_storage_size(pvc["allocated_storage"])
        
        # Format namespace storage sizes
        for ns_data in namespace_analysis.values():
            ns_data["requested_storage_formatted"] = _format_storage_size(ns_data["requested_storage_bytes"])
            ns_data["allocated_storage_formatted"] = _format_storage_size(ns_data["allocated_storage_bytes"])
        
        analysis["namespace_breakdown"] = namespace_analysis
    
    # Add detailed lists if requested
    if include_usage_details:
        analysis["detailed_pvs"] = pvs
        analysis["detailed_pvcs"] = pvcs
        
        # Orphaned PVs (Available but never bound)
        orphaned_pvs = [pv for pv in pvs if pv["status"] == "Available" and not pv["claim_ref"]]
        analysis["orphaned_pvs"] = orphaned_pvs
        analysis["orphaned_pv_count"] = len(orphaned_pvs)
        
        # Unbound PVCs
        unbound_pvcs = [pvc for pvc in pvcs if pvc["status"] == "Pending"]
        analysis["unbound_pvcs"] = unbound_pvcs
        analysis["unbound_pvc_count"] = len(unbound_pvcs)
    
    return {"status": "success", "data": analysis}


analyze_storage_usage_tool = StructuredTool.from_function(
    func=analyze_storage_usage,
    name="analyze_storage_usage",
    description="Provides comprehensive analysis of storage usage patterns including capacity utilization, storage classes, access modes, and namespace breakdowns.",
    args_schema=StorageAnalysisInputSchema
)


# ===============================================================================
#                     CREATE / DELETE PVC TOOLS
# ===============================================================================

class CreatePVCInputSchema(BaseModel):
    namespace: str = Field(description="Namespace in which to create the PVC.")
    name: str = Field(description="Name for the PersistentVolumeClaim.")
    storage: str = Field(
        description="Storage size, e.g. '1Gi', '500Mi', '10Gi'.",
    )
    access_modes: List[str] = Field(
        default=["ReadWriteOnce"],
        description="Access modes for the PVC (e.g. ReadWriteOnce, ReadOnlyMany, ReadWriteMany).",
    )
    storage_class_name: Optional[str] = Field(
        default=None,
        description="StorageClass to use. Omit to use the cluster default.",
    )


class DeletePVCInputSchema(BaseModel):
    namespace: str = Field(description="Namespace of the PVC.")
    name: str = Field(description="Name of the PersistentVolumeClaim to delete.")


@_handle_k8s_exceptions
def create_persistent_volume_claim(
    namespace: str,
    name: str,
    storage: str,
    access_modes: Optional[List[str]] = None,
    storage_class_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Creates a PersistentVolumeClaim in the specified namespace."""
    core_v1 = get_core_v1_api()
    if access_modes is None:
        access_modes = ["ReadWriteOnce"]

    pvc_body = client.V1PersistentVolumeClaim(
        api_version="v1",
        kind="PersistentVolumeClaim",
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=access_modes,
            resources=client.V1VolumeResourceRequirements(
                requests={"storage": storage}
            ),
            storage_class_name=storage_class_name,
        ),
    )

    try:
        result = core_v1.create_namespaced_persistent_volume_claim(
            namespace=namespace, body=pvc_body
        )
        action = "created"
    except client.exceptions.ApiException as e:
        if e.status == 409:
            return {
                "status": "success",
                "action": "already_exists",
                "message": f"PVC '{name}' already exists in namespace '{namespace}'.",
            }
        raise

    return {
        "status": "success",
        "action": action,
        "pvc": {
            "name": result.metadata.name,
            "namespace": result.metadata.namespace,
            "storage": storage,
            "access_modes": access_modes,
            "storage_class": storage_class_name,
            "phase": result.status.phase if result.status else "Pending",
        },
    }


@_handle_k8s_exceptions
def delete_persistent_volume_claim(namespace: str, name: str) -> Dict[str, Any]:
    """Deletes a PersistentVolumeClaim from a namespace."""
    core_v1 = get_core_v1_api()
    core_v1.delete_namespaced_persistent_volume_claim(name=name, namespace=namespace)
    return {
        "status": "success",
        "message": f"PVC '{name}' deleted from namespace '{namespace}'.",
    }


create_persistent_volume_claim_tool = StructuredTool.from_function(
    func=create_persistent_volume_claim,
    name="create_persistent_volume_claim",
    description=(
        "Create a PersistentVolumeClaim (PVC) in a namespace. "
        "Specify the storage size (e.g. '1Gi'), access modes (default: ReadWriteOnce), "
        "and optionally a StorageClass. Returns the created PVC details."
    ),
    args_schema=CreatePVCInputSchema,
)

delete_persistent_volume_claim_tool = StructuredTool.from_function(
    func=delete_persistent_volume_claim,
    name="delete_persistent_volume_claim",
    description="Delete a PersistentVolumeClaim from a namespace.",
    args_schema=DeletePVCInputSchema,
)


# ===============================================================================
#                           TOOL COLLECTION
# ===============================================================================

# Export all persistent volume tools for easy import
pv_tools = [
    connectivity_check_tool,
    list_persistent_volumes_tool,
    describe_persistent_volume_tool,
    list_persistent_volume_claims_tool,
    describe_persistent_volume_claim_tool,
    analyze_storage_usage_tool,
    create_persistent_volume_claim_tool,
    delete_persistent_volume_claim_tool,
]

