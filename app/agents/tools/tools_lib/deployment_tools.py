import json
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

from kubernetes import client
from kubernetes.client.exceptions import ApiException
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.agents.tools.tools_lib._base import (
    get_core_v1_api,
    get_apps_v1_api,
    _handle_k8s_exceptions,
    NoArgumentsInputSchema,
    NamespaceInputSchema,
)
from app.services import kubernetes_service
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")


# ===============================================================================
#                               INPUT SCHEMAS
# ===============================================================================

class PodInputSchema(BaseModel):
    """Schema for tools that require pod name and namespace."""
    namespace: str = Field(description="The Kubernetes namespace where the pod is located.")
    pod_name: str = Field(description="The name of the Kubernetes pod.")


class DeploymentInputSchema(BaseModel):
    """Schema for tools that require deployment name and namespace."""
    namespace: str = Field(description="The Kubernetes namespace where the deployment is located.")
    deployment_name: str = Field(description="The name of the Kubernetes deployment.")


class TimeRangeInputSchema(BaseModel):
    """Schema for tools that use time ranges."""
    hours: int = Field(default=48, description="The number of hours to look back. Defaults to 48 hours.")


class CreateDeploymentInputSchema(BaseModel):
    """Schema for creating Kubernetes deployments."""
    namespace: str = Field(description="The Kubernetes namespace where the deployment will be created.")
    deployment_name: str = Field(description="The name of the deployment. Must be RFC 1123 compliant.")
    container_image: str = Field(description="The container image to use for the deployment.")
    replicas: int = Field(default=1, description="The number of replicas for the deployment.")
    labels: Optional[Dict[str, str]] = Field(default=None, description="Pod labels to apply, e.g. {'app': 'api', 'env': 'prod'}. The app label defaults to the deployment name.")
    env_vars: Optional[Dict[str, str]] = Field(default=None, description="Direct environment variables as key-value pairs, e.g. {'DB_HOST': 'postgres', 'PORT': '8080'}.")
    env_from_secret: Optional[str] = Field(default=None, description="Name of a Secret to inject as environment variables (envFrom secretRef). All keys in the Secret become env vars.")
    env_from_configmap: Optional[str] = Field(default=None, description="Name of a ConfigMap to inject as environment variables (envFrom configMapRef). All keys in the ConfigMap become env vars.")
    command: Optional[List[str]] = Field(default=None, description="Container command as a list, e.g. ['sh', '-c', 'while true; do sleep 10; done']. Overrides the image ENTRYPOINT.")
    cpu_request: Optional[str] = Field(default=None, description="CPU request for the container, e.g. '100m'. Recommended for resource-aware scheduling.")
    memory_request: Optional[str] = Field(default=None, description="Memory request for the container, e.g. '128Mi'. Recommended for resource-aware scheduling.")
    cpu_limit: Optional[str] = Field(default=None, description="CPU limit for the container, e.g. '500m'.")
    memory_limit: Optional[str] = Field(default=None, description="Memory limit for the container, e.g. '256Mi'.")


class ConnectivityCheckInputSchema(BaseModel):
    """Schema for connectivity check tool."""
    timeout_seconds: Optional[int] = Field(default=5, description="The timeout for the Kubernetes API call in seconds.")


class ScaleDeploymentInputSchema(BaseModel):
    """Schema for scaling a deployment."""
    namespace: str = Field(description="The Kubernetes namespace where the deployment is located.")
    deployment_name: str = Field(description="The name of the Kubernetes deployment to scale.")
    replicas: int = Field(description="The desired number of replicas.", ge=0)


class RolloutUndoInputSchema(BaseModel):
    """Schema for rolling back a deployment."""
    namespace: str = Field(description="The Kubernetes namespace where the deployment is located.")
    deployment_name: str = Field(description="The name of the Kubernetes deployment to roll back.")
    revision: int = Field(default=0, description="Target revision number to roll back to. Use 0 (default) to roll back to the previous revision.")


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
#                            DEPLOYMENT TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def list_all_deployments() -> Dict[str, Any]:
    """Lists all Kubernetes deployments across all namespaces."""
    apps_v1 = get_apps_v1_api()
    deployments = apps_v1.list_deployment_for_all_namespaces(timeout_seconds=10)
    deployment_list = [
        {
            "name": d.metadata.name,
            "namespace": d.metadata.namespace,
            "replicas": d.spec.replicas,
            "labels": d.metadata.labels,
        }
        for d in deployments.items
    ]
    return {"status": "success", "total_count": len(deployment_list), "data": deployment_list}


list_all_deployments_tool = StructuredTool.from_function(
    func=list_all_deployments,
    name="list_all_deployments_across_namespaces",
    description="Lists all Kubernetes deployments across all namespaces. Returns name, namespace, replicas, and labels for each deployment. No input arguments are required.",
    args_schema=NoArgumentsInputSchema
)


@_handle_k8s_exceptions
def list_deployments_in_namespace(namespace: str) -> Dict[str, Any]:
    """Lists all Kubernetes deployments in a specified namespace."""
    apps_v1 = get_apps_v1_api()
    deployments = apps_v1.list_namespaced_deployment(namespace=namespace, timeout_seconds=10)
    deployment_names = [deployment.metadata.name for deployment in deployments.items]
    return {"status": "success", "total_count": len(deployment_names), "data": deployment_names}


list_deployments_in_namespace_tool = StructuredTool.from_function(
    func=list_deployments_in_namespace,
    name="list_deployments_in_namespace",
    description="Lists all Kubernetes deployments in a specified namespace. Returns a list of deployment names.",
    args_schema=NamespaceInputSchema
)


@_handle_k8s_exceptions
def describe_kubernetes_deployment(namespace: str, deployment_name: str) -> Dict[str, Any]:
    """Retrieves detailed information about a specific Kubernetes deployment."""
    apps_v1 = get_apps_v1_api()
    deployment = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
    deployment_description = {
        "name": deployment.metadata.name,
        "namespace": deployment.metadata.namespace,
        "labels": deployment.metadata.labels,
        "annotations": deployment.metadata.annotations,
        "replicas": deployment.spec.replicas,
        "selector": deployment.spec.selector.match_labels,
        "strategy": deployment.spec.strategy.type,
        "containers": [
            {
                "name": container.name,
                "image": container.image,
                "ports": [{"name": port.name, "port": port.container_port} for port in container.ports] if container.ports else [],
                "resources": container.resources.to_dict() if container.resources else {},
                "env": [
                    {"name": e.name, "value": e.value or "(from secret/configmap)"}
                    for e in container.env
                ] if container.env else [],
                "liveness_probe": container.liveness_probe.to_dict() if container.liveness_probe else None,
                "readiness_probe": container.readiness_probe.to_dict() if container.readiness_probe else None,
            }
            for container in deployment.spec.template.spec.containers
        ]
    }
    return {"status": "success", "data": deployment_description}


describe_kubernetes_deployment_tool = StructuredTool.from_function(
    func=describe_kubernetes_deployment,
    name="describe_kubernetes_deployment",
    description="Retrieves detailed information about a specific Kubernetes deployment including metadata, containers, and resource specifications.",
    args_schema=DeploymentInputSchema
)


@_handle_k8s_exceptions
def create_kubernetes_deployment(
    namespace: str,
    deployment_name: str,
    container_image: str,
    replicas: int = 1,
    labels: Optional[Dict[str, str]] = None,
    env_vars: Optional[Dict[str, str]] = None,
    env_from_secret: Optional[str] = None,
    env_from_configmap: Optional[str] = None,
    command: Optional[List[str]] = None,
    cpu_request: Optional[str] = None,
    memory_request: Optional[str] = None,
    cpu_limit: Optional[str] = None,
    memory_limit: Optional[str] = None,
) -> Dict[str, Any]:
    """Creates a Kubernetes deployment in a specified namespace."""
    # Pre-flight: verify the target namespace exists before issuing any write call.
    # This converts a silent 4xx routing exit into a clear, actionable error in the
    # same agent turn — namespace not found is never a retriable error.
    core_v1 = get_core_v1_api()
    try:
        core_v1.read_namespace(name=namespace)
    except ApiException as _ns_exc:
        if _ns_exc.status == 404:
            _ns_hint = ""
            try:
                _ns_list = core_v1.list_namespace(timeout_seconds=10)
                _available = sorted(ns.metadata.name for ns in _ns_list.items)
                if _available:
                    _ns_hint = f" Available namespaces: {', '.join(_available)}."
            except Exception:
                pass  # enumeration is advisory — never block the error response
            logger.info(
                "namespace_precheck_failed tool=create_kubernetes_deployment "
                "namespace=%s http_status=404",
                namespace,
            )
            return {
                "status": "error",
                "message": (
                    f"Namespace '{namespace}' not found.{_ns_hint} "
                    "Create it first or choose an existing namespace."
                ),
                "error_type": "NamespaceNotFound",
                "suggested_action": (
                    f"Use create_kubernetes_namespace to create '{namespace}', "
                    "or redeploy targeting one of the listed namespaces."
                ),
            }
        elif _ns_exc.status == 403:
            logger.info(
                "namespace_precheck_failed tool=create_kubernetes_deployment "
                "namespace=%s http_status=403",
                namespace,
            )
            return {
                "status": "error",
                "message": f"Insufficient permissions to verify namespace '{namespace}'.",
                "error_type": "Forbidden",
                "suggested_action": (
                    "Ensure the service account has 'get' permission on namespaces. "
                    "Use check_who_can or describe_service_account to inspect RBAC."
                ),
            }
        else:
            logger.info(
                "namespace_precheck_failed tool=create_kubernetes_deployment "
                "namespace=%s http_status=%s",
                namespace, _ns_exc.status,
            )
            return {
                "status": "error",
                "message": f"Could not verify namespace '{namespace}': {_ns_exc.reason} (status: {_ns_exc.status}).",
                "error_type": "ApiException",
                "suggested_action": "Check cluster connectivity and try again.",
            }
    except Exception as _ns_exc:
        logger.info(
            "namespace_precheck_failed tool=create_kubernetes_deployment "
            "namespace=%s error=%s",
            namespace, _ns_exc,
        )
        return {
            "status": "error",
            "message": f"Could not verify namespace '{namespace}': {_ns_exc}.",
            "error_type": type(_ns_exc).__name__,
            "suggested_action": "Check cluster connectivity and try again.",
        }

    safe_name = deployment_name.replace("_", "-").lower()

    # Build labels — always include app label for selector.
    # Caller-supplied labels must NOT override the 'app' key: Kubernetes requires
    # spec.selector.matchLabels to be an exact subset of spec.template.metadata.labels,
    # and our selector is always {"app": safe_name}.  If the caller passes a different
    # "app" value the two sets diverge → hard 422.  Strip it silently.
    pod_labels = {"app": safe_name}
    if labels:
        extra = {k: v for k, v in labels.items() if k != "app"}
        pod_labels.update(extra)

    # Build resource requirements
    resource_requirements = None
    res_requests = {}
    res_limits = {}
    if cpu_request:
        res_requests["cpu"] = cpu_request
    if memory_request:
        res_requests["memory"] = memory_request
    if cpu_limit:
        res_limits["cpu"] = cpu_limit
    if memory_limit:
        res_limits["memory"] = memory_limit
    if res_requests or res_limits:
        resource_requirements = client.V1ResourceRequirements(
            requests=res_requests or None,
            limits=res_limits or None,
        )

    # Build env var list
    env_list = []
    if env_vars:
        for k, v in env_vars.items():
            env_list.append(client.V1EnvVar(name=k, value=str(v)))

    # Build envFrom list
    env_from_list = []
    if env_from_secret:
        env_from_list.append(client.V1EnvFromSource(
            secret_ref=client.V1SecretEnvSource(name=env_from_secret)
        ))
    if env_from_configmap:
        env_from_list.append(client.V1EnvFromSource(
            config_map_ref=client.V1ConfigMapEnvSource(name=env_from_configmap)
        ))

    # Define the container
    container = client.V1Container(
        name=safe_name,
        image=container_image,
        resources=resource_requirements,
        env=env_list or None,
        env_from=env_from_list or None,
        command=command or None,
    )

    # Define the pod template
    template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels=pod_labels),
        spec=client.V1PodSpec(containers=[container])
    )

    # Use app label as selector (always present)
    spec = client.V1DeploymentSpec(
        replicas=replicas,
        selector=client.V1LabelSelector(match_labels={"app": safe_name}),
        template=template
    )

    deployment = client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(name=safe_name, namespace=namespace, labels=pod_labels),
        spec=spec
    )

    apps_v1 = get_apps_v1_api()
    try:
        response = apps_v1.create_namespaced_deployment(namespace=namespace, body=deployment)
    except ApiException as e:
        if e.status == 409:
            # Deployment already exists — return current state as success (idempotent).
            existing = apps_v1.read_namespaced_deployment(name=safe_name, namespace=namespace)
            return {
                "status": "success",
                "data": {
                    "name": existing.metadata.name,
                    "namespace": existing.metadata.namespace,
                    "replicas": existing.spec.replicas,
                    "message": f"Deployment '{safe_name}' already exists in namespace '{namespace}' — returning current state.",
                },
            }
        raise
    return {"status": "success", "data": {"name": response.metadata.name, "namespace": response.metadata.namespace}}


create_kubernetes_deployment_tool = StructuredTool.from_function(
    func=create_kubernetes_deployment,
    name="create_kubernetes_deployment",
    description=(
        "Creates a Kubernetes Deployment. Supports: replicas, labels, direct env vars (env_vars), "
        "injecting all keys from a Secret (env_from_secret) or ConfigMap (env_from_configmap) as env vars, "
        "custom container command, and CPU/memory requests and limits. "
        "Use env_from_secret and env_from_configmap for envFrom-style injection."
    ),
    args_schema=CreateDeploymentInputSchema
)


@_handle_k8s_exceptions
def check_deployments_missing_resource_limits(namespace: str) -> Dict[str, Any]:
    """Checks which deployments in a namespace have containers missing resource limits."""
    apps_v1 = get_apps_v1_api()
    deployments = apps_v1.list_namespaced_deployment(namespace=namespace, timeout_seconds=10)
    
    missing_limits = []
    for deployment in deployments.items:
        deployment_name = deployment.metadata.name
        containers = deployment.spec.template.spec.containers
        for container in containers:
            if not container.resources or not container.resources.limits:
                missing_limits.append({"deployment": deployment_name, "container": container.name})

    return {"status": "success", "data": missing_limits}


check_deployments_missing_resource_limits_tool = StructuredTool.from_function(
    func=check_deployments_missing_resource_limits,
    name="check_deployments_missing_resource_limits",
    description="Checks which deployments in a specified namespace have containers missing resource limits.",
    args_schema=NamespaceInputSchema
)


@_handle_k8s_exceptions
def check_deployments_without_affinity(namespace: str) -> Dict[str, Any]:
    """Checks deployments with multiple replicas but no affinity rules."""
    apps_v1 = get_apps_v1_api()
    deployments = apps_v1.list_namespaced_deployment(namespace=namespace, timeout_seconds=10)
    
    problematic_deployments = []
    for deployment in deployments.items:
        replicas = deployment.spec.replicas
        affinity = deployment.spec.template.spec.affinity
        if replicas and replicas > 1 and not affinity:
            problematic_deployments.append({
                "name": deployment.metadata.name,
                "replicas": replicas
            })

    return {"status": "success", "data": problematic_deployments}


check_deployments_without_affinity_tool = StructuredTool.from_function(
    func=check_deployments_without_affinity,
    name="check_deployments_without_affinity",
    description="Identifies deployments with multiple replicas but lacking affinity rules, which could lead to pods being scheduled on the same node.",
    args_schema=NamespaceInputSchema
)


@_handle_k8s_exceptions
def get_recent_deployment_changelog(hours: int = 48) -> Dict[str, Any]:
    """Fetches deployments modified within the last specified number of hours across all namespaces."""
    apps_v1 = get_apps_v1_api()
    
    # Calculate the time threshold
    time_threshold = datetime.utcnow() - timedelta(hours=hours)
    
    # Fetch all deployments across all namespaces
    deployments = apps_v1.list_deployment_for_all_namespaces(timeout_seconds=30)
    
    # Filter deployments modified within the time range
    recent_changes = []
    for deployment in deployments.items:
        # Check creation time and last updated time
        creation_time = deployment.metadata.creation_timestamp.replace(tzinfo=None)
        last_update_time = creation_time
        
        # Check for more recent update from status
        if deployment.status and deployment.status.conditions:
            for condition in deployment.status.conditions:
                if condition.last_update_time:
                    condition_time = condition.last_update_time.replace(tzinfo=None)
                    if condition_time > last_update_time:
                        last_update_time = condition_time
        
        if last_update_time >= time_threshold:
            recent_changes.append({
                "name": deployment.metadata.name,
                "namespace": deployment.metadata.namespace,
                "last_modified": last_update_time.isoformat(),
                "creation_time": creation_time.isoformat()
            })
    
    return {"status": "success", "data": recent_changes}


get_recent_deployment_changelog_tool = StructuredTool.from_function(
    func=get_recent_deployment_changelog,
    name="get_recent_deployment_changelog",
    description="Fetches deployments modified within the last specified number of hours across all namespaces.",
    args_schema=TimeRangeInputSchema
)


@_handle_k8s_exceptions
def scale_deployment(namespace: str, deployment_name: str, replicas: int) -> Dict[str, Any]:
    """Scales a Kubernetes deployment to the specified number of replicas."""
    apps_v1 = get_apps_v1_api()
    patch_body = {"spec": {"replicas": replicas}}
    response = apps_v1.patch_namespaced_deployment_scale(
        name=deployment_name, namespace=namespace, body=patch_body
    )
    return {
        "status": "success",
        "data": {
            "name": deployment_name,
            "namespace": namespace,
            "replicas": response.spec.replicas,
        },
    }


scale_deployment_tool = StructuredTool.from_function(
    func=scale_deployment,
    name="scale_deployment",
    description="Scale a Kubernetes deployment to a specified number of replicas.",
    args_schema=ScaleDeploymentInputSchema,
)


@_handle_k8s_exceptions
def delete_deployment(namespace: str, deployment_name: str) -> Dict[str, Any]:
    """Deletes a Kubernetes deployment from the specified namespace."""
    if deployment_name.strip() in ("*", "all", "ALL"):
        return {
            "status": "error",
            "message": (
                "Wildcard deletion is not supported. "
                "List deployments first with list_all_deployments or list_deployments_in_namespace, "
                "then provide explicit deployment names."
            ),
        }
    apps_v1 = get_apps_v1_api()
    apps_v1.delete_namespaced_deployment(name=deployment_name, namespace=namespace)
    return {"status": "success", "data": {"name": deployment_name, "namespace": namespace, "deleted": True}}


delete_deployment_tool = StructuredTool.from_function(
    func=delete_deployment,
    name="delete_deployment",
    description="Delete a Kubernetes deployment from a namespace.",
    args_schema=DeploymentInputSchema,
)


@_handle_k8s_exceptions
def rollout_undo_deployment(namespace: str, deployment_name: str, revision: int = 0) -> Dict[str, Any]:
    """Rolls back a Kubernetes deployment to a previous revision.

    Mirrors `kubectl rollout undo deployment/<name>` by finding the target
    ReplicaSet (previous revision by default) and patching the deployment's
    pod template spec to match it.
    """
    apps_v1 = get_apps_v1_api()

    # Read the current deployment to get its label selector and current revision
    deployment = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
    annotations = deployment.metadata.annotations or {}
    current_revision = int(annotations.get("deployment.kubernetes.io/revision", "0"))

    if current_revision == 0:
        return {
            "status": "error",
            "message": f"Deployment '{deployment_name}' has no revision history.",
        }

    # Determine the target revision
    target_revision = revision if revision > 0 else current_revision - 1
    if target_revision < 1:
        return {
            "status": "error",
            "message": f"Cannot roll back: current revision is {current_revision}, no earlier revision available.",
        }

    # List ReplicaSets owned by this deployment using its match labels
    selector = deployment.spec.selector.match_labels
    label_selector = ",".join(f"{k}={v}" for k, v in selector.items())
    rs_list = apps_v1.list_namespaced_replica_set(namespace=namespace, label_selector=label_selector)

    # Find the RS with the target revision annotation
    target_rs = None
    for rs in rs_list.items:
        rs_annotations = rs.metadata.annotations or {}
        rs_revision = int(rs_annotations.get("deployment.kubernetes.io/revision", "-1"))
        if rs_revision == target_revision:
            target_rs = rs
            break

    if target_rs is None:
        return {
            "status": "error",
            "message": f"Could not find ReplicaSet for revision {target_revision} of deployment '{deployment_name}'.",
        }

    # Patch the deployment's pod template to match the target RS
    patch_body = {
        "spec": {
            "template": target_rs.spec.template.to_dict()
        }
    }
    apps_v1.patch_namespaced_deployment(name=deployment_name, namespace=namespace, body=patch_body)

    return {
        "status": "success",
        "data": {
            "deployment": deployment_name,
            "namespace": namespace,
            "rolled_back_from_revision": current_revision,
            "rolled_back_to_revision": target_revision,
        },
    }


rollout_undo_deployment_tool = StructuredTool.from_function(
    func=rollout_undo_deployment,
    name="rollout_undo_deployment",
    description="Roll back a Kubernetes deployment to its previous revision (or a specific revision). Equivalent to `kubectl rollout undo deployment/<name>`.",
    args_schema=RolloutUndoInputSchema,
)


# ===============================================================================
#                        ROLLOUT RESTART DEPLOYMENT
# ===============================================================================

@_handle_k8s_exceptions
def rollout_restart_deployment(namespace: str, deployment_name: str) -> Dict[str, Any]:
    """Triggers a rolling restart of a Deployment by patching the pod template annotation.

    Equivalent to: kubectl rollout restart deployment/<name>
    Forces all pods to be replaced in a rolling fashion without downtime.
    Use after changing a ConfigMap or Secret that the deployment depends on.
    """
    import datetime
    apps_v1 = get_apps_v1_api()
    patch_body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": datetime.datetime.utcnow().isoformat() + "Z"
                    }
                }
            }
        }
    }
    apps_v1.patch_namespaced_deployment(name=deployment_name, namespace=namespace, body=patch_body)
    return {
        "status": "success",
        "data": {
            "deployment": deployment_name,
            "namespace": namespace,
            "message": f"Rollout restart triggered for deployment '{deployment_name}' in namespace '{namespace}'.",
        },
    }


rollout_restart_deployment_tool = StructuredTool.from_function(
    func=rollout_restart_deployment,
    name="rollout_restart_deployment",
    description="Trigger a rolling restart of a Deployment (equivalent to kubectl rollout restart). Use after patching a ConfigMap or Secret that the deployment reads.",
    args_schema=DeploymentInputSchema,
)


# ===============================================================================
#                         DEPLOYMENT ROLLOUT STATUS
# ===============================================================================

@_handle_k8s_exceptions
def get_deployment_rollout_status(namespace: str, deployment_name: str) -> Dict[str, Any]:
    """Returns the rollout status of a Deployment: desired, updated, available, and ready replicas.

    Equivalent to `kubectl rollout status deployment/<name>`. Indicates whether
    the rollout is complete (all pods updated and available) or still in progress.
    Falls back to a cluster-wide search when the deployment is not found in the
    specified namespace.
    """
    from kubernetes.client.exceptions import ApiException as _ApiException
    apps_v1 = get_apps_v1_api()
    try:
        d = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
    except _ApiException as exc:
        if exc.status != 404:
            raise
        # Cross-namespace fallback: search cluster-wide by name.
        try:
            all_deployments = apps_v1.list_deployment_for_all_namespaces(
                field_selector=f"metadata.name={deployment_name}",
                timeout_seconds=10,
            )
        except _ApiException as fallback_exc:
            if fallback_exc.status == 403:
                return {
                    "status": "error",
                    "message": (
                        f"Deployment '{deployment_name}' not found in namespace '{namespace}'. "
                        "Cross-namespace search requires cluster-wide list permission (403 Forbidden)."
                    ),
                    "error_type": "PermissionDenied",
                }
            raise
        matches = all_deployments.items
        if not matches:
            return {
                "status": "error",
                "message": f"Deployment '{deployment_name}' not found in namespace '{namespace}' or any other namespace.",
                "error_type": "NotFound",
            }
        if len(matches) > 1:
            found_ns = [m.metadata.namespace for m in matches]
            return {
                "status": "error",
                "message": (
                    f"Deployment '{deployment_name}' not found in namespace '{namespace}'. "
                    f"Found in multiple namespaces: {found_ns}. Please specify the correct namespace."
                ),
                "error_type": "AmbiguousNamespace",
            }
        d = matches[0]
        resolved_ns = d.metadata.namespace
        logger.info(
            "tool:namespace_fallback original_ns=%s resolved_ns=%s deployment=%s",
            namespace, resolved_ns, deployment_name,
        )
        namespace = resolved_ns  # use resolved namespace for the response
    spec = d.spec
    status = d.status
    desired = spec.replicas or 0
    updated = status.updated_replicas or 0
    available = status.available_replicas or 0
    ready = status.ready_replicas or 0
    unavailable = status.unavailable_replicas or 0

    complete = (updated == desired and available == desired and ready == desired)
    data: Dict[str, Any] = {
        "deployment": deployment_name,
        "namespace": namespace,
        "rollout_complete": complete,
        "desired_replicas": desired,
        "updated_replicas": updated,
        "available_replicas": available,
        "ready_replicas": ready,
        "unavailable_replicas": unavailable,
        "summary": (
            f"Rollout complete: {desired}/{desired} replicas available."
            if complete
            else f"Rollout in progress: {available}/{desired} replicas available ({updated} updated, {unavailable} unavailable)."
        ),
    }
    # Surface namespace resolution so callers know we searched across namespaces.
    if d.metadata.namespace != namespace:
        data["namespace_resolved"] = d.metadata.namespace
    return {"status": "success", "data": data}


get_deployment_rollout_status_tool = StructuredTool.from_function(
    func=get_deployment_rollout_status,
    name="get_deployment_rollout_status",
    description="Check whether a Deployment rollout is complete. Returns desired, updated, available, and ready replica counts. Use after updating an image or command to verify the fix worked.",
    args_schema=DeploymentInputSchema,
)


# ===============================================================================
#                       UPDATE DEPLOYMENT IMAGE / COMMAND
# ===============================================================================

class UpdateDeploymentImageInputSchema(BaseModel):
    """Schema for updating a deployment's container image."""
    namespace: str = Field(description="The Kubernetes namespace where the deployment is located.")
    deployment_name: str = Field(description="The name of the deployment to update.")
    new_image: str = Field(description="The new container image, e.g. nginx:alpine or nginx:1.25.")
    container_name: str = Field(default="", description="Name of the specific container to update. If empty, updates the first container.")


@_handle_k8s_exceptions
def update_deployment_image(namespace: str, deployment_name: str, new_image: str, container_name: str = "") -> Dict[str, Any]:
    """Updates the container image of a Kubernetes Deployment in-place.

    Equivalent to: kubectl set image deployment/<name> <container>=<image>
    """
    apps_v1 = get_apps_v1_api()
    deployment = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
    containers = deployment.spec.template.spec.containers

    if container_name:
        target = next((c for c in containers if c.name == container_name), None)
        if target is None:
            return {"status": "error", "message": f"Container '{container_name}' not found in deployment '{deployment_name}'."}
        target.image = new_image
    else:
        containers[0].image = new_image
        container_name = containers[0].name

    patch_body = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [{"name": c.name, "image": c.image} for c in containers]
                }
            }
        }
    }
    apps_v1.patch_namespaced_deployment(name=deployment_name, namespace=namespace, body=patch_body)
    return {
        "status": "success",
        "data": {
            "deployment": deployment_name,
            "namespace": namespace,
            "container": container_name,
            "new_image": new_image,
        },
    }


update_deployment_image_tool = StructuredTool.from_function(
    func=update_deployment_image,
    name="update_deployment_image",
    description="Update the container image of a Kubernetes Deployment. Use this to fix ImagePullBackOff errors by switching to a valid image tag, or to upgrade a workload.",
    args_schema=UpdateDeploymentImageInputSchema,
)


class UpdateDeploymentCommandInputSchema(BaseModel):
    """Schema for updating a deployment's container command."""
    namespace: str = Field(description="The Kubernetes namespace where the deployment is located.")
    deployment_name: str = Field(description="The name of the deployment to update.")
    command: List[str] = Field(description="The new container command as a list, e.g. ['sh', '-c', 'while true; do echo hi; sleep 30; done'].")
    container_name: str = Field(default="", description="Name of the specific container to update. If empty, updates the first container.")


@_handle_k8s_exceptions
def update_deployment_command(namespace: str, deployment_name: str, command: List[str], container_name: str = "") -> Dict[str, Any]:
    """Updates the container command (entrypoint) of a Kubernetes Deployment.

    Equivalent to patching spec.template.spec.containers[].command.
    Use this to fix CrashLoopBackOff caused by a bad command.

    If the container already has a non-null command, returns a confirmation_required
    response — call force_update_deployment_command to proceed after user confirms.
    """
    apps_v1 = get_apps_v1_api()
    deployment = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
    containers = deployment.spec.template.spec.containers

    if container_name:
        target = next((c for c in containers if c.name == container_name), None)
        if target is None:
            return {"status": "error", "message": f"Container '{container_name}' not found in deployment '{deployment_name}'."}
        resolved_name = container_name
    else:
        target = containers[0]
        resolved_name = target.name

    # B4 safety guard: if the container already has a non-null command, require
    # explicit user confirmation before overwriting to prevent silent entrypoint corruption.
    if target.command:
        return {
            "status": "confirmation_required",
            "message": (
                f"⚠️ Deployment '{deployment_name}' container '{resolved_name}' already has a command: "
                f"{target.command}. "
                f"Overwriting it will replace the container entrypoint. "
                f"Show this to the user and ask them to confirm before proceeding. "
                f"If the user confirms, call force_update_deployment_command with the same arguments."
            ),
            "existing_command": target.command,
            "proposed_command": command,
        }

    patch_body = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": resolved_name,
                            "command": command,
                            "args": [],
                        }
                    ]
                }
            }
        }
    }
    apps_v1.patch_namespaced_deployment(name=deployment_name, namespace=namespace, body=patch_body)
    return {
        "status": "success",
        "data": {
            "deployment": deployment_name,
            "namespace": namespace,
            "container": resolved_name,
            "new_command": command,
        },
    }


update_deployment_command_tool = StructuredTool.from_function(
    func=update_deployment_command,
    name="update_deployment_command",
    description=(
        "Update the container command of a Kubernetes Deployment. "
        "Use this to fix CrashLoopBackOff by replacing the failing command. "
        "If the container already has an existing command, this tool returns confirmation_required "
        "and you MUST show the existing command to the user and ask them to confirm before calling "
        "force_update_deployment_command to proceed."
    ),
    args_schema=UpdateDeploymentCommandInputSchema,
)


# Confirmed-overwrite tool — only call this after the user has explicitly confirmed
# that they want to overwrite the existing entrypoint shown by update_deployment_command.
# Never invoke this tool without first calling update_deployment_command and getting
# user confirmation. This tool skips the existing-command guard intentionally.
@_handle_k8s_exceptions
def force_update_deployment_command(namespace: str, deployment_name: str, command: List[str], container_name: str = "") -> Dict[str, Any]:
    """Force-updates the container command of a Kubernetes Deployment, overwriting any existing command.

    Only call this after update_deployment_command returned confirmation_required AND the user
    has explicitly confirmed they want to overwrite the existing entrypoint.
    """
    apps_v1 = get_apps_v1_api()
    deployment = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
    containers = deployment.spec.template.spec.containers

    if container_name:
        target = next((c for c in containers if c.name == container_name), None)
        if target is None:
            return {"status": "error", "message": f"Container '{container_name}' not found in deployment '{deployment_name}'."}
        resolved_name = container_name
    else:
        target = containers[0]
        resolved_name = target.name

    previous_command = target.command

    patch_body = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": resolved_name,
                            "command": command,
                            "args": [],
                        }
                    ]
                }
            }
        }
    }
    apps_v1.patch_namespaced_deployment(name=deployment_name, namespace=namespace, body=patch_body)
    return {
        "status": "success",
        "data": {
            "deployment": deployment_name,
            "namespace": namespace,
            "container": resolved_name,
            "previous_command": previous_command,
            "new_command": command,
        },
    }


force_update_deployment_command_tool = StructuredTool.from_function(
    func=force_update_deployment_command,
    name="force_update_deployment_command",
    description=(
        "Force-update the container command of a Kubernetes Deployment, overwriting any existing command. "
        "ONLY call this after update_deployment_command returned confirmation_required AND the user has "
        "explicitly confirmed they want to overwrite the existing entrypoint. "
        "Never call this as the first action — always call update_deployment_command first."
    ),
    args_schema=UpdateDeploymentCommandInputSchema,
)


class PatchDeploymentResourcesInputSchema(BaseModel):
    """Schema for patching resource requests/limits on a running deployment."""
    namespace: str = Field(description="The Kubernetes namespace where the deployment is located.")
    deployment_name: str = Field(description="The name of the deployment to patch.")
    container_name: str = Field(default="", description="Name of the specific container to patch. If empty, patches the first container.")
    cpu_request: Optional[str] = Field(default=None, description="New CPU request, e.g. '500m' or '2'. Leave unset to keep current value.")
    memory_request: Optional[str] = Field(default=None, description="New memory request, e.g. '1Gi' or '512Mi'. Leave unset to keep current value.")
    cpu_limit: Optional[str] = Field(default=None, description="New CPU limit, e.g. '1' or '2000m'. Leave unset to keep current value.")
    memory_limit: Optional[str] = Field(default=None, description="New memory limit, e.g. '2Gi'. Leave unset to keep current value.")


@_handle_k8s_exceptions
def patch_deployment_resources(
    namespace: str,
    deployment_name: str,
    container_name: str = "",
    cpu_request: Optional[str] = None,
    memory_request: Optional[str] = None,
    cpu_limit: Optional[str] = None,
    memory_limit: Optional[str] = None,
) -> Dict[str, Any]:
    """Patches CPU/memory requests and limits on a running deployment's container.

    Equivalent to patching spec.template.spec.containers[].resources.
    Use this to fix Pending pods caused by excessive resource requests, or to right-size workloads.
    """
    apps_v1 = get_apps_v1_api()
    deployment = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
    containers = deployment.spec.template.spec.containers

    if container_name:
        target = next((c for c in containers if c.name == container_name), None)
        if target is None:
            return {"status": "error", "message": f"Container '{container_name}' not found in deployment '{deployment_name}'."}
        resolved_name = container_name
    else:
        target = containers[0]
        resolved_name = target.name

    requests: Dict[str, str] = {}
    limits: Dict[str, str] = {}
    if cpu_request:
        requests["cpu"] = cpu_request
    if memory_request:
        requests["memory"] = memory_request
    if cpu_limit:
        limits["cpu"] = cpu_limit
    if memory_limit:
        limits["memory"] = memory_limit

    resources: Dict[str, Any] = {}
    if requests:
        resources["requests"] = requests
    if limits:
        resources["limits"] = limits

    if not resources:
        return {"status": "error", "message": "No resource values provided — specify at least one of cpu_request, memory_request, cpu_limit, memory_limit."}

    patch_body = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [{"name": resolved_name, "resources": resources}]
                }
            }
        }
    }
    apps_v1.patch_namespaced_deployment(name=deployment_name, namespace=namespace, body=patch_body)
    return {
        "status": "success",
        "data": {
            "deployment": deployment_name,
            "namespace": namespace,
            "container": resolved_name,
            "patched_resources": resources,
        },
    }


patch_deployment_resources_tool = StructuredTool.from_function(
    func=patch_deployment_resources,
    name="patch_deployment_resources",
    description=(
        "Patch CPU/memory requests and limits on a running deployment's container. "
        "Use this to fix Pending pods caused by excessive resource requests (e.g. reduce 64 CPU to 500m), "
        "or to right-size workloads. Triggers a rolling update."
    ),
    args_schema=PatchDeploymentResourcesInputSchema,
)


# ===============================================================================
#                           TOOL COLLECTION
# ===============================================================================

# Export all deployment tools for easy import
# Note: kubernetes_connectivity_check is intentionally excluded here — it is a
# cluster-level diagnostic that already lives in namespace_tools (→ AdvancedOps).
deployment_tools = [
    list_all_deployments_tool,
    list_deployments_in_namespace_tool,
    describe_kubernetes_deployment_tool,
    create_kubernetes_deployment_tool,
    check_deployments_missing_resource_limits_tool,
    check_deployments_without_affinity_tool,
    get_recent_deployment_changelog_tool,
    scale_deployment_tool,
    delete_deployment_tool,
    rollout_undo_deployment_tool,
    update_deployment_image_tool,
    update_deployment_command_tool,
    force_update_deployment_command_tool,
    patch_deployment_resources_tool,
    get_deployment_rollout_status_tool,
    rollout_restart_deployment_tool,
]



