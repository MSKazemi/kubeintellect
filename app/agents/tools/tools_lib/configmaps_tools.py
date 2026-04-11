import json
import base64
from typing import Dict, Any, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.agents.tools.tools_lib._base import (
    get_core_v1_api,
    _handle_k8s_exceptions,
    NoArgumentsInputSchema,
    NamespaceOptionalInputSchema,
    calculate_age as _calculate_age,
)
from app.services import kubernetes_service
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")


def _analyze_config_data(data: Dict[str, str], config_type: str = "ConfigMap") -> Dict[str, Any]:
    """Analyze configuration data and extract insights."""
    if not data:
        return {"total_keys": 0, "total_size_bytes": 0, "analysis": f"Empty {config_type}"}
    
    analysis = {
        "total_keys": len(data),
        "total_size_bytes": sum(len(str(v).encode('utf-8')) for v in data.values()),
        "key_analysis": {},
        "content_types": {},
        "largest_key": None,
        "largest_size": 0
    }
    
    for key, value in data.items():
        value_str = str(value) if value is not None else ""
        size = len(value_str.encode('utf-8'))
        
        if size > analysis["largest_size"]:
            analysis["largest_size"] = size
            analysis["largest_key"] = key
        
        # Analyze content type
        content_type = "text"
        if key.endswith(('.json',)) or value_str.strip().startswith(('{', '[')):
            try:
                json.loads(value_str)
                content_type = "json"
            except Exception:
                content_type = "text"
        elif key.endswith(('.yaml', '.yml')) or ('apiVersion:' in value_str and 'kind:' in value_str):
            content_type = "yaml"
        elif key.endswith(('.xml',)) or value_str.strip().startswith('<?xml'):
            content_type = "xml"
        elif key.endswith(('.properties', '.conf', '.cfg')):
            content_type = "properties"
        elif '\n' in value_str:
            content_type = "multiline_text"
        
        analysis["key_analysis"][key] = {
            "size_bytes": size,
            "content_type": content_type,
            "line_count": len(value_str.split('\n')) if value_str else 1
        }
        
        # Count content types
        analysis["content_types"][content_type] = analysis["content_types"].get(content_type, 0) + 1
    
    return analysis


def _decode_secret_data(data: Dict[str, str]) -> Dict[str, str]:
    """Decode base64 encoded secret data."""
    if not data:
        return {}
    
    decoded_data = {}
    for key, value in data.items():
        try:
            # Kubernetes secrets are base64 encoded
            decoded_value = base64.b64decode(value).decode('utf-8')
            decoded_data[key] = decoded_value
        except Exception:
            # If decoding fails, keep original value
            decoded_data[key] = value
    
    return decoded_data


# ===============================================================================
#                               INPUT SCHEMAS
# ===============================================================================

class ConfigMapInputSchema(BaseModel):
    """Schema for ConfigMap-specific operations."""
    namespace: str = Field(description="The Kubernetes namespace where the ConfigMap is located.")
    configmap_name: str = Field(description="The name of the ConfigMap.")


class SecretInputSchema(BaseModel):
    """Schema for Secret-specific operations."""
    namespace: str = Field(description="The Kubernetes namespace where the Secret is located.")
    secret_name: str = Field(description="The name of the Secret.")


class ConfigSearchInputSchema(BaseModel):
    """Schema for searching configuration data."""
    namespace: Optional[str] = Field(default=None, description="The Kubernetes namespace to search. If not provided, searches all namespaces.")
    search_key: Optional[str] = Field(default=None, description="Key to search for in configuration data.")
    search_value: Optional[str] = Field(default=None, description="Value to search for in configuration data.")
    config_type: Optional[str] = Field(default="both", description="Type of config to search: 'configmap', 'secret', or 'both'.")


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
#                              CONFIGMAP TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def list_configmaps(namespace: Optional[str] = None) -> Dict[str, Any]:
    """Lists all ConfigMaps in a namespace or across all namespaces."""
    core_v1 = get_core_v1_api()
    
    if namespace:
        configmap_list = core_v1.list_namespaced_config_map(namespace=namespace, timeout_seconds=10)
    else:
        configmap_list = core_v1.list_config_map_for_all_namespaces(timeout_seconds=10)
    
    configmaps = []
    for cm in configmap_list.items:
        # Analyze configuration data
        data_analysis = _analyze_config_data(cm.data or {}, "ConfigMap")
        
        configmap_info = {
            "name": cm.metadata.name,
            "namespace": cm.metadata.namespace,
            "creation_timestamp": cm.metadata.creation_timestamp.isoformat() if cm.metadata.creation_timestamp else None,
            "age": _calculate_age(cm.metadata.creation_timestamp),
            "labels": cm.metadata.labels or {},
            "annotations": cm.metadata.annotations or {},
            "data_keys": list((cm.data or {}).keys()),
            "binary_data_keys": list((cm.binary_data or {}).keys()),
            "data_analysis": data_analysis,
            "immutable": getattr(cm, 'immutable', False)
        }
        
        configmaps.append(configmap_info)
    
    # Group by namespace if querying all namespaces
    if not namespace:
        configmaps_by_namespace = {}
        for cm in configmaps:
            ns = cm["namespace"]
            if ns not in configmaps_by_namespace:
                configmaps_by_namespace[ns] = []
            configmaps_by_namespace[ns].append(cm)
        
        result_data = {
            "configmaps_by_namespace": configmaps_by_namespace,
            "total_configmap_count": len(configmaps),
            "namespace_count": len(configmaps_by_namespace)
        }
    else:
        result_data = {
            "namespace": namespace,
            "configmaps": configmaps,
            "configmap_count": len(configmaps)
        }
    
    return {"status": "success", "total_count": len(configmaps), "data": result_data}


list_configmaps_tool = StructuredTool.from_function(
    func=list_configmaps,
    name="list_configmaps",
    description="Lists all Kubernetes ConfigMaps in a namespace or across all namespaces with comprehensive configuration analysis including data types and sizes.",
    args_schema=NamespaceOptionalInputSchema
)


@_handle_k8s_exceptions
def describe_configmap(namespace: str, configmap_name: str) -> Dict[str, Any]:
    """Gets detailed information about a specific ConfigMap."""
    core_v1 = get_core_v1_api()
    
    configmap = core_v1.read_namespaced_config_map(name=configmap_name, namespace=namespace)
    
    # Analyze configuration data
    data_analysis = _analyze_config_data(configmap.data or {}, "ConfigMap")
    
    configmap_details = {
        "name": configmap.metadata.name,
        "namespace": configmap.metadata.namespace,
        "labels": configmap.metadata.labels or {},
        "annotations": configmap.metadata.annotations or {},
        "creation_timestamp": configmap.metadata.creation_timestamp.isoformat() if configmap.metadata.creation_timestamp else None,
        "age": _calculate_age(configmap.metadata.creation_timestamp),
        "uid": configmap.metadata.uid,
        "resource_version": configmap.metadata.resource_version,
        "immutable": getattr(configmap, 'immutable', False),
        "data": configmap.data or {},
        "binary_data": configmap.binary_data or {},
        "data_analysis": data_analysis
    }
    
    # Add usage information by finding pods that reference this ConfigMap
    try:
        pods = core_v1.list_namespaced_pod(namespace=namespace, timeout_seconds=10)
        using_pods = []
        
        for pod in pods.items:
            pod_uses_cm = False
            usage_details = []
            
            # Check volumes
            if pod.spec.volumes:
                for volume in pod.spec.volumes:
                    if volume.config_map and volume.config_map.name == configmap_name:
                        pod_uses_cm = True
                        usage_details.append(f"Volume: {volume.name}")
            
            # Check environment variables
            if pod.spec.containers:
                for container in pod.spec.containers:
                    if container.env:
                        for env in container.env:
                            if env.value_from and env.value_from.config_map_key_ref:
                                if env.value_from.config_map_key_ref.name == configmap_name:
                                    pod_uses_cm = True
                                    usage_details.append(f"Env var: {env.name} in container {container.name}")
                    
                    if container.env_from:
                        for env_from in container.env_from:
                            if env_from.config_map_ref and env_from.config_map_ref.name == configmap_name:
                                pod_uses_cm = True
                                usage_details.append(f"Env from ConfigMap in container {container.name}")
            
            if pod_uses_cm:
                using_pods.append({
                    "name": pod.metadata.name,
                    "phase": pod.status.phase,
                    "usage_details": usage_details
                })
        
        configmap_details["used_by_pods"] = using_pods
        configmap_details["usage_count"] = len(using_pods)
    
    except Exception as e:
        configmap_details["used_by_pods"] = []
        configmap_details["usage_count"] = 0
        configmap_details["usage_check_error"] = str(e)
    
    return {"status": "success", "data": configmap_details}


describe_configmap_tool = StructuredTool.from_function(
    func=describe_configmap,
    name="describe_configmap",
    description="Gets comprehensive detailed information about a specific Kubernetes ConfigMap including data analysis and pod usage information.",
    args_schema=ConfigMapInputSchema
)


# ===============================================================================
#                               SECRET TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def list_secrets(namespace: Optional[str] = None) -> Dict[str, Any]:
    """Lists all Secrets in a namespace or across all namespaces."""
    core_v1 = get_core_v1_api()
    
    if namespace:
        secret_list = core_v1.list_namespaced_secret(namespace=namespace, timeout_seconds=10)
    else:
        secret_list = core_v1.list_secret_for_all_namespaces(timeout_seconds=10)
    
    secrets = []
    for secret in secret_list.items:
        # Analyze secret data (without decoding for security)
        data_keys = list((secret.data or {}).keys()) if secret.data else []
        string_data_keys = list((secret.string_data or {}).keys()) if secret.string_data else []
        
        secret_info = {
            "name": secret.metadata.name,
            "namespace": secret.metadata.namespace,
            "type": secret.type,
            "creation_timestamp": secret.metadata.creation_timestamp.isoformat() if secret.metadata.creation_timestamp else None,
            "age": _calculate_age(secret.metadata.creation_timestamp),
            "labels": secret.metadata.labels or {},
            "annotations": secret.metadata.annotations or {},
            "data_keys": data_keys,
            "string_data_keys": string_data_keys,
            "total_keys": len(data_keys) + len(string_data_keys),
            "immutable": getattr(secret, 'immutable', False)
        }
        
        secrets.append(secret_info)
    
    # Group by namespace if querying all namespaces
    if not namespace:
        secrets_by_namespace = {}
        secret_types = {}
        
        for secret in secrets:
            ns = secret["namespace"]
            if ns not in secrets_by_namespace:
                secrets_by_namespace[ns] = []
            secrets_by_namespace[ns].append(secret)
            
            # Count secret types
            secret_type = secret["type"]
            secret_types[secret_type] = secret_types.get(secret_type, 0) + 1
        
        result_data = {
            "secrets_by_namespace": secrets_by_namespace,
            "total_secret_count": len(secrets),
            "namespace_count": len(secrets_by_namespace),
            "secret_types": secret_types
        }
    else:
        # Count secret types for single namespace
        secret_types = {}
        for secret in secrets:
            secret_type = secret["type"]
            secret_types[secret_type] = secret_types.get(secret_type, 0) + 1
        
        result_data = {
            "namespace": namespace,
            "secrets": secrets,
            "secret_count": len(secrets),
            "secret_types": secret_types
        }
    
    return {"status": "success", "data": result_data}


list_secrets_tool = StructuredTool.from_function(
    func=list_secrets,
    name="list_secrets",
    description="Lists all Kubernetes Secrets in a namespace or across all namespaces with secret type analysis and key information (data remains secure).",
    args_schema=NamespaceOptionalInputSchema
)


@_handle_k8s_exceptions
def describe_secret(namespace: str, secret_name: str, decode_data: bool = False) -> Dict[str, Any]:
    """Gets detailed information about a specific Secret."""
    core_v1 = get_core_v1_api()
    
    secret = core_v1.read_namespaced_secret(name=secret_name, namespace=namespace)
    
    secret_details = {
        "name": secret.metadata.name,
        "namespace": secret.metadata.namespace,
        "type": secret.type,
        "labels": secret.metadata.labels or {},
        "annotations": secret.metadata.annotations or {},
        "creation_timestamp": secret.metadata.creation_timestamp.isoformat() if secret.metadata.creation_timestamp else None,
        "age": _calculate_age(secret.metadata.creation_timestamp),
        "uid": secret.metadata.uid,
        "resource_version": secret.metadata.resource_version,
        "immutable": getattr(secret, 'immutable', False),
        "data_keys": list((secret.data or {}).keys()),
        "string_data_keys": list((secret.string_data or {}).keys()),
        "total_keys": len((secret.data or {}).keys()) + len((secret.string_data or {}).keys())
    }
    
    # Optionally decode secret data (use with caution)
    if decode_data and secret.data:
        try:
            decoded_data = _decode_secret_data(secret.data)
            secret_details["decoded_data"] = decoded_data
            secret_details["data_analysis"] = _analyze_config_data(decoded_data, "Secret")
        except Exception as e:
            secret_details["decode_error"] = str(e)
    else:
        secret_details["decoded_data"] = "Not decoded for security"
    
    # Add usage information by finding pods that reference this Secret
    try:
        pods = core_v1.list_namespaced_pod(namespace=namespace, timeout_seconds=10)
        using_pods = []
        
        for pod in pods.items:
            pod_uses_secret = False
            usage_details = []
            
            # Check volumes
            if pod.spec.volumes:
                for volume in pod.spec.volumes:
                    if volume.secret and volume.secret.secret_name == secret_name:
                        pod_uses_secret = True
                        usage_details.append(f"Volume: {volume.name}")
            
            # Check environment variables
            if pod.spec.containers:
                for container in pod.spec.containers:
                    if container.env:
                        for env in container.env:
                            if env.value_from and env.value_from.secret_key_ref:
                                if env.value_from.secret_key_ref.name == secret_name:
                                    pod_uses_secret = True
                                    usage_details.append(f"Env var: {env.name} in container {container.name}")
                    
                    if container.env_from:
                        for env_from in container.env_from:
                            if env_from.secret_ref and env_from.secret_ref.name == secret_name:
                                pod_uses_secret = True
                                usage_details.append(f"Env from Secret in container {container.name}")
            
            # Check image pull secrets
            if pod.spec.image_pull_secrets:
                for pull_secret in pod.spec.image_pull_secrets:
                    if pull_secret.name == secret_name:
                        pod_uses_secret = True
                        usage_details.append("Image pull secret")
            
            if pod_uses_secret:
                using_pods.append({
                    "name": pod.metadata.name,
                    "phase": pod.status.phase,
                    "usage_details": usage_details
                })
        
        secret_details["used_by_pods"] = using_pods
        secret_details["usage_count"] = len(using_pods)
    
    except Exception as e:
        secret_details["used_by_pods"] = []
        secret_details["usage_count"] = 0
        secret_details["usage_check_error"] = str(e)
    
    return {"status": "success", "data": secret_details}


describe_secret_tool = StructuredTool.from_function(
    func=describe_secret,
    name="describe_secret",
    description="Gets comprehensive detailed information about a specific Kubernetes Secret including usage analysis and optionally decoded data (use with caution).",
    args_schema=SecretInputSchema
)


# ===============================================================================
#                          CONFIGURATION ANALYSIS TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def search_configuration_data(search_key: Optional[str] = None, search_value: Optional[str] = None, 
                             config_type: str = "both", namespace: Optional[str] = None) -> Dict[str, Any]:
    """Search for keys or values across ConfigMaps and Secrets."""
    core_v1 = get_core_v1_api()
    results = {
        "configmaps": [],
        "secrets": [],
        "search_criteria": {
            "search_key": search_key,
            "search_value": search_value,
            "config_type": config_type,
            "namespace": namespace
        }
    }
    
    if config_type in ["both", "configmap"]:
        # Search ConfigMaps
        if namespace:
            configmap_list = core_v1.list_namespaced_config_map(namespace=namespace, timeout_seconds=10)
        else:
            configmap_list = core_v1.list_config_map_for_all_namespaces(timeout_seconds=10)
        
        for cm in configmap_list.items:
            matches = []
            data = cm.data or {}
            
            for key, value in data.items():
                key_matches = not search_key or search_key.lower() in key.lower()
                value_matches = not search_value or search_value.lower() in str(value).lower()
                
                if key_matches and value_matches:
                    matches.append({
                        "key": key,
                        "value_preview": str(value)[:200] + ("..." if len(str(value)) > 200 else ""),
                        "value_size": len(str(value))
                    })
            
            if matches:
                results["configmaps"].append({
                    "name": cm.metadata.name,
                    "namespace": cm.metadata.namespace,
                    "matches": matches,
                    "match_count": len(matches)
                })
    
    if config_type in ["both", "secret"]:
        # Search Secrets (keys only for security)
        if namespace:
            secret_list = core_v1.list_namespaced_secret(namespace=namespace, timeout_seconds=10)
        else:
            secret_list = core_v1.list_secret_for_all_namespaces(timeout_seconds=10)
        
        for secret in secret_list.items:
            matches = []
            data_keys = list((secret.data or {}).keys())
            
            for key in data_keys:
                key_matches = not search_key or search_key.lower() in key.lower()
                
                if key_matches:
                    matches.append({
                        "key": key,
                        "note": "Value not shown for security"
                    })
            
            # Only decode and search values if explicitly searching for values
            if search_value and secret.data:
                try:
                    decoded_data = _decode_secret_data(secret.data)
                    for key, value in decoded_data.items():
                        if search_value.lower() in str(value).lower():
                            matches.append({
                                "key": key,
                                "value_match": "Contains searched value",
                                "note": "Value content not shown for security"
                            })
                except Exception:
                    pass  # Skip decoding errors
            
            if matches:
                results["secrets"].append({
                    "name": secret.metadata.name,
                    "namespace": secret.metadata.namespace,
                    "type": secret.type,
                    "matches": matches,
                    "match_count": len(matches)
                })
    
    # Summary statistics
    results["summary"] = {
        "total_configmap_matches": len(results["configmaps"]),
        "total_secret_matches": len(results["secrets"]),
        "total_matches": len(results["configmaps"]) + len(results["secrets"])
    }
    
    return {"status": "success", "data": results}


search_configuration_data_tool = StructuredTool.from_function(
    func=search_configuration_data,
    name="search_configuration_data",
    description="Searches for keys or values across ConfigMaps and Secrets with security-conscious handling of Secret data.",
    args_schema=ConfigSearchInputSchema
)


@_handle_k8s_exceptions
def analyze_cluster_configuration() -> Dict[str, Any]:
    """Analyzes configuration resources across the entire cluster."""
    core_v1 = get_core_v1_api()
    
    # Get all ConfigMaps and Secrets
    configmaps = core_v1.list_config_map_for_all_namespaces(timeout_seconds=10)
    secrets = core_v1.list_secret_for_all_namespaces(timeout_seconds=10)
    
    analysis = {
        "configmap_analysis": {
            "total_count": len(configmaps.items),
            "by_namespace": {},
            "total_data_size": 0,
            "content_types": {},
            "largest_configmaps": []
        },
        "secret_analysis": {
            "total_count": len(secrets.items),
            "by_namespace": {},
            "by_type": {},
            "largest_secrets": []
        },
        "namespace_summary": {}
    }
    
    # Analyze ConfigMaps
    for cm in configmaps.items:
        ns = cm.metadata.namespace
        data_analysis = _analyze_config_data(cm.data or {}, "ConfigMap")
        
        if ns not in analysis["configmap_analysis"]["by_namespace"]:
            analysis["configmap_analysis"]["by_namespace"][ns] = 0
        analysis["configmap_analysis"]["by_namespace"][ns] += 1
        
        analysis["configmap_analysis"]["total_data_size"] += data_analysis["total_size_bytes"]
        
        # Merge content types
        for content_type, count in data_analysis["content_types"].items():
            analysis["configmap_analysis"]["content_types"][content_type] = \
                analysis["configmap_analysis"]["content_types"].get(content_type, 0) + count
        
        # Track largest ConfigMaps
        analysis["configmap_analysis"]["largest_configmaps"].append({
            "name": cm.metadata.name,
            "namespace": ns,
            "size_bytes": data_analysis["total_size_bytes"],
            "key_count": data_analysis["total_keys"]
        })
    
    # Sort and keep top 10 largest ConfigMaps
    analysis["configmap_analysis"]["largest_configmaps"].sort(
        key=lambda x: x["size_bytes"], reverse=True
    )
    analysis["configmap_analysis"]["largest_configmaps"] = \
        analysis["configmap_analysis"]["largest_configmaps"][:10]
    
    # Analyze Secrets
    for secret in secrets.items:
        ns = secret.metadata.namespace
        secret_type = secret.type
        
        if ns not in analysis["secret_analysis"]["by_namespace"]:
            analysis["secret_analysis"]["by_namespace"][ns] = 0
        analysis["secret_analysis"]["by_namespace"][ns] += 1
        
        if secret_type not in analysis["secret_analysis"]["by_type"]:
            analysis["secret_analysis"]["by_type"][secret_type] = 0
        analysis["secret_analysis"]["by_type"][secret_type] += 1
        
        # Track largest Secrets (by key count)
        key_count = len((secret.data or {}).keys()) + len((secret.string_data or {}).keys())
        analysis["secret_analysis"]["largest_secrets"].append({
            "name": secret.metadata.name,
            "namespace": ns,
            "type": secret_type,
            "key_count": key_count
        })
    
    # Sort and keep top 10 largest Secrets
    analysis["secret_analysis"]["largest_secrets"].sort(
        key=lambda x: x["key_count"], reverse=True
    )
    analysis["secret_analysis"]["largest_secrets"] = \
        analysis["secret_analysis"]["largest_secrets"][:10]
    
    # Create namespace summary
    all_namespaces = set()
    all_namespaces.update(analysis["configmap_analysis"]["by_namespace"].keys())
    all_namespaces.update(analysis["secret_analysis"]["by_namespace"].keys())
    
    for ns in all_namespaces:
        cm_count = analysis["configmap_analysis"]["by_namespace"].get(ns, 0)
        secret_count = analysis["secret_analysis"]["by_namespace"].get(ns, 0)
        
        analysis["namespace_summary"][ns] = {
            "configmaps": cm_count,
            "secrets": secret_count,
            "total_config_resources": cm_count + secret_count
        }
    
    return {"status": "success", "data": analysis}


analyze_cluster_configuration_tool = StructuredTool.from_function(
    func=analyze_cluster_configuration,
    name="analyze_cluster_configuration",
    description="Provides comprehensive analysis of all configuration resources (ConfigMaps and Secrets) across the entire cluster including size analysis and distribution statistics.",
    args_schema=NoArgumentsInputSchema
)


# ===============================================================================
#                           CREATE TOOLS
# ===============================================================================

class CreateConfigMapInputSchema(BaseModel):
    namespace: str = Field(description="The Kubernetes namespace where the ConfigMap will be created.")
    name: str = Field(description="The name of the ConfigMap.")
    data: Dict[str, str] = Field(description="Key-value pairs to store in the ConfigMap. All values must be strings.")


class CreateSecretInputSchema(BaseModel):
    namespace: str = Field(description="The Kubernetes namespace where the Secret will be created.")
    name: str = Field(description="The name of the Secret.")
    data: Dict[str, str] = Field(description="Key-value pairs to store in the Secret. Values are plain text and will be base64-encoded automatically.")
    secret_type: str = Field(default="Opaque", description="Secret type. Default is 'Opaque'. Use 'kubernetes.io/tls' for TLS certs.")


@_handle_k8s_exceptions
def create_configmap(namespace: str, name: str, data: Dict[str, str]) -> Dict[str, Any]:
    """Creates a ConfigMap in the specified namespace with the given key-value pairs."""
    from kubernetes import client as k8s_client
    core_v1 = get_core_v1_api()
    configmap = k8s_client.V1ConfigMap(
        api_version="v1",
        kind="ConfigMap",
        metadata=k8s_client.V1ObjectMeta(name=name, namespace=namespace),
        data=data,
    )
    resp = core_v1.create_namespaced_config_map(namespace=namespace, body=configmap)
    return {
        "status": "success",
        "message": f"ConfigMap '{name}' created in namespace '{namespace}'.",
        "data": {"name": resp.metadata.name, "namespace": resp.metadata.namespace, "keys": list(data.keys())},
    }


create_configmap_tool = StructuredTool.from_function(
    func=create_configmap,
    name="create_configmap",
    description="Creates a Kubernetes ConfigMap in the specified namespace with the provided key-value data pairs.",
    args_schema=CreateConfigMapInputSchema,
)


@_handle_k8s_exceptions
def create_secret(namespace: str, name: str, data: Dict[str, str], secret_type: str = "Opaque") -> Dict[str, Any]:
    """Creates a Kubernetes Secret in the specified namespace. Plain-text values are base64-encoded automatically."""
    from kubernetes import client as k8s_client
    core_v1 = get_core_v1_api()
    encoded_data = {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}
    secret = k8s_client.V1Secret(
        api_version="v1",
        kind="Secret",
        metadata=k8s_client.V1ObjectMeta(name=name, namespace=namespace),
        type=secret_type,
        data=encoded_data,
    )
    resp = core_v1.create_namespaced_secret(namespace=namespace, body=secret)
    return {
        "status": "success",
        "message": f"Secret '{name}' created in namespace '{namespace}'.",
        "data": {"name": resp.metadata.name, "namespace": resp.metadata.namespace, "keys": list(data.keys())},
    }


create_secret_tool = StructuredTool.from_function(
    func=create_secret,
    name="create_secret",
    description="Creates a Kubernetes Secret in the specified namespace. Accepts plain-text values which are automatically base64-encoded.",
    args_schema=CreateSecretInputSchema,
)


class DeleteConfigMapInputSchema(BaseModel):
    namespace: str = Field(..., description="Kubernetes namespace containing the ConfigMap.")
    name: str = Field(..., description="Name of the ConfigMap to delete.")


@_handle_k8s_exceptions
def delete_configmap(namespace: str, name: str) -> Dict[str, Any]:
    core_v1 = get_core_v1_api()
    core_v1.delete_namespaced_config_map(name=name, namespace=namespace)
    return {"status": "success", "message": f"ConfigMap '{name}' deleted from namespace '{namespace}'."}


delete_configmap_tool = StructuredTool.from_function(
    func=delete_configmap,
    name="delete_configmap",
    description="Deletes a Kubernetes ConfigMap from the specified namespace.",
    args_schema=DeleteConfigMapInputSchema,
)


class DeleteSecretInputSchema(BaseModel):
    namespace: str = Field(..., description="Kubernetes namespace containing the Secret.")
    name: str = Field(..., description="Name of the Secret to delete.")


@_handle_k8s_exceptions
def delete_secret(namespace: str, name: str) -> Dict[str, Any]:
    core_v1 = get_core_v1_api()
    core_v1.delete_namespaced_secret(name=name, namespace=namespace)
    return {"status": "success", "message": f"Secret '{name}' deleted from namespace '{namespace}'."}


delete_secret_tool = StructuredTool.from_function(
    func=delete_secret,
    name="delete_secret",
    description="Deletes a Kubernetes Secret from the specified namespace.",
    args_schema=DeleteSecretInputSchema,
)


# ===============================================================================
#                        PATCH CONFIGMAP KEY
# ===============================================================================

class PatchConfigMapKeyInputSchema(BaseModel):
    namespace: str = Field(description="The Kubernetes namespace where the ConfigMap is located.")
    configmap_name: str = Field(description="The name of the ConfigMap to patch.")
    key: str = Field(description="The key within the ConfigMap to update.")
    value: str = Field(description="The new value for the key.")


@_handle_k8s_exceptions
def patch_configmap_key(namespace: str, configmap_name: str, key: str, value: str) -> Dict[str, Any]:
    """Updates a single key in an existing Kubernetes ConfigMap without touching other keys.

    Equivalent to: kubectl patch configmap <name> -p '{"data": {"<key>": "<value>"}}'
    """
    core_v1 = get_core_v1_api()
    patch_body = {"data": {key: value}}
    core_v1.patch_namespaced_config_map(name=configmap_name, namespace=namespace, body=patch_body)
    return {
        "status": "success",
        "data": {
            "configmap": configmap_name,
            "namespace": namespace,
            "patched_key": key,
            "new_value": value,
        },
    }


patch_configmap_key_tool = StructuredTool.from_function(
    func=patch_configmap_key,
    name="patch_configmap_key",
    description="Update a single key in an existing ConfigMap without affecting other keys. Use for config changes like changing LOG_LEVEL or feature flags.",
    args_schema=PatchConfigMapKeyInputSchema,
)


# ===============================================================================
#                        PATCH SECRET KEY
# ===============================================================================

class PatchSecretKeyInputSchema(BaseModel):
    namespace: str = Field(description="The Kubernetes namespace where the Secret is located.")
    secret_name: str = Field(description="The name of the Secret to patch.")
    key: str = Field(description="The key within the Secret to update.")
    value: str = Field(description="The new plain-text value for the key (will be stored as base64 automatically by Kubernetes).")


@_handle_k8s_exceptions
def patch_secret_key(namespace: str, secret_name: str, key: str, value: str) -> Dict[str, Any]:
    """Updates a single key in an existing Kubernetes Secret without touching other keys.

    Uses stringData so the value is provided as plain text — Kubernetes base64-encodes it automatically.
    Equivalent to: kubectl patch secret <name> -p '{"stringData": {"<key>": "<value>"}}'
    """
    core_v1 = get_core_v1_api()
    patch_body = {"stringData": {key: value}}
    core_v1.patch_namespaced_secret(name=secret_name, namespace=namespace, body=patch_body)
    return {
        "status": "success",
        "data": {
            "secret": secret_name,
            "namespace": namespace,
            "patched_key": key,
            "message": f"Key '{key}' in Secret '{secret_name}' updated successfully.",
        },
    }


patch_secret_key_tool = StructuredTool.from_function(
    func=patch_secret_key,
    name="patch_secret_key",
    description="Update a single key in an existing Secret without affecting other keys. Provide the value as plain text — Kubernetes handles base64 encoding automatically. Use to fix wrong passwords, tokens, or credentials stored in a Secret.",
    args_schema=PatchSecretKeyInputSchema,
)


# ===============================================================================
#                           TOOL COLLECTION
# ===============================================================================

# Export all configuration tools for easy import
configmap_tools = [
    connectivity_check_tool,
    list_configmaps_tool,
    describe_configmap_tool,
    list_secrets_tool,
    describe_secret_tool,
    search_configuration_data_tool,
    analyze_cluster_configuration_tool,
    create_configmap_tool,
    create_secret_tool,
    delete_configmap_tool,
    delete_secret_tool,
    patch_configmap_key_tool,
    patch_secret_key_tool,
]

