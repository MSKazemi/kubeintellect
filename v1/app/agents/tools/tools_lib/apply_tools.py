import yaml
import os
from typing import Dict, Any, Optional

from kubernetes import client
from kubernetes.client.exceptions import ApiException
from kubernetes.utils import create_from_yaml
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.agents.tools.tools_lib._base import (
    get_core_v1_api,
    get_apps_v1_api,
    get_batch_v1_api,
    get_networking_v1_api,
    _handle_k8s_exceptions,
    _load_k8s_config,
)
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")


# ===============================================================================
#                               INPUT SCHEMAS
# ===============================================================================

class ApplyYamlFileInputSchema(BaseModel):
    """Schema for applying YAML from a file path."""
    yaml_file_path: str = Field(description="The path to the YAML file to apply.")
    namespace: Optional[str] = Field(default=None, description="The namespace to apply the resources to. If not provided, uses the namespace specified in the YAML or 'default'.")
    dry_run: bool = Field(default=False, description="If true, performs a dry run without actually creating resources.")


class ApplyYamlContentInputSchema(BaseModel):
    """Schema for applying YAML from content string."""
    yaml_content: str = Field(description="The YAML content as a string to apply.")
    namespace: Optional[str] = Field(default=None, description="The namespace to apply the resources to. If not provided, uses the namespace specified in the YAML or 'default'.")
    dry_run: bool = Field(default=False, description="If true, performs a dry run without actually creating resources.")


class DeleteResourceInputSchema(BaseModel):
    """Schema for deleting resources from YAML."""
    yaml_content: Optional[str] = Field(default=None, description="The YAML content as a string containing resources to delete.")
    yaml_file_path: Optional[str] = Field(default=None, description="The path to the YAML file containing resources to delete.")
    namespace: Optional[str] = Field(default=None, description="The namespace containing the resources to delete.")


# ===============================================================================
#                               APPLY TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def apply_yaml_file(yaml_file_path: str, namespace: Optional[str] = None, dry_run: bool = False) -> Dict[str, Any]:
    """Apply Kubernetes resources from a YAML file, similar to 'kubectl apply -f'."""
    if not os.path.exists(yaml_file_path):
        return {
            "status": "error",
            "message": f"YAML file not found: {yaml_file_path}",
            "error_type": "FileNotFound"
        }
    
    try:
        with open(yaml_file_path, 'r') as file:
            yaml_content = file.read()
        
        return apply_yaml_content(yaml_content, namespace, dry_run)
    
    except IOError as e:
        return {
            "status": "error",
            "message": f"Error reading YAML file: {str(e)}",
            "error_type": "IOError"
        }


apply_yaml_file_tool = StructuredTool.from_function(
    func=apply_yaml_file,
    name="apply_yaml_file",
    description="Apply Kubernetes resources from a YAML file, similar to 'kubectl apply -f <file>'. Supports dry run mode.",
    args_schema=ApplyYamlFileInputSchema
)


@_handle_k8s_exceptions
def apply_yaml_content(yaml_content: str, namespace: Optional[str] = None, dry_run: bool = False) -> Dict[str, Any]:
    """Apply Kubernetes resources from YAML content string."""
    _load_k8s_config()
    
    try:
        # Parse YAML documents (can be multiple resources separated by ---)
        documents = list(yaml.safe_load_all(yaml_content))
        documents = [doc for doc in documents if doc is not None]
        
        if not documents:
            return {
                "status": "error",
                "message": "No valid YAML documents found in content",
                "error_type": "ValidationError"
            }
        
        applied_resources = []
        failed_resources = []
        
        for doc in documents:
            try:
                result = _apply_single_resource(doc, namespace, dry_run)
                if result["status"] == "success":
                    applied_resources.append(result["data"])
                else:
                    failed_resources.append({
                        "resource": f"{doc.get('kind', 'Unknown')}/{doc.get('metadata', {}).get('name', 'Unknown')}",
                        "error": result["message"]
                    })
            except Exception as e:
                failed_resources.append({
                    "resource": f"{doc.get('kind', 'Unknown')}/{doc.get('metadata', {}).get('name', 'Unknown')}",
                    "error": str(e)
                })
        
        result = {
            "status": "success" if not failed_resources else "partial",
            "applied_resources": applied_resources,
            "failed_resources": failed_resources,
            "total_attempted": len(documents),
            "successful_count": len(applied_resources),
            "failed_count": len(failed_resources)
        }
        
        if dry_run:
            result["dry_run"] = True
            result["message"] = "Dry run completed - no resources were actually created/updated"
        
        return result
        
    except yaml.YAMLError as e:
        return {
            "status": "error",
            "message": f"YAML parsing error: {str(e)}",
            "error_type": "YAMLError"
        }


apply_yaml_content_tool = StructuredTool.from_function(
    func=apply_yaml_content,
    name="apply_yaml_content",
    description="Apply Kubernetes resources from YAML content string, similar to 'echo <yaml> | kubectl apply -f -'. Supports dry run mode.",
    args_schema=ApplyYamlContentInputSchema
)


def _apply_single_resource(resource_doc: Dict[str, Any], namespace: Optional[str] = None, dry_run: bool = False) -> Dict[str, Any]:
    """Apply a single Kubernetes resource."""
    kind = resource_doc.get("kind")
    api_version = resource_doc.get("apiVersion")
    metadata = resource_doc.get("metadata", {})
    resource_name = metadata.get("name")
    
    if not kind or not api_version or not resource_name:
        return {
            "status": "error",
            "message": "Invalid resource: missing kind, apiVersion, or name",
            "error_type": "ValidationError"
        }
    
    # Override namespace if provided
    if namespace:
        if "metadata" not in resource_doc:
            resource_doc["metadata"] = {}
        resource_doc["metadata"]["namespace"] = namespace
    
    # Get the resource namespace (from override, resource, or default)
    resource_namespace = resource_doc.get("metadata", {}).get("namespace", "default")
    
    try:
        # Use different API clients based on apiVersion
        if api_version == "v1":
            api_client = get_core_v1_api()
            result = _apply_core_v1_resource(api_client, resource_doc, dry_run)
        elif api_version.startswith("apps/"):
            api_client = get_apps_v1_api()
            result = _apply_apps_v1_resource(api_client, resource_doc, dry_run)
        elif api_version.startswith("batch/"):
            api_client = get_batch_v1_api()
            result = _apply_batch_v1_resource(api_client, resource_doc, dry_run)
        elif api_version.startswith("networking.k8s.io/"):
            api_client = get_networking_v1_api()
            result = _apply_networking_v1_resource(api_client, resource_doc, dry_run)
        else:
            # For other API versions, try to use the generic create_from_yaml
            return _apply_generic_resource(resource_doc, dry_run)
        
        return {
            "status": "success",
            "data": {
                "kind": kind,
                "name": resource_name,
                "namespace": resource_namespace,
                "action": result.get("action", "applied"),
                "api_version": api_version
            }
        }
        
    except ApiException as e:
        return {
            "status": "error",
            "message": f"Failed to apply {kind}/{resource_name}: {e.reason}",
            "error_type": "ApiException"
        }


def _apply_core_v1_resource(api_client, resource_doc: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    """Apply Core v1 resources (Pods, Services, ConfigMaps, etc.)."""
    kind = resource_doc.get("kind")
    name = resource_doc["metadata"]["name"]
    namespace = resource_doc.get("metadata", {}).get("namespace", "default")
    
    dry_run_param = "All" if dry_run else None
    
    if kind == "Pod":
        body = client.V1Pod(
            api_version="v1",
            kind="Pod",
            metadata=client.V1ObjectMeta(**resource_doc.get("metadata", {})),
            spec=client.V1PodSpec(**resource_doc.get("spec", {}))
        )
        try:
            # Try to read existing pod
            api_client.read_namespaced_pod(name=name, namespace=namespace)
            # Pod exists, update it
            response = api_client.replace_namespaced_pod(name=name, namespace=namespace, body=body, dry_run=dry_run_param)
            return {"action": "updated", "response": response}
        except ApiException as e:
            if e.status == 404:
                # Pod doesn't exist, create it
                response = api_client.create_namespaced_pod(namespace=namespace, body=body, dry_run=dry_run_param)
                return {"action": "created", "response": response}
            raise
    
    elif kind == "Service":
        body = client.V1Service(
            api_version="v1",
            kind="Service",
            metadata=client.V1ObjectMeta(**resource_doc.get("metadata", {})),
            spec=client.V1ServiceSpec(**resource_doc.get("spec", {}))
        )
        try:
            api_client.read_namespaced_service(name=name, namespace=namespace)
            response = api_client.replace_namespaced_service(name=name, namespace=namespace, body=body, dry_run=dry_run_param)
            return {"action": "updated", "response": response}
        except ApiException as e:
            if e.status == 404:
                response = api_client.create_namespaced_service(namespace=namespace, body=body, dry_run=dry_run_param)
                return {"action": "created", "response": response}
            raise
    
    elif kind == "ConfigMap":
        body = client.V1ConfigMap(
            api_version="v1",
            kind="ConfigMap",
            metadata=client.V1ObjectMeta(**resource_doc.get("metadata", {})),
            data=resource_doc.get("data", {}),
            binary_data=resource_doc.get("binaryData", {})
        )
        try:
            api_client.read_namespaced_config_map(name=name, namespace=namespace)
            response = api_client.replace_namespaced_config_map(name=name, namespace=namespace, body=body, dry_run=dry_run_param)
            return {"action": "updated", "response": response}
        except ApiException as e:
            if e.status == 404:
                response = api_client.create_namespaced_config_map(namespace=namespace, body=body, dry_run=dry_run_param)
                return {"action": "created", "response": response}
            raise
    
    elif kind == "Secret":
        body = client.V1Secret(
            api_version="v1",
            kind="Secret",
            metadata=client.V1ObjectMeta(**resource_doc.get("metadata", {})),
            type=resource_doc.get("type", "Opaque"),
            data=resource_doc.get("data", {}),
            string_data=resource_doc.get("stringData", {})
        )
        try:
            api_client.read_namespaced_secret(name=name, namespace=namespace)
            response = api_client.replace_namespaced_secret(name=name, namespace=namespace, body=body, dry_run=dry_run_param)
            return {"action": "updated", "response": response}
        except ApiException as e:
            if e.status == 404:
                response = api_client.create_namespaced_secret(namespace=namespace, body=body, dry_run=dry_run_param)
                return {"action": "created", "response": response}
            raise
    
    elif kind == "Namespace":
        body = client.V1Namespace(
            api_version="v1",
            kind="Namespace",
            metadata=client.V1ObjectMeta(**resource_doc.get("metadata", {}))
        )
        try:
            api_client.read_namespace(name=name)
            response = api_client.replace_namespace(name=name, body=body, dry_run=dry_run_param)
            return {"action": "updated", "response": response}
        except ApiException as e:
            if e.status == 404:
                response = api_client.create_namespace(body=body, dry_run=dry_run_param)
                return {"action": "created", "response": response}
            raise
    
    else:
        raise ValueError(f"Unsupported Core v1 resource kind: {kind}")


def _apply_apps_v1_resource(api_client, resource_doc: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    """Apply Apps v1 resources (Deployments, DaemonSets, etc.)."""
    kind = resource_doc.get("kind")
    name = resource_doc["metadata"]["name"]
    namespace = resource_doc.get("metadata", {}).get("namespace", "default")
    
    dry_run_param = "All" if dry_run else None
    
    if kind == "Deployment":
        body = client.V1Deployment(
            api_version="apps/v1",
            kind="Deployment",
            metadata=client.V1ObjectMeta(**resource_doc.get("metadata", {})),
            spec=client.V1DeploymentSpec(**resource_doc.get("spec", {}))
        )
        try:
            api_client.read_namespaced_deployment(name=name, namespace=namespace)
            response = api_client.replace_namespaced_deployment(name=name, namespace=namespace, body=body, dry_run=dry_run_param)
            return {"action": "updated", "response": response}
        except ApiException as e:
            if e.status == 404:
                response = api_client.create_namespaced_deployment(namespace=namespace, body=body, dry_run=dry_run_param)
                return {"action": "created", "response": response}
            raise
    
    elif kind == "DaemonSet":
        body = client.V1DaemonSet(
            api_version="apps/v1",
            kind="DaemonSet",
            metadata=client.V1ObjectMeta(**resource_doc.get("metadata", {})),
            spec=client.V1DaemonSetSpec(**resource_doc.get("spec", {}))
        )
        try:
            api_client.read_namespaced_daemon_set(name=name, namespace=namespace)
            response = api_client.replace_namespaced_daemon_set(name=name, namespace=namespace, body=body, dry_run=dry_run_param)
            return {"action": "updated", "response": response}
        except ApiException as e:
            if e.status == 404:
                response = api_client.create_namespaced_daemon_set(namespace=namespace, body=body, dry_run=dry_run_param)
                return {"action": "created", "response": response}
            raise
    
    elif kind == "StatefulSet":
        body = client.V1StatefulSet(
            api_version="apps/v1",
            kind="StatefulSet",
            metadata=client.V1ObjectMeta(**resource_doc.get("metadata", {})),
            spec=client.V1StatefulSetSpec(**resource_doc.get("spec", {}))
        )
        try:
            api_client.read_namespaced_stateful_set(name=name, namespace=namespace)
            response = api_client.replace_namespaced_stateful_set(name=name, namespace=namespace, body=body, dry_run=dry_run_param)
            return {"action": "updated", "response": response}
        except ApiException as e:
            if e.status == 404:
                response = api_client.create_namespaced_stateful_set(namespace=namespace, body=body, dry_run=dry_run_param)
                return {"action": "created", "response": response}
            raise
    
    else:
        raise ValueError(f"Unsupported Apps v1 resource kind: {kind}")


def _apply_batch_v1_resource(api_client, resource_doc: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    """Apply Batch v1 resources (Jobs, CronJobs)."""
    kind = resource_doc.get("kind")
    name = resource_doc["metadata"]["name"]
    namespace = resource_doc.get("metadata", {}).get("namespace", "default")
    
    dry_run_param = "All" if dry_run else None
    
    if kind == "Job":
        body = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(**resource_doc.get("metadata", {})),
            spec=client.V1JobSpec(**resource_doc.get("spec", {}))
        )
        try:
            api_client.read_namespaced_job(name=name, namespace=namespace)
            response = api_client.replace_namespaced_job(name=name, namespace=namespace, body=body, dry_run=dry_run_param)
            return {"action": "updated", "response": response}
        except ApiException as e:
            if e.status == 404:
                response = api_client.create_namespaced_job(namespace=namespace, body=body, dry_run=dry_run_param)
                return {"action": "created", "response": response}
            raise
    
    elif kind == "CronJob":
        body = client.V1CronJob(
            api_version="batch/v1",
            kind="CronJob",
            metadata=client.V1ObjectMeta(**resource_doc.get("metadata", {})),
            spec=client.V1CronJobSpec(**resource_doc.get("spec", {}))
        )
        try:
            api_client.read_namespaced_cron_job(name=name, namespace=namespace)
            response = api_client.replace_namespaced_cron_job(name=name, namespace=namespace, body=body, dry_run=dry_run_param)
            return {"action": "updated", "response": response}
        except ApiException as e:
            if e.status == 404:
                response = api_client.create_namespaced_cron_job(namespace=namespace, body=body, dry_run=dry_run_param)
                return {"action": "created", "response": response}
            raise
    
    else:
        raise ValueError(f"Unsupported Batch v1 resource kind: {kind}")


def _apply_networking_v1_resource(api_client, resource_doc: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    """Apply Networking v1 resources (Ingress, NetworkPolicy)."""
    kind = resource_doc.get("kind")
    name = resource_doc["metadata"]["name"]
    namespace = resource_doc.get("metadata", {}).get("namespace", "default")
    
    dry_run_param = "All" if dry_run else None
    
    if kind == "Ingress":
        body = client.V1Ingress(
            api_version="networking.k8s.io/v1",
            kind="Ingress",
            metadata=client.V1ObjectMeta(**resource_doc.get("metadata", {})),
            spec=client.V1IngressSpec(**resource_doc.get("spec", {}))
        )
        try:
            api_client.read_namespaced_ingress(name=name, namespace=namespace)
            response = api_client.replace_namespaced_ingress(name=name, namespace=namespace, body=body, dry_run=dry_run_param)
            return {"action": "updated", "response": response}
        except ApiException as e:
            if e.status == 404:
                response = api_client.create_namespaced_ingress(namespace=namespace, body=body, dry_run=dry_run_param)
                return {"action": "created", "response": response}
            raise
    
    elif kind == "NetworkPolicy":
        body = client.V1NetworkPolicy(
            api_version="networking.k8s.io/v1",
            kind="NetworkPolicy",
            metadata=client.V1ObjectMeta(**resource_doc.get("metadata", {})),
            spec=client.V1NetworkPolicySpec(**resource_doc.get("spec", {}))
        )
        try:
            api_client.read_namespaced_network_policy(name=name, namespace=namespace)
            response = api_client.replace_namespaced_network_policy(name=name, namespace=namespace, body=body, dry_run=dry_run_param)
            return {"action": "updated", "response": response}
        except ApiException as e:
            if e.status == 404:
                response = api_client.create_namespaced_network_policy(namespace=namespace, body=body, dry_run=dry_run_param)
                return {"action": "created", "response": response}
            raise
    
    else:
        raise ValueError(f"Unsupported Networking v1 resource kind: {kind}")


def _apply_generic_resource(resource_doc: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    """Apply resources using the generic create_from_yaml utility."""
    try:
        # Convert the resource doc back to YAML string for create_from_yaml
        yaml_str = yaml.dump(resource_doc)
        
        # Use the kubernetes utils to create the resource
        k8s_client = client.ApiClient()
        
        if dry_run:
            # For dry run, we can't easily use create_from_yaml, so we'll return a placeholder
            return {
                "status": "success",
                "data": {
                    "kind": resource_doc.get("kind"),
                    "name": resource_doc.get("metadata", {}).get("name"),
                    "namespace": resource_doc.get("metadata", {}).get("namespace", "default"),
                    "action": "dry-run",
                    "api_version": resource_doc.get("apiVersion")
                }
            }
        
        # This is a simplified approach - in practice, you might want more sophisticated handling
        create_from_yaml(k8s_client, yaml_str)
        
        return {
            "status": "success",
            "data": {
                "kind": resource_doc.get("kind"),
                "name": resource_doc.get("metadata", {}).get("name"),
                "namespace": resource_doc.get("metadata", {}).get("namespace", "default"),
                "action": "applied",
                "api_version": resource_doc.get("apiVersion")
            }
        }
    
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to apply resource: {str(e)}",
            "error_type": "GenericError"
        }


@_handle_k8s_exceptions
def delete_from_yaml(yaml_content: Optional[str] = None, yaml_file_path: Optional[str] = None, 
                     namespace: Optional[str] = None) -> Dict[str, Any]:
    """Delete Kubernetes resources specified in YAML content or file."""
    if not yaml_content and not yaml_file_path:
        return {
            "status": "error",
            "message": "Either yaml_content or yaml_file_path must be provided",
            "error_type": "ValidationError"
        }
    
    if yaml_file_path:
        if not os.path.exists(yaml_file_path):
            return {
                "status": "error",
                "message": f"YAML file not found: {yaml_file_path}",
                "error_type": "FileNotFound"
            }
        
        try:
            with open(yaml_file_path, 'r') as file:
                yaml_content = file.read()
        except IOError as e:
            return {
                "status": "error",
                "message": f"Error reading YAML file: {str(e)}",
                "error_type": "IOError"
            }
    
    try:
        documents = list(yaml.safe_load_all(yaml_content))
        documents = [doc for doc in documents if doc is not None]
        
        if not documents:
            return {
                "status": "error",
                "message": "No valid YAML documents found in content",
                "error_type": "ValidationError"
            }
        
        deleted_resources = []
        failed_resources = []
        
        for doc in documents:
            try:
                result = _delete_single_resource(doc, namespace)
                if result["status"] == "success":
                    deleted_resources.append(result["data"])
                else:
                    failed_resources.append({
                        "resource": f"{doc.get('kind', 'Unknown')}/{doc.get('metadata', {}).get('name', 'Unknown')}",
                        "error": result["message"]
                    })
            except Exception as e:
                failed_resources.append({
                    "resource": f"{doc.get('kind', 'Unknown')}/{doc.get('metadata', {}).get('name', 'Unknown')}",
                    "error": str(e)
                })
        
        return {
            "status": "success" if not failed_resources else "partial",
            "deleted_resources": deleted_resources,
            "failed_resources": failed_resources,
            "total_attempted": len(documents),
            "successful_count": len(deleted_resources),
            "failed_count": len(failed_resources)
        }
        
    except yaml.YAMLError as e:
        return {
            "status": "error",
            "message": f"YAML parsing error: {str(e)}",
            "error_type": "YAMLError"
        }


def _delete_single_resource(resource_doc: Dict[str, Any], namespace: Optional[str] = None) -> Dict[str, Any]:
    """Delete a single Kubernetes resource."""
    kind = resource_doc.get("kind")
    api_version = resource_doc.get("apiVersion")
    metadata = resource_doc.get("metadata", {})
    resource_name = metadata.get("name")
    resource_namespace = namespace or metadata.get("namespace", "default")
    
    if not kind or not api_version or not resource_name:
        return {
            "status": "error",
            "message": "Invalid resource: missing kind, apiVersion, or name",
            "error_type": "ValidationError"
        }
    
    try:
        if api_version == "v1":
            api_client = get_core_v1_api()
            _delete_core_v1_resource(api_client, kind, resource_name, resource_namespace)
        elif api_version.startswith("apps/"):
            api_client = get_apps_v1_api()
            _delete_apps_v1_resource(api_client, kind, resource_name, resource_namespace)
        elif api_version.startswith("batch/"):
            api_client = get_batch_v1_api()
            _delete_batch_v1_resource(api_client, kind, resource_name, resource_namespace)
        elif api_version.startswith("networking.k8s.io/"):
            api_client = get_networking_v1_api()
            _delete_networking_v1_resource(api_client, kind, resource_name, resource_namespace)
        else:
            return {
                "status": "error",
                "message": f"Unsupported API version for deletion: {api_version}",
                "error_type": "UnsupportedApiVersion"
            }
        
        return {
            "status": "success",
            "data": {
                "kind": kind,
                "name": resource_name,
                "namespace": resource_namespace,
                "action": "deleted",
                "api_version": api_version
            }
        }
        
    except ApiException as e:
        if e.status == 404:
            return {
                "status": "success",
                "data": {
                    "kind": kind,
                    "name": resource_name,
                    "namespace": resource_namespace,
                    "action": "not_found",
                    "api_version": api_version
                }
            }
        return {
            "status": "error",
            "message": f"Failed to delete {kind}/{resource_name}: {e.reason}",
            "error_type": "ApiException"
        }


def _delete_core_v1_resource(api_client, kind: str, name: str, namespace: str):
    """Delete Core v1 resources."""
    if kind == "Pod":
        api_client.delete_namespaced_pod(name=name, namespace=namespace)
    elif kind == "Service":
        api_client.delete_namespaced_service(name=name, namespace=namespace)
    elif kind == "ConfigMap":
        api_client.delete_namespaced_config_map(name=name, namespace=namespace)
    elif kind == "Secret":
        api_client.delete_namespaced_secret(name=name, namespace=namespace)
    elif kind == "Namespace":
        api_client.delete_namespace(name=name)
    else:
        raise ValueError(f"Unsupported Core v1 resource kind for deletion: {kind}")


def _delete_apps_v1_resource(api_client, kind: str, name: str, namespace: str):
    """Delete Apps v1 resources."""
    if kind == "Deployment":
        api_client.delete_namespaced_deployment(name=name, namespace=namespace)
    elif kind == "DaemonSet":
        api_client.delete_namespaced_daemon_set(name=name, namespace=namespace)
    elif kind == "StatefulSet":
        api_client.delete_namespaced_stateful_set(name=name, namespace=namespace)
    else:
        raise ValueError(f"Unsupported Apps v1 resource kind for deletion: {kind}")


def _delete_batch_v1_resource(api_client, kind: str, name: str, namespace: str):
    """Delete Batch v1 resources."""
    if kind == "Job":
        api_client.delete_namespaced_job(name=name, namespace=namespace)
    elif kind == "CronJob":
        api_client.delete_namespaced_cron_job(name=name, namespace=namespace)
    else:
        raise ValueError(f"Unsupported Batch v1 resource kind for deletion: {kind}")


def _delete_networking_v1_resource(api_client, kind: str, name: str, namespace: str):
    """Delete Networking v1 resources."""
    if kind == "Ingress":
        api_client.delete_namespaced_ingress(name=name, namespace=namespace)
    elif kind == "NetworkPolicy":
        api_client.delete_namespaced_network_policy(name=name, namespace=namespace)
    else:
        raise ValueError(f"Unsupported Networking v1 resource kind for deletion: {kind}")


delete_from_yaml_tool = StructuredTool.from_function(
    func=delete_from_yaml,
    name="delete_from_yaml",
    description="Delete Kubernetes resources specified in YAML content or file, similar to 'kubectl delete -f'.",
    args_schema=DeleteResourceInputSchema
)


# ===============================================================================
#                           TOOL COLLECTION
# ===============================================================================

# Export all apply tools for easy import
apply_tools = [
    apply_yaml_file_tool,
    apply_yaml_content_tool,
    delete_from_yaml_tool,
]
