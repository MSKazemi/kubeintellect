from datetime import datetime
from typing import List, Dict, Any, Optional

from kubernetes import client
from kubernetes.client.exceptions import ApiException
from langchain_core.tools import StructuredTool
from opentelemetry import trace
from pydantic import BaseModel, Field

from app.agents.tools.tools_lib._base import (
    get_core_v1_api,
    get_apps_v1_api,
    get_networking_v1_api,
    _handle_k8s_exceptions,
    NoArgumentsInputSchema,
    NamespaceInputSchema,
    NamespaceOptionalInputSchema,
)
from app.utils.logger_config import setup_logging
from app.utils.metrics import tool_calls_total

_tracer = trace.get_tracer("kubeintellect.tools")

logger = setup_logging(app_name="kubeintellect")


# ===============================================================================
#                               INPUT SCHEMAS
# ===============================================================================

class ServiceInputSchema(BaseModel):
    """Schema for tools that require service name and namespace."""
    namespace: str = Field(description="The Kubernetes namespace where the service is located.")
    service_name: str = Field(description="The name of the Kubernetes service.")


class ServiceTypeInputSchema(BaseModel):
    """Schema for tools that filter by service type."""
    service_type: Optional[str] = Field(default=None, description="Filter services by type (ClusterIP, NodePort, LoadBalancer, ExternalName). If not provided, returns all types.")
    namespace: Optional[str] = Field(default=None, description="The Kubernetes namespace to query. If not provided, queries all namespaces.")


class CreateServiceInputSchema(BaseModel):
    """Schema for creating Kubernetes services."""
    namespace: str = Field(description="The Kubernetes namespace where the service will be created.")
    service_name: str = Field(description="The name of the service to be created. It will be converted to a valid RFC 1123 compliant name.")
    service_type: str = Field(default="ClusterIP", description="The type of service to create (ClusterIP, NodePort, LoadBalancer, ExternalName).")
    ports: List[Dict[str, Any]] = Field(description="List of port configurations. Each port should have 'port' (required), 'target_port' (optional), 'protocol' (optional, default TCP), and 'name' (optional).")
    selector: Dict[str, str] = Field(description="Selector labels to match pods that should receive traffic from this service.")
    labels: Optional[Dict[str, str]] = Field(default=None, description="Labels to assign to the service.")


class UpdateServiceInputSchema(BaseModel):
    """Schema for updating Kubernetes services."""
    namespace: str = Field(description="The Kubernetes namespace where the service is located.")
    service_name: str = Field(description="The name of the service to update.")
    service_type: str = Field(default="ClusterIP", description="The type of service (ClusterIP, NodePort, LoadBalancer, ExternalName).")
    ports: Optional[List[Dict[str, Any]]] = Field(default=None, description="List of port configurations. Each port should have 'port' (required), 'target_port' (optional), 'protocol' (optional, default TCP), and 'name' (optional). If omitted, existing ports are preserved.")
    selector: Dict[str, str] = Field(description="Selector labels to match pods that should receive traffic from this service.")
    labels: Optional[Dict[str, str]] = Field(default=None, description="Labels to assign to the service.")


class PatchServiceInputSchema(BaseModel):
    """Schema for patching Kubernetes services."""
    namespace: str = Field(description="The Kubernetes namespace where the service is located.")
    service_name: str = Field(description="The name of the service to patch.")
    patch_data: Dict[str, Any] = Field(description="The patch data to apply to the service. This should be a dictionary containing the fields to update.")


# ===============================================================================
#                               SERVICE TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def list_all_services() -> Dict[str, Any]:
    """Lists all Kubernetes services across all namespaces."""
    core_v1 = get_core_v1_api()
    services = core_v1.list_service_for_all_namespaces(timeout_seconds=10)
    
    service_list = []
    for svc in services.items:
        # Extract port information
        ports = []
        if svc.spec.ports:
            for port in svc.spec.ports:
                ports.append({
                    "name": port.name,
                    "port": port.port,
                    "target_port": str(port.target_port) if port.target_port else None,
                    "protocol": port.protocol,
                    "node_port": port.node_port
                })
        
        service_list.append({
            "name": svc.metadata.name,
            "namespace": svc.metadata.namespace,
            "type": svc.spec.type,
            "cluster_ip": svc.spec.cluster_ip,
            "external_ips": svc.spec.external_i_ps or [],
            "ports": ports,
            "labels": svc.metadata.labels or {},
            "creation_timestamp": svc.metadata.creation_timestamp.isoformat() if svc.metadata.creation_timestamp else None
        })
    
    return {"status": "success", "total_count": len(service_list), "data": service_list}


list_all_services_tool = StructuredTool.from_function(
    func=list_all_services,
    name="list_all_kubernetes_services",
    description="Lists all Kubernetes services across all namespaces with detailed information including ports, IPs, and labels.",
    args_schema=NoArgumentsInputSchema
)


@_handle_k8s_exceptions
def list_services_in_namespace(namespace: str) -> Dict[str, Any]:
    """Lists all Kubernetes services in a specific namespace."""
    core_v1 = get_core_v1_api()
    services = core_v1.list_namespaced_service(namespace=namespace, timeout_seconds=10)
    
    service_list = []
    for svc in services.items:
        # Extract port information
        ports = []
        if svc.spec.ports:
            for port in svc.spec.ports:
                ports.append({
                    "name": port.name,
                    "port": port.port,
                    "target_port": str(port.target_port) if port.target_port else None,
                    "protocol": port.protocol,
                    "node_port": port.node_port
                })
        
        service_list.append({
            "name": svc.metadata.name,
            "namespace": svc.metadata.namespace,
            "type": svc.spec.type,
            "cluster_ip": svc.spec.cluster_ip,
            "external_ips": svc.spec.external_i_ps or [],
            "ports": ports,
            "labels": svc.metadata.labels or {},
            "creation_timestamp": svc.metadata.creation_timestamp.isoformat() if svc.metadata.creation_timestamp else None
        })
    
    return {"status": "success", "total_count": len(service_list), "data": service_list}


list_services_in_namespace_tool = StructuredTool.from_function(
    func=list_services_in_namespace,
    name="list_services_in_namespace",
    description="Lists all Kubernetes services in a specific namespace with detailed port and IP information.",
    args_schema=NamespaceInputSchema
)


@_handle_k8s_exceptions
def describe_service(namespace: str, service_name: str) -> Dict[str, Any]:
    """Retrieves detailed information about a specific Kubernetes service."""
    core_v1 = get_core_v1_api()
    service = core_v1.read_namespaced_service(name=service_name, namespace=namespace)
    
    # Extract port information with details
    ports = []
    if service.spec.ports:
        for port in service.spec.ports:
            ports.append({
                "name": port.name,
                "port": port.port,
                "target_port": str(port.target_port) if port.target_port else None,
                "protocol": port.protocol,
                "node_port": port.node_port
            })
    
    # Get endpoints information
    try:
        endpoints = core_v1.read_namespaced_endpoints(name=service_name, namespace=namespace)
        endpoint_info = []
        
        if endpoints.subsets:
            for subset in endpoints.subsets:
                addresses = []
                if subset.addresses:
                    for addr in subset.addresses:
                        addresses.append({
                            "ip": addr.ip,
                            "target_ref": {
                                "kind": addr.target_ref.kind,
                                "name": addr.target_ref.name
                            } if addr.target_ref else None
                        })
                
                endpoint_ports = []
                if subset.ports:
                    for port in subset.ports:
                        endpoint_ports.append({
                            "name": port.name,
                            "port": port.port,
                            "protocol": port.protocol
                        })
                
                endpoint_info.append({
                    "addresses": addresses,
                    "ports": endpoint_ports
                })
    except ApiException:
        endpoint_info = []
    
    service_description = {
        "name": service.metadata.name,
        "namespace": service.metadata.namespace,
        "labels": service.metadata.labels or {},
        "annotations": service.metadata.annotations or {},
        "type": service.spec.type,
        "cluster_ip": service.spec.cluster_ip,
        "external_ips": service.spec.external_i_ps or [],
        "load_balancer_ip": service.spec.load_balancer_ip,
        "external_name": service.spec.external_name,
        "session_affinity": service.spec.session_affinity,
        "selector": service.spec.selector or {},
        "ports": ports,
        "endpoints": endpoint_info,
        "creation_timestamp": service.metadata.creation_timestamp.isoformat() if service.metadata.creation_timestamp else None
    }
    
    return {"status": "success", "data": service_description}


describe_service_tool = StructuredTool.from_function(
    func=describe_service,
    name="describe_kubernetes_service",
    description="Retrieves detailed information about a specific Kubernetes service including endpoints, ports, and selectors.",
    args_schema=ServiceInputSchema
)


@_handle_k8s_exceptions
def list_services_by_type(service_type: Optional[str] = None, namespace: Optional[str] = None) -> Dict[str, Any]:
    """Lists Kubernetes services filtered by type and optionally by namespace."""
    core_v1 = get_core_v1_api()
    
    if namespace:
        services = core_v1.list_namespaced_service(namespace=namespace, timeout_seconds=10)
    else:
        services = core_v1.list_service_for_all_namespaces(timeout_seconds=10)
    
    service_list = []
    for svc in services.items:
        # Filter by service type if specified
        if service_type and svc.spec.type != service_type:
            continue
            
        # Extract basic port information
        port_summary = []
        if svc.spec.ports:
            for port in svc.spec.ports:
                port_info = f"{port.port}"
                if port.target_port:
                    port_info += f":{port.target_port}"
                if port.protocol and port.protocol != "TCP":
                    port_info += f"/{port.protocol}"
                port_summary.append(port_info)
        
        service_list.append({
            "name": svc.metadata.name,
            "namespace": svc.metadata.namespace,
            "type": svc.spec.type,
            "cluster_ip": svc.spec.cluster_ip,
            "external_ips": svc.spec.external_i_ps or [],
            "ports": port_summary,
            "selector": svc.spec.selector or {},
            "age": (datetime.utcnow() - svc.metadata.creation_timestamp.replace(tzinfo=None)).days if svc.metadata.creation_timestamp else "Unknown"
        })
    
    return {"status": "success", "total_count": len(service_list), "data": service_list}


list_services_by_type_tool = StructuredTool.from_function(
    func=list_services_by_type,
    name="list_services_by_type",
    description="Lists Kubernetes services filtered by type (ClusterIP, NodePort, LoadBalancer, ExternalName) and optionally by namespace.",
    args_schema=ServiceTypeInputSchema
)


@_handle_k8s_exceptions
def list_external_services(namespace: Optional[str] = None) -> Dict[str, Any]:
    """Lists services that have external access (NodePort, LoadBalancer, or ExternalName)."""
    core_v1 = get_core_v1_api()
    
    if namespace:
        services = core_v1.list_namespaced_service(namespace=namespace, timeout_seconds=10)
    else:
        services = core_v1.list_service_for_all_namespaces(timeout_seconds=10)
    
    external_services = []
    external_types = ["NodePort", "LoadBalancer", "ExternalName"]
    
    for svc in services.items:
        if svc.spec.type in external_types or svc.spec.external_i_ps:
            # Extract external access information
            external_access = {
                "type": svc.spec.type,
                "external_ips": svc.spec.external_i_ps or [],
                "load_balancer_ip": svc.spec.load_balancer_ip,
                "external_name": svc.spec.external_name
            }
            
            # For NodePort and LoadBalancer, include external ports
            external_ports = []
            if svc.spec.ports and svc.spec.type in ["NodePort", "LoadBalancer"]:
                for port in svc.spec.ports:
                    if port.node_port:
                        external_ports.append({
                            "port": port.port,
                            "node_port": port.node_port,
                            "protocol": port.protocol
                        })
            
            external_services.append({
                "name": svc.metadata.name,
                "namespace": svc.metadata.namespace,
                "external_access": external_access,
                "external_ports": external_ports,
                "selector": svc.spec.selector or {},
                "labels": svc.metadata.labels or {}
            })
    
    return {"status": "success", "total_count": len(external_services), "data": external_services}


list_external_services_tool = StructuredTool.from_function(
    func=list_external_services,
    name="list_external_services",
    description="Lists Kubernetes services that have external access (NodePort, LoadBalancer, ExternalName) with their external access details.",
    args_schema=NamespaceOptionalInputSchema
)


@_handle_k8s_exceptions
def check_service_endpoints(namespace: str, service_name: str) -> Dict[str, Any]:
    """Checks the endpoints of a specific service to verify backend connectivity."""
    core_v1 = get_core_v1_api()
    
    try:
        # Get service information
        service = core_v1.read_namespaced_service(name=service_name, namespace=namespace)
        
        # Get endpoints
        endpoints = core_v1.read_namespaced_endpoints(name=service_name, namespace=namespace)
        
        endpoint_status = {
            "service_name": service_name,
            "namespace": namespace,
            "service_type": service.spec.type,
            "selector": service.spec.selector or {},
            "has_endpoints": False,
            "ready_endpoints": [],
            "not_ready_endpoints": [],
            "total_endpoints": 0
        }
        
        if endpoints.subsets:
            for subset in endpoints.subsets:
                # Ready addresses
                if subset.addresses:
                    for addr in subset.addresses:
                        endpoint_status["ready_endpoints"].append({
                            "ip": addr.ip,
                            "target": {
                                "kind": addr.target_ref.kind,
                                "name": addr.target_ref.name
                            } if addr.target_ref else None
                        })
                
                # Not ready addresses
                if subset.not_ready_addresses:
                    for addr in subset.not_ready_addresses:
                        endpoint_status["not_ready_endpoints"].append({
                            "ip": addr.ip,
                            "target": {
                                "kind": addr.target_ref.kind,
                                "name": addr.target_ref.name
                            } if addr.target_ref else None
                        })
        
        endpoint_status["total_endpoints"] = len(endpoint_status["ready_endpoints"]) + len(endpoint_status["not_ready_endpoints"])
        endpoint_status["has_endpoints"] = endpoint_status["total_endpoints"] > 0

        # When 0 endpoints and the service has a selector, list pods in the namespace
        # and compare pod labels against the selector to surface selector mismatches.
        if not endpoint_status["has_endpoints"] and endpoint_status["selector"]:
            try:
                pods = core_v1.list_namespaced_pod(namespace=namespace, timeout_seconds=10)
                selector = endpoint_status["selector"]
                matching_pods = []
                near_miss_pods = []  # pods with same label key but different value
                for pod in pods.items:
                    pod_labels = pod.metadata.labels or {}
                    if all(pod_labels.get(k) == v for k, v in selector.items()):
                        matching_pods.append(pod.metadata.name)
                    else:
                        # Check if any selector key exists in pod labels with a different value
                        mismatched_keys = {
                            k: {"expected": v, "actual": pod_labels.get(k, "<missing>")}
                            for k, v in selector.items()
                            if k in pod_labels and pod_labels[k] != v
                        }
                        if mismatched_keys:
                            near_miss_pods.append({
                                "pod": pod.metadata.name,
                                "pod_labels": pod_labels,
                                "mismatched_keys": mismatched_keys,
                            })
                endpoint_status["selector_diagnosis"] = {
                    "selector": selector,
                    "matching_pods": matching_pods,
                    "near_miss_pods": near_miss_pods,
                    "diagnosis": (
                        "SELECTOR MISMATCH: service selector does not match any pod labels. "
                        + (f"Near misses found: {near_miss_pods}" if near_miss_pods else
                           "No pods with any matching selector label key found in namespace.")
                    ) if not matching_pods else "Selector matches pods but endpoints not ready",
                }
            except Exception:
                pass  # best-effort; don't fail the whole endpoint check

        return {"status": "success", "data": endpoint_status}
        
    except ApiException as e:
        if e.status == 404:
            return {
                "status": "error",
                "message": f"Service '{service_name}' not found in namespace '{namespace}'",
                "error_type": "NotFound"
            }
        raise


check_service_endpoints_tool = StructuredTool.from_function(
    func=check_service_endpoints,
    name="check_service_endpoints",
    description="Checks the endpoints of a Kubernetes service to verify backend pod connectivity and readiness.",
    args_schema=ServiceInputSchema
)


@_handle_k8s_exceptions
def create_service(namespace: str, service_name: str, service_type: str = "ClusterIP", 
                  ports: List[Dict[str, Any]] = None, selector: Dict[str, str] = None, 
                  labels: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Creates a Kubernetes service in a specified namespace."""
    # Ensure service name is RFC 1123 compliant
    safe_name = service_name.replace("_", "-").lower()
    
    if not ports:
        return {
            "status": "error",
            "message": "At least one port must be specified for the service",
            "error_type": "ValidationError"
        }
    
    if not selector:
        return {
            "status": "error",
            "message": "Selector must be specified to match target pods",
            "error_type": "ValidationError"
        }
    
    # Build port specifications
    service_ports = []
    for port_config in ports:
        if 'port' not in port_config:
            return {
                "status": "error",
                "message": "Each port configuration must include a 'port' field",
                "error_type": "ValidationError"
            }
        
        service_port = client.V1ServicePort(
            port=port_config['port'],
            target_port=port_config.get('target_port', port_config['port']),
            protocol=port_config.get('protocol', 'TCP'),
            name=port_config.get('name')
        )
        service_ports.append(service_port)
    
    # Create service spec
    service_spec = client.V1ServiceSpec(
        selector=selector,
        ports=service_ports,
        type=service_type
    )
    
    # Create service metadata
    metadata = client.V1ObjectMeta(
        name=safe_name,
        namespace=namespace,
        labels=labels or {}
    )
    
    # Create the service object
    service = client.V1Service(
        api_version="v1",
        kind="Service",
        metadata=metadata,
        spec=service_spec
    )
    
    # Create the service
    core_v1 = get_core_v1_api()
    response = core_v1.create_namespaced_service(namespace=namespace, body=service)
    
    return {
        "status": "success", 
        "data": {
            "name": response.metadata.name, 
            "namespace": response.metadata.namespace,
            "type": response.spec.type
        }
    }


create_service_tool = StructuredTool.from_function(
    func=create_service,
    name="create_kubernetes_service",
    description="Creates a Kubernetes service in a specified namespace with the given configuration.",
    args_schema=CreateServiceInputSchema
)


@_handle_k8s_exceptions
def delete_service(namespace: str, service_name: str) -> Dict[str, Any]:
    """Deletes a Kubernetes service from a specified namespace."""
    core_v1 = get_core_v1_api()
    
    try:
        # Check if service exists first
        core_v1.read_namespaced_service(name=service_name, namespace=namespace)
        
        # Delete the service
        core_v1.delete_namespaced_service(name=service_name, namespace=namespace)
        
        return {
            "status": "success",
            "message": f"Service '{service_name}' deleted from namespace '{namespace}'"
        }
    except ApiException as e:
        if e.status == 404:
            return {
                "status": "success",
                "message": f"Service '{service_name}' does not exist in namespace '{namespace}'"
            }
        else:
            raise


delete_service_tool = StructuredTool.from_function(
    func=delete_service,
    name="delete_kubernetes_service",
    description="Deletes a Kubernetes service from a specified namespace.",
    args_schema=ServiceInputSchema
)


@_handle_k8s_exceptions
def update_service(namespace: str, service_name: str, service_type: str = "ClusterIP",
                  ports: List[Dict[str, Any]] = None, selector: Dict[str, str] = None,
                  labels: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Updates a Kubernetes service by replacing it entirely."""
    core_v1 = get_core_v1_api()
    
    if not selector:
        return {
            "status": "error",
            "message": "Selector must be specified to match target pods",
            "error_type": "ValidationError"
        }

    try:
        # Get existing service to preserve certain fields (especially clusterIP and ports)
        existing_service = core_v1.read_namespaced_service(name=service_name, namespace=namespace)

        # Update selector
        existing_service.spec.selector = selector

        # Update ports only if explicitly provided; otherwise keep existing ports intact
        if ports:
            service_ports = []
            for port_config in ports:
                if 'port' not in port_config:
                    return {
                        "status": "error",
                        "message": "Each port configuration must include a 'port' field",
                        "error_type": "ValidationError"
                    }
                service_port = client.V1ServicePort(
                    port=port_config['port'],
                    target_port=port_config.get('target_port', port_config['port']),
                    protocol=port_config.get('protocol', 'TCP'),
                    name=port_config.get('name')
                )
                service_ports.append(service_port)
            existing_service.spec.ports = service_ports

        existing_service.spec.type = service_type
        
        # Update labels if provided
        if labels:
            existing_service.metadata.labels = labels
        
        # Update the service
        response = core_v1.replace_namespaced_service(
            name=service_name,
            namespace=namespace,
            body=existing_service
        )
        
        return {
            "status": "success",
            "data": {
                "name": response.metadata.name,
                "namespace": response.metadata.namespace,
                "type": response.spec.type
            }
        }
    except ApiException as e:
        if e.status == 404:
            return {
                "status": "error",
                "message": f"Service '{service_name}' not found in namespace '{namespace}'",
                "error_type": "NotFound"
            }
        raise


update_service_tool = StructuredTool.from_function(
    func=update_service,
    name="update_kubernetes_service",
    description="Updates a Kubernetes service by replacing it entirely with new configuration.",
    args_schema=UpdateServiceInputSchema
)


@_handle_k8s_exceptions
def patch_service(namespace: str, service_name: str, patch_data: Dict[str, Any]) -> Dict[str, Any]:
    """Patches a Kubernetes service with partial updates."""
    core_v1 = get_core_v1_api()
    
    try:
        # Patch the service
        response = core_v1.patch_namespaced_service(
            name=service_name,
            namespace=namespace,
            body=patch_data
        )
        
        return {
            "status": "success",
            "data": {
                "name": response.metadata.name,
                "namespace": response.metadata.namespace,
                "type": response.spec.type
            }
        }
    except ApiException as e:
        if e.status == 404:
            return {
                "status": "error",
                "message": f"Service '{service_name}' not found in namespace '{namespace}'",
                "error_type": "NotFound"
            }
        raise


patch_service_tool = StructuredTool.from_function(
    func=patch_service,
    name="patch_kubernetes_service",
    description="Patches a Kubernetes service with partial updates using provided patch data.",
    args_schema=PatchServiceInputSchema
)


@_handle_k8s_exceptions
def get_service(namespace: str, service_name: str) -> Dict[str, Any]:
    """Gets basic information about a specific Kubernetes service."""
    core_v1 = get_core_v1_api()
    
    try:
        service = core_v1.read_namespaced_service(name=service_name, namespace=namespace)
        
        # Extract port information
        ports = []
        if service.spec.ports:
            for port in service.spec.ports:
                ports.append({
                    "name": port.name,
                    "port": port.port,
                    "target_port": str(port.target_port) if port.target_port else None,
                    "protocol": port.protocol,
                    "node_port": port.node_port
                })
        
        service_info = {
            "name": service.metadata.name,
            "namespace": service.metadata.namespace,
            "type": service.spec.type,
            "cluster_ip": service.spec.cluster_ip,
            "external_ips": service.spec.external_i_ps or [],
            "ports": ports,
            "selector": service.spec.selector or {},
            "labels": service.metadata.labels or {},
            "creation_timestamp": service.metadata.creation_timestamp.isoformat() if service.metadata.creation_timestamp else None
        }
        
        return {"status": "success", "data": service_info}
    except ApiException as e:
        if e.status == 404:
            return {
                "status": "error",
                "message": f"Service '{service_name}' not found in namespace '{namespace}'",
                "error_type": "NotFound"
            }
        raise


get_service_tool = StructuredTool.from_function(
    func=get_service,
    name="get_kubernetes_service",
    description="Gets basic information about a specific Kubernetes service.",
    args_schema=ServiceInputSchema
)


@_handle_k8s_exceptions
def get_service_endpoints(namespace: str, service_name: str) -> Dict[str, Any]:
    """Gets the endpoints of a specific Kubernetes service."""
    core_v1 = get_core_v1_api()
    
    try:
        endpoints = core_v1.read_namespaced_endpoints(name=service_name, namespace=namespace)
        
        endpoint_info = []
        if endpoints.subsets:
            for subset in endpoints.subsets:
                addresses = []
                if subset.addresses:
                    for addr in subset.addresses:
                        addresses.append({
                            "ip": addr.ip,
                            "target_ref": {
                                "kind": addr.target_ref.kind,
                                "name": addr.target_ref.name
                            } if addr.target_ref else None
                        })
                
                not_ready_addresses = []
                if subset.not_ready_addresses:
                    for addr in subset.not_ready_addresses:
                        not_ready_addresses.append({
                            "ip": addr.ip,
                            "target_ref": {
                                "kind": addr.target_ref.kind,
                                "name": addr.target_ref.name
                            } if addr.target_ref else None
                        })
                
                endpoint_ports = []
                if subset.ports:
                    for port in subset.ports:
                        endpoint_ports.append({
                            "name": port.name,
                            "port": port.port,
                            "protocol": port.protocol
                        })
                
                endpoint_info.append({
                    "addresses": addresses,
                    "not_ready_addresses": not_ready_addresses,
                    "ports": endpoint_ports
                })
        
        result = {
            "service_name": service_name,
            "namespace": namespace,
            "subsets": endpoint_info
        }
        
        return {"status": "success", "data": result}
    except ApiException as e:
        if e.status == 404:
            return {
                "status": "error",
                "message": f"Endpoints for service '{service_name}' not found in namespace '{namespace}'",
                "error_type": "NotFound"
            }
        raise


get_service_endpoints_tool = StructuredTool.from_function(
    func=get_service_endpoints,
    name="get_service_endpoints",
    description="Gets the endpoints of a Kubernetes service to see which pods are backing the service.",
    args_schema=ServiceInputSchema
)


@_handle_k8s_exceptions
def get_service_events(namespace: str, service_name: str) -> Dict[str, Any]:
    """Gets events related to a specific Kubernetes service."""
    core_v1 = get_core_v1_api()
    
    # Get all events in the namespace and filter for the service
    events = core_v1.list_namespaced_event(namespace=namespace, timeout_seconds=10)
    
    service_events = []
    for event in events.items:
        if (event.involved_object.name == service_name and 
            event.involved_object.kind == "Service"):
            service_events.append({
                "type": event.type,
                "reason": event.reason,
                "message": event.message,
                "first_timestamp": event.first_timestamp.isoformat() if event.first_timestamp else None,
                "last_timestamp": event.last_timestamp.isoformat() if event.last_timestamp else None,
                "count": event.count
            })
    
    return {"status": "success", "data": service_events}


get_service_events_tool = StructuredTool.from_function(
    func=get_service_events,
    name="get_service_events",
    description="Gets events related to a specific Kubernetes service for troubleshooting purposes.",
    args_schema=ServiceInputSchema
)


# ===============================================================================
#                           TOOL COLLECTION
# ===============================================================================

# Export all service tools for easy import
class PatchServiceSelectorInputSchema(BaseModel):
    """Schema for updating only the selector of a Kubernetes Service."""
    namespace: str = Field(description="The Kubernetes namespace where the service is located.")
    service_name: str = Field(description="The name of the service whose selector should be updated.")
    selector: Dict[str, str] = Field(description="The new selector labels as key-value pairs, e.g. app=web version=green. This completely replaces the existing selector.")


@_handle_k8s_exceptions
def patch_service_selector(namespace: str, service_name: str, selector: Dict[str, str]) -> Dict[str, Any]:
    """Atomically replaces the selector of an existing Kubernetes Service without touching ports or other fields.
    Use this for blue-green traffic switching (e.g. change version=blue to version=green) or
    fixing a service that was pointing to the wrong pods.
    """
    core_v1 = get_core_v1_api()
    try:
        response = core_v1.patch_namespaced_service(
            name=service_name,
            namespace=namespace,
            body={"spec": {"selector": selector}},
        )
        return {
            "status": "success",
            "data": {
                "name": response.metadata.name,
                "namespace": response.metadata.namespace,
                "new_selector": selector,
                "message": f"Service '{service_name}' selector updated to {selector}",
            },
        }
    except ApiException as e:
        if e.status == 404:
            return {
                "status": "error",
                "message": f"Service '{service_name}' not found in namespace '{namespace}'",
                "error_type": "NotFound",
            }
        raise


patch_service_selector_tool = StructuredTool.from_function(
    func=patch_service_selector,
    name="patch_service_selector",
    description=(
        "Updates ONLY the selector of an existing Kubernetes Service, leaving ports and all other "
        "settings unchanged. Use this to switch traffic between deployments (e.g. blue-green cutover: "
        "change selector from {'version': 'blue'} to {'version': 'green'}), or to fix a service whose "
        "selector no longer matches any pods. Accepts namespace, service_name, and a selector dict."
    ),
    args_schema=PatchServiceSelectorInputSchema,
)


class CreateSimpleServiceInputSchema(BaseModel):
    namespace: str = Field(description="Kubernetes namespace where the Service will be created.")
    service_name: str = Field(description="Name of the Service to create.")
    selector: Dict[str, str] = Field(description="Pod selector labels. Example: {'app': 'nginx'}.")
    port: int = Field(description="The port number the Service exposes, e.g. 80, 5432, 6379.")
    target_port: Optional[int] = Field(default=None, description="The port on the Pod the traffic goes to. Defaults to the same as port.")
    service_type: str = Field(default="ClusterIP", description="Service type: ClusterIP, NodePort, or LoadBalancer. Defaults to ClusterIP.")


@_handle_k8s_exceptions
def create_simple_service(
    namespace: str,
    service_name: str,
    selector: Dict[str, str],
    port: int,
    target_port: Optional[int] = None,
    service_type: str = "ClusterIP",
) -> Dict[str, Any]:
    """Creates a Kubernetes Service with a single port. Simpler alternative to create_kubernetes_service."""
    core_v1 = get_core_v1_api()
    safe_name = service_name.replace("_", "-").lower()
    svc = client.V1Service(
        api_version="v1",
        kind="Service",
        metadata=client.V1ObjectMeta(name=safe_name, namespace=namespace),
        spec=client.V1ServiceSpec(
            selector=selector,
            type=service_type,
            ports=[client.V1ServicePort(
                port=port,
                target_port=target_port or port,
                protocol="TCP",
            )],
        ),
    )
    response = core_v1.create_namespaced_service(namespace=namespace, body=svc)
    return {
        "status": "success",
        "data": {
            "name": response.metadata.name,
            "namespace": response.metadata.namespace,
            "type": response.spec.type,
            "port": port,
            "selector": selector,
        },
    }


create_simple_service_tool = StructuredTool.from_function(
    func=create_simple_service,
    name="create_simple_service",
    description=(
        "Create a Kubernetes Service with a single port. Simpler than create_kubernetes_service. "
        "Specify selector as a dict (e.g., {'app': 'nginx'}), port as an integer (e.g., 80). "
        "Defaults to ClusterIP. Use for standard single-port services like web servers or databases."
    ),
    args_schema=CreateSimpleServiceInputSchema,
)


class ServiceDependencyInputSchema(BaseModel):
    """Schema for the service dependency awareness tool."""
    namespace: str = Field(description="The Kubernetes namespace of the service to analyse.")
    service_name: str = Field(description="The name of the Kubernetes service to analyse.")


@_handle_k8s_exceptions
def get_service_dependencies(namespace: str, service_name: str) -> Dict[str, Any]:
    """
    Build a dependency map for a Kubernetes service showing backing pods,
    ingresses that route to it, sibling services sharing the same pods,
    and an estimated blast radius for the operation.
    """
    core_v1 = get_core_v1_api()
    networking_v1 = get_networking_v1_api()

    # 1. Fetch the service itself
    try:
        svc = core_v1.read_namespaced_service(name=service_name, namespace=namespace)
    except ApiException as e:
        if e.status == 404:
            return {
                "status": "error",
                "message": f"Service '{service_name}' not found in namespace '{namespace}'",
                "error_type": "NotFound",
            }
        raise

    selector = svc.spec.selector or {}
    svc_ports = []
    if svc.spec.ports:
        for p in svc.spec.ports:
            svc_ports.append({
                "name": p.name,
                "port": p.port,
                "target_port": str(p.target_port) if p.target_port else None,
                "protocol": p.protocol,
            })

    service_summary = {
        "name": svc.metadata.name,
        "namespace": svc.metadata.namespace,
        "type": svc.spec.type,
        "cluster_ip": svc.spec.cluster_ip,
        "selector": selector,
        "ports": svc_ports,
    }

    # 2. List backing pods that match the selector
    backing_pods: list[Dict[str, Any]] = []
    if selector:
        label_selector_str = ",".join(f"{k}={v}" for k, v in selector.items())
        try:
            pod_list = core_v1.list_namespaced_pod(
                namespace=namespace,
                label_selector=label_selector_str,
                timeout_seconds=10,
            )
            for pod in pod_list.items:
                phase = pod.status.phase if pod.status else "Unknown"
                ready = False
                if pod.status and pod.status.conditions:
                    ready = any(
                        c.type == "Ready" and c.status == "True"
                        for c in pod.status.conditions
                    )
                backing_pods.append({
                    "name": pod.metadata.name,
                    "phase": phase,
                    "ready": ready,
                    "node": pod.spec.node_name,
                })
        except ApiException:
            pass  # non-fatal; pods section will be empty

    # 3. Check for Ingresses pointing to this service
    ingresses: list[Dict[str, Any]] = []
    try:
        ingress_list = networking_v1.list_namespaced_ingress(
            namespace=namespace, timeout_seconds=10
        )
        for ing in ingress_list.items:
            if not ing.spec or not ing.spec.rules:
                continue
            for rule in ing.spec.rules:
                if not rule.http or not rule.http.paths:
                    continue
                for path in rule.http.paths:
                    backend_svc = None
                    if path.backend and path.backend.service:
                        backend_svc = path.backend.service.name
                    if backend_svc == service_name:
                        ingresses.append({
                            "ingress_name": ing.metadata.name,
                            "host": rule.host or "*",
                            "path": path.path or "/",
                        })
    except ApiException:
        pass  # non-fatal

    # 4. Check for sibling services that share the same pod selector
    sibling_services: list[str] = []
    if selector:
        try:
            all_svcs = core_v1.list_namespaced_service(namespace=namespace, timeout_seconds=10)
            for other_svc in all_svcs.items:
                if other_svc.metadata.name == service_name:
                    continue
                other_selector = other_svc.spec.selector or {}
                if other_selector and other_selector == selector:
                    sibling_services.append(other_svc.metadata.name)
        except ApiException:
            pass  # non-fatal

    # 5. Estimated blast radius
    if ingresses:
        blast_radius = "HIGH"
        blast_reason = "Service is exposed via Ingress — external traffic will be affected."
    elif len(backing_pods) > 1:
        blast_radius = "MEDIUM"
        blast_reason = f"Service has {len(backing_pods)} backing pods; disruption affects multiple instances."
    else:
        blast_radius = "LOW"
        blast_reason = "Service has 0 or 1 backing pods and no external ingress."

    return {
        "status": "success",
        "data": {
            "service": service_summary,
            "backing_pods": backing_pods,
            "ingresses": ingresses,
            "sibling_services_sharing_selector": sibling_services,
            "estimated_blast_radius": blast_radius,
            "blast_radius_reason": blast_reason,
        },
    }


get_service_dependencies_tool = StructuredTool.from_function(
    func=get_service_dependencies,
    name="get_service_dependencies",
    description=(
        "Builds a dependency map for a Kubernetes service: lists backing pods that match the "
        "service selector, Ingresses that route traffic to it, sibling services sharing the "
        "same pod selector, and an estimated blast radius (HIGH/MEDIUM/LOW). "
        "Use this before mutating or deleting a service to understand downstream impact."
    ),
    args_schema=ServiceDependencyInputSchema,
)


class CreateServiceForDeploymentInputSchema(BaseModel):
    namespace: str = Field(description="Kubernetes namespace of the deployment and service.")
    deployment_name: str = Field(description="Name of the existing deployment to expose.")
    service_name: Optional[str] = Field(default=None, description="Name for the new service. Defaults to '<deployment_name>-svc'.")
    port: int = Field(description="Port the service will expose (e.g. 80, 443, 8080).")
    target_port: Optional[int] = Field(default=None, description="Pod port traffic is forwarded to. Defaults to port.")
    service_type: str = Field(default="ClusterIP", description="Service type: ClusterIP, NodePort, or LoadBalancer.")


@_handle_k8s_exceptions
def create_service_for_deployment(
    namespace: str,
    deployment_name: str,
    port: int,
    service_name: Optional[str] = None,
    target_port: Optional[int] = None,
    service_type: str = "ClusterIP",
) -> Dict[str, Any]:
    """Creates a Kubernetes Service by automatically reading the pod selector from an existing Deployment."""
    with _tracer.start_as_current_span("create_service_for_deployment") as span:
        span.set_attribute("k8s.namespace", namespace)
        span.set_attribute("k8s.deployment", deployment_name)
        apps_v1 = get_apps_v1_api()
        core_v1 = get_core_v1_api()

        try:
            deployment = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
        except ApiException as e:
            if e.status == 404:
                tool_calls_total.labels(tool="create_service_for_deployment", status="error").inc()
                return {
                    "status": "error",
                    "message": (
                        f"Deployment '{deployment_name}' not found in namespace '{namespace}'. "
                        f"The deployment must be created BEFORE exposing it as a service. "
                        f"Do NOT retry this call — create the deployment first, then call this tool."
                    ),
                    "error_type": "NotFound",
                    "unrecoverable": True,
                }
            raise

        selector = deployment.spec.selector.match_labels if deployment.spec.selector else None
        if not selector:
            tool_calls_total.labels(tool="create_service_for_deployment", status="error").inc()
            return {
                "status": "error",
                "message": f"Deployment '{deployment_name}' has no selector labels — cannot create a service.",
                "error_type": "ValidationError",
            }

        safe_name = (service_name or f"{deployment_name}-svc").replace("_", "-").lower()
        svc = client.V1Service(
            api_version="v1",
            kind="Service",
            metadata=client.V1ObjectMeta(name=safe_name, namespace=namespace),
            spec=client.V1ServiceSpec(
                selector=selector,
                type=service_type,
                ports=[client.V1ServicePort(
                    port=port,
                    target_port=target_port or port,
                    protocol="TCP",
                )],
            ),
        )

        try:
            response = core_v1.create_namespaced_service(namespace=namespace, body=svc)
        except ApiException as e:
            if e.status == 409:
                existing = core_v1.read_namespaced_service(name=safe_name, namespace=namespace)
                tool_calls_total.labels(tool="create_service_for_deployment", status="success").inc()
                return {
                    "status": "success",
                    "data": {
                        "name": existing.metadata.name,
                        "namespace": existing.metadata.namespace,
                        "type": existing.spec.type,
                        "message": f"Service '{safe_name}' already exists — returning current state.",
                    },
                }
            raise

        node_port = None
        if response.spec.ports:
            node_port = response.spec.ports[0].node_port

        tool_calls_total.labels(tool="create_service_for_deployment", status="success").inc()
        return {
            "status": "success",
            "data": {
                "name": response.metadata.name,
                "namespace": response.metadata.namespace,
                "type": response.spec.type,
                "port": port,
                "node_port": node_port,
                "selector": selector,
                "message": (
                    f"Service '{safe_name}' ({service_type}) created for deployment '{deployment_name}' "
                    f"on port {port}" + (f", NodePort {node_port}" if node_port else "") + "."
                ),
            },
        }


create_service_for_deployment_tool = StructuredTool.from_function(
    func=create_service_for_deployment,
    name="create_service_for_deployment",
    description=(
        "Creates a Kubernetes Service for an existing Deployment by automatically discovering "
        "the pod selector — no need to look up or pass selector labels manually. "
        "PREFERRED tool when exposing a deployment as a Service. "
        "Provide: namespace, deployment_name, port, service_type (ClusterIP/NodePort/LoadBalancer). "
        "Optionally: service_name (defaults to '<deployment_name>-svc'), target_port."
    ),
    args_schema=CreateServiceForDeploymentInputSchema,
)


service_tools = [
    list_all_services_tool,
    list_services_in_namespace_tool,
    describe_service_tool,
    list_services_by_type_tool,
    list_external_services_tool,
    check_service_endpoints_tool,
    create_simple_service_tool,
    create_service_tool,
    delete_service_tool,
    update_service_tool,
    patch_service_tool,
    patch_service_selector_tool,
    get_service_tool,
    get_service_endpoints_tool,
    get_service_events_tool,
    get_service_dependencies_tool,
    create_service_for_deployment_tool,
]

