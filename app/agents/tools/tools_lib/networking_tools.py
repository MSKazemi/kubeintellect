import json
from datetime import datetime
from typing import List, Dict, Any, Optional

from kubernetes import client
from kubernetes.client.exceptions import ApiException
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.agents.tools.tools_lib._base import (
    get_core_v1_api,
    get_networking_v1_api,
    _handle_k8s_exceptions,
    NoArgumentsInputSchema,
    NamespaceInputSchema,
)
from app.services import kubernetes_service
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")


def _calculate_age(creation_timestamp):
    """Calculate resource age in days."""
    if not creation_timestamp:
        return "Unknown"

    if isinstance(creation_timestamp, str):
        creation_time = datetime.fromisoformat(creation_timestamp.replace('Z', '+00:00'))
    else:
        creation_time = creation_timestamp.replace(tzinfo=None)

    age_delta = datetime.utcnow() - creation_time.replace(tzinfo=None)
    return age_delta.days


def _extract_ingress_paths(rules):
    """Extract paths and backend services from ingress rules."""
    paths = []
    if not rules:
        return paths
    
    for rule in rules:
        if rule.http and rule.http.paths:
            for path in rule.http.paths:
                path_info = {
                    "path": path.path or "/",
                    "path_type": path.path_type or "Prefix"
                }
                
                # Extract backend service
                if path.backend:
                    if path.backend.service:
                        path_info["backend_service"] = path.backend.service.name
                        path_info["backend_port"] = (
                            path.backend.service.port.number if path.backend.service.port and path.backend.service.port.number
                            else path.backend.service.port.name if path.backend.service.port and path.backend.service.port.name
                            else "Unknown"
                        )
                    elif path.backend.resource:
                        path_info["backend_resource"] = {
                            "kind": path.backend.resource.kind,
                            "name": path.backend.resource.name
                        }
                
                paths.append(path_info)
    
    return paths


# ===============================================================================
#                               INPUT SCHEMAS
# ===============================================================================

class IngressInputSchema(BaseModel):
    """Schema for ingress-specific operations."""
    namespace: str = Field(description="The Kubernetes namespace where the ingress is located.")
    ingress_name: str = Field(description="The name of the ingress resource.")


class NetworkPolicyInputSchema(BaseModel):
    """Schema for network policy operations."""
    namespace: str = Field(description="The Kubernetes namespace to query for network policies.")


class ServiceTypeInputSchema(BaseModel):
    """Schema for filtering services by type."""
    namespace: Optional[str] = Field(default=None, description="The Kubernetes namespace to query. If not provided, queries all namespaces.")
    service_type: str = Field(description="The service type to filter (ClusterIP, NodePort, LoadBalancer, ExternalName).")


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
#                               INGRESS TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def list_ingress_in_namespace(namespace: str) -> Dict[str, Any]:
    """Lists all ingress resources in a specified namespace with detailed information."""
    networking_v1 = get_networking_v1_api()
    
    ingress_list = networking_v1.list_namespaced_ingress(namespace=namespace, timeout_seconds=10)
    
    ingresses = []
    for ingress in ingress_list.items:
        # Extract TLS information
        tls_config = []
        if ingress.spec.tls:
            for tls in ingress.spec.tls:
                tls_info = {
                    "hosts": tls.hosts or [],
                    "secret_name": tls.secret_name
                }
                tls_config.append(tls_info)
        
        # Extract rules and paths
        hosts = []
        paths = []
        if ingress.spec.rules:
            for rule in ingress.spec.rules:
                if rule.host:
                    hosts.append(rule.host)
            paths = _extract_ingress_paths(ingress.spec.rules)
        
        # Extract load balancer status
        load_balancer_status = {}
        if ingress.status and ingress.status.load_balancer and ingress.status.load_balancer.ingress:
            ingress_points = []
            for lb_ingress in ingress.status.load_balancer.ingress:
                ingress_point = {}
                if lb_ingress.ip:
                    ingress_point["ip"] = lb_ingress.ip
                if lb_ingress.hostname:
                    ingress_point["hostname"] = lb_ingress.hostname
                ingress_points.append(ingress_point)
            load_balancer_status["ingress"] = ingress_points
        
        ingress_info = {
            "name": ingress.metadata.name,
            "namespace": ingress.metadata.namespace,
            "hosts": hosts,
            "paths": paths,
            "tls_config": tls_config,
            "load_balancer_status": load_balancer_status,
            "creation_timestamp": ingress.metadata.creation_timestamp.isoformat() if ingress.metadata.creation_timestamp else None,
            "age_days": _calculate_age(ingress.metadata.creation_timestamp),
            "labels": ingress.metadata.labels or {},
            "annotations": ingress.metadata.annotations or {}
        }
        
        # Extract ingress class
        if ingress.spec.ingress_class_name:
            ingress_info["ingress_class"] = ingress.spec.ingress_class_name
        elif ingress.metadata.annotations and "kubernetes.io/ingress.class" in ingress.metadata.annotations:
            ingress_info["ingress_class"] = ingress.metadata.annotations["kubernetes.io/ingress.class"]
        else:
            ingress_info["ingress_class"] = None
        
        ingresses.append(ingress_info)
    
    return {
        "status": "success", 
        "data": {
            "namespace": namespace,
            "ingresses": ingresses,
            "ingress_count": len(ingresses)
        }
    }


list_ingress_in_namespace_tool = StructuredTool.from_function(
    func=list_ingress_in_namespace,
    name="list_ingress_in_namespace",
    description="Lists all Kubernetes ingress resources in a specified namespace with comprehensive details including hosts, paths, TLS configuration, and load balancer status.",
    args_schema=NamespaceInputSchema
)


@_handle_k8s_exceptions
def list_all_ingresses() -> Dict[str, Any]:
    """Lists all ingress resources across all namespaces."""
    networking_v1 = get_networking_v1_api()
    
    ingress_list = networking_v1.list_ingress_for_all_namespaces(timeout_seconds=10)
    
    ingresses_by_namespace = {}
    total_count = 0
    
    for ingress in ingress_list.items:
        namespace = ingress.metadata.namespace
        
        if namespace not in ingresses_by_namespace:
            ingresses_by_namespace[namespace] = []
        
        # Extract basic ingress information
        hosts = []
        if ingress.spec.rules:
            hosts = [rule.host for rule in ingress.spec.rules if rule.host]
        
        ingress_info = {
            "name": ingress.metadata.name,
            "namespace": namespace,
            "hosts": hosts,
            "ingress_class": (
                ingress.spec.ingress_class_name if ingress.spec.ingress_class_name
                else ingress.metadata.annotations.get("kubernetes.io/ingress.class") if ingress.metadata.annotations
                else None
            ),
            "tls_enabled": bool(ingress.spec.tls),
            "age_days": _calculate_age(ingress.metadata.creation_timestamp)
        }
        
        ingresses_by_namespace[namespace].append(ingress_info)
        total_count += 1
    
    # Sort namespaces by ingress count
    sorted_namespaces = sorted(
        ingresses_by_namespace.items(),
        key=lambda x: len(x[1]),
        reverse=True
    )
    
    return {
        "status": "success", 
        "data": {
            "ingresses_by_namespace": dict(sorted_namespaces),
            "total_ingress_count": total_count,
            "namespace_count": len(ingresses_by_namespace)
        }
    }


list_all_ingresses_tool = StructuredTool.from_function(
    func=list_all_ingresses,
    name="list_all_ingresses",
    description="Lists all Kubernetes ingress resources across all namespaces with namespace-wise grouping and summary statistics.",
    args_schema=NoArgumentsInputSchema
)


@_handle_k8s_exceptions
def describe_ingress(namespace: str, ingress_name: str) -> Dict[str, Any]:
    """Gets detailed information about a specific ingress resource."""
    networking_v1 = get_networking_v1_api()
    
    ingress = networking_v1.read_namespaced_ingress(name=ingress_name, namespace=namespace)
    
    # Extract comprehensive ingress details
    ingress_details = {
        "name": ingress.metadata.name,
        "namespace": ingress.metadata.namespace,
        "labels": ingress.metadata.labels or {},
        "annotations": ingress.metadata.annotations or {},
        "creation_timestamp": ingress.metadata.creation_timestamp.isoformat() if ingress.metadata.creation_timestamp else None,
        "age_days": _calculate_age(ingress.metadata.creation_timestamp),
        "ingress_class": ingress.spec.ingress_class_name,
        "rules": [],
        "tls_configuration": [],
        "default_backend": None
    }
    
    # Extract default backend
    if ingress.spec.default_backend:
        if ingress.spec.default_backend.service:
            ingress_details["default_backend"] = {
                "service_name": ingress.spec.default_backend.service.name,
                "service_port": (
                    ingress.spec.default_backend.service.port.number if ingress.spec.default_backend.service.port.number
                    else ingress.spec.default_backend.service.port.name
                )
            }
        elif ingress.spec.default_backend.resource:
            ingress_details["default_backend"] = {
                "resource_kind": ingress.spec.default_backend.resource.kind,
                "resource_name": ingress.spec.default_backend.resource.name
            }
    
    # Extract rules with detailed path information
    if ingress.spec.rules:
        for rule in ingress.spec.rules:
            rule_info = {
                "host": rule.host,
                "paths": []
            }
            
            if rule.http and rule.http.paths:
                for path in rule.http.paths:
                    path_info = {
                        "path": path.path or "/",
                        "path_type": path.path_type or "Prefix",
                        "backend": {}
                    }
                    
                    if path.backend.service:
                        path_info["backend"] = {
                            "type": "service",
                            "service_name": path.backend.service.name,
                            "service_port": (
                                path.backend.service.port.number if path.backend.service.port.number
                                else path.backend.service.port.name
                            )
                        }
                    elif path.backend.resource:
                        path_info["backend"] = {
                            "type": "resource",
                            "resource_kind": path.backend.resource.kind,
                            "resource_name": path.backend.resource.name
                        }
                    
                    rule_info["paths"].append(path_info)
            
            ingress_details["rules"].append(rule_info)
    
    # Extract TLS configuration
    if ingress.spec.tls:
        for tls in ingress.spec.tls:
            tls_info = {
                "hosts": tls.hosts or [],
                "secret_name": tls.secret_name
            }
            ingress_details["tls_configuration"].append(tls_info)
    
    # Extract load balancer status
    if ingress.status and ingress.status.load_balancer:
        ingress_details["load_balancer_status"] = {
            "ingress_points": []
        }
        
        if ingress.status.load_balancer.ingress:
            for lb_ingress in ingress.status.load_balancer.ingress:
                point = {}
                if lb_ingress.ip:
                    point["ip"] = lb_ingress.ip
                if lb_ingress.hostname:
                    point["hostname"] = lb_ingress.hostname
                ingress_details["load_balancer_status"]["ingress_points"].append(point)
    
    return {"status": "success", "data": ingress_details}


describe_ingress_tool = StructuredTool.from_function(
    func=describe_ingress,
    name="describe_ingress",
    description="Gets comprehensive detailed information about a specific Kubernetes ingress resource including rules, TLS configuration, and load balancer status.",
    args_schema=IngressInputSchema
)


# ===============================================================================
#                            LOAD BALANCER TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def check_loadbalancer_external_ips(namespace: str) -> Dict[str, Any]:
    """Checks LoadBalancer services in a namespace for external IP assignments."""
    core_v1 = get_core_v1_api()
    
    services = core_v1.list_namespaced_service(namespace=namespace, timeout_seconds=10)
    loadbalancer_services = []
    
    for svc in services.items:
        if svc.spec.type == "LoadBalancer":
            service_info = {
                "service_name": svc.metadata.name,
                "namespace": svc.metadata.namespace,
                "creation_timestamp": svc.metadata.creation_timestamp.isoformat() if svc.metadata.creation_timestamp else None,
                "age_days": _calculate_age(svc.metadata.creation_timestamp),
                "external_ips": [],
                "hostnames": [],
                "ports": [],
                "status": "Pending"
            }
            
            # Extract ports
            if svc.spec.ports:
                for port in svc.spec.ports:
                    port_info = {
                        "name": port.name,
                        "port": port.port,
                        "target_port": str(port.target_port) if port.target_port else None,
                        "protocol": port.protocol,
                        "node_port": port.node_port
                    }
                    service_info["ports"].append(port_info)
            
            # Extract external IPs and hostnames
            if svc.status and svc.status.load_balancer and svc.status.load_balancer.ingress:
                service_info["status"] = "Provisioned"
                for ingress in svc.status.load_balancer.ingress:
                    if ingress.ip:
                        service_info["external_ips"].append(ingress.ip)
                    if ingress.hostname:
                        service_info["hostnames"].append(ingress.hostname)
            
            # Add selector information
            if svc.spec.selector:
                service_info["selector"] = svc.spec.selector
            
            loadbalancer_services.append(service_info)
    
    # Calculate summary statistics
    provisioned_count = len([svc for svc in loadbalancer_services if svc["status"] == "Provisioned"])
    pending_count = len([svc for svc in loadbalancer_services if svc["status"] == "Pending"])
    
    return {
        "status": "success", 
        "data": {
            "namespace": namespace,
            "loadbalancer_services": loadbalancer_services,
            "summary": {
                "total_loadbalancer_services": len(loadbalancer_services),
                "provisioned": provisioned_count,
                "pending": pending_count
            }
        }
    }


check_loadbalancer_external_ips_tool = StructuredTool.from_function(
    func=check_loadbalancer_external_ips,
    name="check_loadbalancer_external_ips",
    description="Checks LoadBalancer services in a namespace for external IP assignments with detailed service information and provisioning status.",
    args_schema=NamespaceInputSchema
)


@_handle_k8s_exceptions
def list_services_by_type(service_type: str, namespace: Optional[str] = None) -> Dict[str, Any]:
    """Lists services filtered by type across namespaces."""
    core_v1 = get_core_v1_api()
    
    # Validate service type
    valid_types = ["ClusterIP", "NodePort", "LoadBalancer", "ExternalName"]
    if service_type not in valid_types:
        return {
            "status": "error",
            "message": f"Invalid service type. Must be one of: {', '.join(valid_types)}",
            "error_type": "ValidationError"
        }
    
    # Get services based on namespace scope
    if namespace:
        services = core_v1.list_namespaced_service(namespace=namespace, timeout_seconds=10).items
    else:
        services = core_v1.list_service_for_all_namespaces(timeout_seconds=10).items
    
    # Filter by service type
    filtered_services = []
    for svc in services:
        if svc.spec.type == service_type:
            service_info = {
                "name": svc.metadata.name,
                "namespace": svc.metadata.namespace,
                "type": svc.spec.type,
                "cluster_ip": svc.spec.cluster_ip,
                "ports": [],
                "age_days": _calculate_age(svc.metadata.creation_timestamp),
                "selector": svc.spec.selector or {}
            }
            
            # Add ports information
            if svc.spec.ports:
                for port in svc.spec.ports:
                    port_info = {
                        "name": port.name,
                        "port": port.port,
                        "target_port": str(port.target_port) if port.target_port else None,
                        "protocol": port.protocol
                    }
                    
                    # Add NodePort specific info
                    if service_type == "NodePort" and port.node_port:
                        port_info["node_port"] = port.node_port
                    
                    service_info["ports"].append(port_info)
            
            # Add LoadBalancer specific info
            if service_type == "LoadBalancer":
                service_info["external_ips"] = []
                service_info["hostnames"] = []
                if svc.status and svc.status.load_balancer and svc.status.load_balancer.ingress:
                    for ingress in svc.status.load_balancer.ingress:
                        if ingress.ip:
                            service_info["external_ips"].append(ingress.ip)
                        if ingress.hostname:
                            service_info["hostnames"].append(ingress.hostname)
            
            # Add ExternalName specific info
            if service_type == "ExternalName":
                service_info["external_name"] = svc.spec.external_name
            
            filtered_services.append(service_info)
    
    # Group by namespace if querying all namespaces
    if not namespace:
        services_by_namespace = {}
        for svc in filtered_services:
            ns = svc["namespace"]
            if ns not in services_by_namespace:
                services_by_namespace[ns] = []
            services_by_namespace[ns].append(svc)
        
        result_data = {
            "service_type": service_type,
            "services_by_namespace": services_by_namespace,
            "total_count": len(filtered_services),
            "namespace_count": len(services_by_namespace)
        }
    else:
        result_data = {
            "service_type": service_type,
            "namespace": namespace,
            "services": filtered_services,
            "count": len(filtered_services)
        }
    
    return {"status": "success", "data": result_data}


list_services_by_type_tool = StructuredTool.from_function(
    func=list_services_by_type,
    name="list_services_by_type",
    description="Lists Kubernetes services filtered by type (ClusterIP, NodePort, LoadBalancer, ExternalName) with detailed service information and optional namespace filtering.",
    args_schema=ServiceTypeInputSchema
)


# ===============================================================================
#                            NETWORK POLICY TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def list_network_policies(namespace: str) -> Dict[str, Any]:
    """Lists all network policies in a specified namespace."""
    networking_v1 = get_networking_v1_api()
    
    try:
        policies = networking_v1.list_namespaced_network_policy(namespace=namespace, timeout_seconds=10)
    except ApiException as e:
        if "not found" in str(e).lower() or e.status == 404:
            return {
                "status": "success",
                "data": {
                    "namespace": namespace,
                    "network_policies": [],
                    "policy_count": 0,
                    "note": "No network policies found or NetworkPolicy resource not available in this cluster"
                }
            }
        raise
    
    network_policies = []
    for policy in policies.items:
        policy_info = {
            "name": policy.metadata.name,
            "namespace": policy.metadata.namespace,
            "creation_timestamp": policy.metadata.creation_timestamp.isoformat() if policy.metadata.creation_timestamp else None,
            "age_days": _calculate_age(policy.metadata.creation_timestamp),
            "pod_selector": policy.spec.pod_selector.match_labels if policy.spec.pod_selector and policy.spec.pod_selector.match_labels else {},
            "policy_types": policy.spec.policy_types or [],
            "ingress_rules": len(policy.spec.ingress) if policy.spec.ingress else 0,
            "egress_rules": len(policy.spec.egress) if policy.spec.egress else 0
        }
        
        # Extract ingress rules details
        if policy.spec.ingress:
            policy_info["ingress_rules_details"] = []
            for rule in policy.spec.ingress:
                rule_info = {
                    "from_rules": [],
                    "ports": []
                }
                
                # kubernetes client v35+ uses _from (from is a reserved keyword)
                from_peers_list = getattr(rule, '_from', None) or getattr(rule, 'from_', None)
                if from_peers_list:
                    for from_rule in from_peers_list:
                        if from_rule.pod_selector:
                            rule_info["from_rules"].append({
                                "type": "pod_selector",
                                "match_labels": from_rule.pod_selector.match_labels or {}
                            })
                        if from_rule.namespace_selector:
                            rule_info["from_rules"].append({
                                "type": "namespace_selector",
                                "match_labels": from_rule.namespace_selector.match_labels or {}
                            })
                        if from_rule.ip_block:
                            rule_info["from_rules"].append({
                                "type": "ip_block",
                                "cidr": from_rule.ip_block.cidr,
                                "except": from_rule.ip_block.except_ or []
                            })
                
                if rule.ports:
                    for port in rule.ports:
                        port_info = {
                            "protocol": port.protocol,
                            "port": str(port.port) if port.port else None
                        }
                        rule_info["ports"].append(port_info)
                
                policy_info["ingress_rules_details"].append(rule_info)
        
        # Extract egress rules details
        if policy.spec.egress:
            policy_info["egress_rules_details"] = []
            for rule in policy.spec.egress:
                rule_info = {
                    "to_rules": [],
                    "ports": []
                }
                
                if rule.to:
                    for to_rule in rule.to:
                        if to_rule.pod_selector:
                            rule_info["to_rules"].append({
                                "type": "pod_selector",
                                "match_labels": to_rule.pod_selector.match_labels or {}
                            })
                        if to_rule.namespace_selector:
                            rule_info["to_rules"].append({
                                "type": "namespace_selector",
                                "match_labels": to_rule.namespace_selector.match_labels or {}
                            })
                        if to_rule.ip_block:
                            rule_info["to_rules"].append({
                                "type": "ip_block",
                                "cidr": to_rule.ip_block.cidr,
                                "except": to_rule.ip_block.except_ or []
                            })
                
                if rule.ports:
                    for port in rule.ports:
                        port_info = {
                            "protocol": port.protocol,
                            "port": str(port.port) if port.port else None
                        }
                        rule_info["ports"].append(port_info)
                
                policy_info["egress_rules_details"].append(rule_info)
        
        network_policies.append(policy_info)
    
    return {
        "status": "success", 
        "data": {
            "namespace": namespace,
            "network_policies": network_policies,
            "policy_count": len(network_policies)
        }
    }


list_network_policies_tool = StructuredTool.from_function(
    func=list_network_policies,
    name="list_network_policies",
    description="Lists all NetworkPolicy resources in a specified namespace with detailed ingress and egress rules.",
    args_schema=NamespaceInputSchema
)


# ===============================================================================
#                          NETWORK ANALYSIS TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def analyze_network_connectivity() -> Dict[str, Any]:
    """Analyzes network connectivity resources across the cluster."""
    core_v1 = get_core_v1_api()
    networking_v1 = get_networking_v1_api()
    
    # Get all services
    all_services = core_v1.list_service_for_all_namespaces(timeout_seconds=10).items
    
    # Get all ingresses
    try:
        all_ingresses = networking_v1.list_ingress_for_all_namespaces(timeout_seconds=10).items
    except ApiException:
        all_ingresses = []
    
    # Analyze services by type
    service_analysis = {
        "ClusterIP": 0,
        "NodePort": 0,
        "LoadBalancer": 0,
        "ExternalName": 0
    }
    
    external_access_points = {
        "nodeport_services": [],
        "loadbalancer_services": [],
        "ingress_resources": []
    }
    
    for svc in all_services:
        service_analysis[svc.spec.type] += 1
        
        if svc.spec.type == "NodePort":
            node_ports = []
            if svc.spec.ports:
                for port in svc.spec.ports:
                    if port.node_port:
                        node_ports.append(port.node_port)
            
            if node_ports:
                external_access_points["nodeport_services"].append({
                    "name": svc.metadata.name,
                    "namespace": svc.metadata.namespace,
                    "node_ports": node_ports
                })
        
        elif svc.spec.type == "LoadBalancer":
            lb_info = {
                "name": svc.metadata.name,
                "namespace": svc.metadata.namespace,
                "external_ips": [],
                "hostnames": []
            }
            
            if svc.status and svc.status.load_balancer and svc.status.load_balancer.ingress:
                for ingress in svc.status.load_balancer.ingress:
                    if ingress.ip:
                        lb_info["external_ips"].append(ingress.ip)
                    if ingress.hostname:
                        lb_info["hostnames"].append(ingress.hostname)
            
            external_access_points["loadbalancer_services"].append(lb_info)
    
    # Analyze ingresses
    ingress_analysis = {
        "total_ingresses": len(all_ingresses),
        "tls_enabled": 0,
        "unique_hosts": set()
    }
    
    for ingress in all_ingresses:
        if ingress.spec.tls:
            ingress_analysis["tls_enabled"] += 1
        
        if ingress.spec.rules:
            for rule in ingress.spec.rules:
                if rule.host:
                    ingress_analysis["unique_hosts"].add(rule.host)
        
        # Add to external access points
        hosts = []
        if ingress.spec.rules:
            hosts = [rule.host for rule in ingress.spec.rules if rule.host]
        
        external_access_points["ingress_resources"].append({
            "name": ingress.metadata.name,
            "namespace": ingress.metadata.namespace,
            "hosts": hosts,
            "tls_enabled": bool(ingress.spec.tls)
        })
    
    ingress_analysis["unique_hosts"] = list(ingress_analysis["unique_hosts"])
    
    return {
        "status": "success",
        "data": {
            "service_analysis": service_analysis,
            "ingress_analysis": ingress_analysis,
            "external_access_points": external_access_points,
            "summary": {
                "total_services": len(all_services),
                "total_ingresses": len(all_ingresses),
                "external_services": service_analysis["NodePort"] + service_analysis["LoadBalancer"],
                "unique_ingress_hosts": len(ingress_analysis["unique_hosts"])
            }
        }
    }


analyze_network_connectivity_tool = StructuredTool.from_function(
    func=analyze_network_connectivity,
    name="analyze_network_connectivity",
    description="Provides comprehensive analysis of network connectivity resources across the cluster including services, ingresses, and external access points.",
    args_schema=NoArgumentsInputSchema
)


# ===============================================================================
#                     CREATE / DELETE NETWORK POLICY TOOLS
# ===============================================================================

class CreateNetworkPolicyInput(BaseModel):
    namespace: str = Field(description="Namespace in which to create the NetworkPolicy.")
    name: str = Field(description="Name for the NetworkPolicy.")
    pod_selector_labels: Optional[Dict[str, str]] = Field(
        default=None,
        description="Label selector for pods this policy applies to. Empty dict {} means all pods.",
    )
    policy_types: List[str] = Field(
        default=["Ingress"],
        description="Policy types to enforce: 'Ingress', 'Egress', or both.",
    )
    allow_ingress_ports: Optional[List[int]] = Field(
        default=None,
        description=(
            "Specific TCP ports to allow inbound. If None and policy_types includes Ingress, "
            "all ingress is denied (deny-all). "
            "To allow all ingress unconditionally, pass an empty list []."
        ),
    )
    allow_from_labels: Optional[Dict[str, str]] = Field(
        default=None,
        description="Allow ingress only from pods matching these labels. Ignored if allow_ingress_ports is None.",
    )


class DeleteNetworkPolicyInput(BaseModel):
    namespace: str = Field(description="Namespace of the NetworkPolicy.")
    name: str = Field(description="Name of the NetworkPolicy to delete.")


@_handle_k8s_exceptions
def create_network_policy(
    namespace: str,
    name: str,
    pod_selector_labels: Optional[Dict[str, str]] = None,
    policy_types: Optional[List[str]] = None,
    allow_ingress_ports: Optional[List[int]] = None,
    allow_from_labels: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Creates or replaces a NetworkPolicy in a namespace."""
    networking_v1 = get_networking_v1_api()
    if policy_types is None:
        policy_types = ["Ingress"]

    # Build ingress rules
    ingress_rules = None
    if "Ingress" in policy_types:
        if allow_ingress_ports is not None:
            ports = [
                client.V1NetworkPolicyPort(protocol="TCP", port=p)
                for p in allow_ingress_ports
            ] if allow_ingress_ports else []

            from_peers = []
            if allow_from_labels:
                from_peers.append(
                    client.V1NetworkPolicyPeer(
                        pod_selector=client.V1LabelSelector(
                            match_labels=allow_from_labels
                        )
                    )
                )

            ingress_rules = [
                client.V1NetworkPolicyIngressRule(
                    ports=ports if ports else None,
                    _from=from_peers if from_peers else None,
                )
            ]
        else:
            # deny-all ingress: empty ingress list
            ingress_rules = []

    policy_body = client.V1NetworkPolicy(
        api_version="networking.k8s.io/v1",
        kind="NetworkPolicy",
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        spec=client.V1NetworkPolicySpec(
            pod_selector=client.V1LabelSelector(
                match_labels=pod_selector_labels if pod_selector_labels else {}
            ),
            policy_types=policy_types,
            ingress=ingress_rules,
        ),
    )

    try:
        result = networking_v1.create_namespaced_network_policy(
            namespace=namespace, body=policy_body
        )
        action = "created"
    except client.exceptions.ApiException as e:
        if e.status == 409:
            result = networking_v1.replace_namespaced_network_policy(
                name=name, namespace=namespace, body=policy_body
            )
            action = "replaced"
        else:
            raise

    return {
        "status": "success",
        "action": action,
        "network_policy": {
            "name": result.metadata.name,
            "namespace": result.metadata.namespace,
            "pod_selector": pod_selector_labels,
            "policy_types": policy_types,
        },
    }


@_handle_k8s_exceptions
def delete_network_policy(namespace: str, name: str) -> Dict[str, Any]:
    """Deletes a NetworkPolicy from a namespace."""
    networking_v1 = get_networking_v1_api()
    networking_v1.delete_namespaced_network_policy(name=name, namespace=namespace)
    return {
        "status": "success",
        "message": f"NetworkPolicy '{name}' deleted from namespace '{namespace}'.",
    }


create_network_policy_tool = StructuredTool.from_function(
    func=create_network_policy,
    name="create_network_policy",
    description=(
        "Create (or replace) a NetworkPolicy in a namespace. "
        "Set pod_selector_labels to {} to target all pods. "
        "Leave allow_ingress_ports as None to create a deny-all ingress policy. "
        "Pass specific ports to allow only those ports. "
        "Optionally restrict ingress to pods with allow_from_labels."
    ),
    args_schema=CreateNetworkPolicyInput,
)

delete_network_policy_tool = StructuredTool.from_function(
    func=delete_network_policy,
    name="delete_network_policy",
    description="Delete a NetworkPolicy from a namespace.",
    args_schema=DeleteNetworkPolicyInput,
)


# ===============================================================================
#                           TOOL COLLECTION
# ===============================================================================

# Export all networking tools for easy import
networking_tools = [
    connectivity_check_tool,
    list_ingress_in_namespace_tool,
    list_all_ingresses_tool,
    describe_ingress_tool,
    check_loadbalancer_external_ips_tool,
    list_services_by_type_tool,
    list_network_policies_tool,
    analyze_network_connectivity_tool,
    create_network_policy_tool,
    delete_network_policy_tool,
]


