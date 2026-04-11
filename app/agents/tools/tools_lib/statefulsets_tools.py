from typing import List, Dict, Any, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.agents.tools.tools_lib._base import (
    get_core_v1_api,
    get_apps_v1_api,
    _handle_k8s_exceptions,
    NoArgumentsInputSchema,
    NamespaceInputSchema,
)
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")


# ===============================================================================
#                               INPUT SCHEMAS
# ===============================================================================

class StatefulSetInputSchema(BaseModel):
    """Schema for tools that require statefulset name and namespace."""
    namespace: str = Field(description="The Kubernetes namespace where the statefulset is located.")
    statefulset_name: str = Field(description="The name of the Kubernetes statefulset.")


class ScaleStatefulSetInputSchema(BaseModel):
    """Schema for scaling a StatefulSet."""
    namespace: str = Field(description="The Kubernetes namespace where the statefulset is located.")
    statefulset_name: str = Field(description="The name of the Kubernetes statefulset.")
    replicas: int = Field(description="The desired number of replicas.")


class VolumeClaimTemplate(BaseModel):
    """Defines a PVC template for StatefulSet pods."""
    claim_name: str = Field(description="Name for the volume claim (also used as volume mount name).")
    storage: str = Field(default="1Gi", description="Storage size, e.g. '1Gi', '10Gi'.")
    storage_class: Optional[str] = Field(default=None, description="StorageClass name. Omit to use cluster default.")
    access_modes: List[str] = Field(default=["ReadWriteOnce"], description="Access modes, e.g. ['ReadWriteOnce'].")
    mount_path: str = Field(description="Path inside the container where the volume is mounted.")


class CreateStatefulSetInputSchema(BaseModel):
    """Schema for creating a StatefulSet."""
    namespace: str = Field(description="The Kubernetes namespace where the StatefulSet will be created.")
    name: str = Field(description="The name of the StatefulSet.")
    container_image: str = Field(description="The container image to use for the StatefulSet's pods.")
    replicas: int = Field(default=1, description="The number of replicas for the StatefulSet.")
    container_port: Optional[int] = Field(default=None, description="Optional container port to expose.")
    labels: Optional[Dict[str, str]] = Field(default=None, description="Additional labels to apply to the StatefulSet and pod template.")
    env_vars: Optional[Dict[str, str]] = Field(default=None, description="Environment variables as key-value pairs.")
    env_from_secret: Optional[str] = Field(default=None, description="Name of a Secret to inject all keys as env vars.")
    env_from_configmap: Optional[str] = Field(default=None, description="Name of a ConfigMap to inject all keys as env vars.")
    cpu_request: Optional[str] = Field(default=None, description="CPU request, e.g. '100m'.")
    memory_request: Optional[str] = Field(default=None, description="Memory request, e.g. '128Mi'.")
    cpu_limit: Optional[str] = Field(default=None, description="CPU limit, e.g. '500m'.")
    memory_limit: Optional[str] = Field(default=None, description="Memory limit, e.g. '256Mi'.")
    volume_claim_templates: Optional[List[VolumeClaimTemplate]] = Field(default=None, description="PVC templates for persistent storage. Each pod gets its own PVC.")


# ===============================================================================
#                            STATEFULSET TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def list_statefulsets() -> Dict[str, Any]:
    """Lists all StatefulSets across all namespaces."""
    apps_v1 = get_apps_v1_api()
    statefulsets = apps_v1.list_stateful_set_for_all_namespaces(timeout_seconds=10)
    
    result = [
        {
            "name": ss.metadata.name,
            "namespace": ss.metadata.namespace,
            "replicas": ss.spec.replicas,
            "ready_replicas": ss.status.ready_replicas or 0,
            "labels": ss.metadata.labels or {},
            "creation_timestamp": ss.metadata.creation_timestamp.isoformat() if ss.metadata.creation_timestamp else None
        }
        for ss in statefulsets.items
    ]
    return {"status": "success", "data": result}


list_statefulsets_tool = StructuredTool.from_function(
    func=list_statefulsets,
    name="list_kubernetes_statefulsets",
    description="Lists all StatefulSets in a Kubernetes cluster across all namespaces. Returns name, namespace, replicas, ready replicas, labels, and creation timestamp for each StatefulSet.",
    args_schema=NoArgumentsInputSchema
)


@_handle_k8s_exceptions
def list_statefulsets_in_namespace(namespace: str) -> Dict[str, Any]:
    """Lists all StatefulSets in a specific namespace."""
    apps_v1 = get_apps_v1_api()
    statefulsets = apps_v1.list_namespaced_stateful_set(namespace=namespace, timeout_seconds=10)
    
    result = [
        {
            "name": ss.metadata.name,
            "namespace": ss.metadata.namespace,
            "replicas": ss.spec.replicas,
            "ready_replicas": ss.status.ready_replicas or 0,
            "labels": ss.metadata.labels or {},
            "creation_timestamp": ss.metadata.creation_timestamp.isoformat() if ss.metadata.creation_timestamp else None
        }
        for ss in statefulsets.items
    ]
    return {"status": "success", "data": result}


list_statefulsets_in_namespace_tool = StructuredTool.from_function(
    func=list_statefulsets_in_namespace,
    name="list_statefulsets_in_namespace",
    description="Lists all StatefulSets in a specific Kubernetes namespace.",
    args_schema=NamespaceInputSchema
)


@_handle_k8s_exceptions
def describe_statefulset(namespace: str, statefulset_name: str) -> Dict[str, Any]:
    """Retrieves detailed information about a specific StatefulSet."""
    apps_v1 = get_apps_v1_api()
    statefulset = apps_v1.read_namespaced_stateful_set(name=statefulset_name, namespace=namespace)
    
    # Extract container information
    containers = []
    for container in statefulset.spec.template.spec.containers:
        containers.append({
            "name": container.name,
            "image": container.image,
            "resources": container.resources.to_dict() if container.resources else {},
            "ports": [{"name": port.name, "port": port.container_port} for port in container.ports] if container.ports else []
        })
    
    # Extract volume claims information
    volume_claims = []
    if statefulset.spec.volume_claim_templates:
        for claim in statefulset.spec.volume_claim_templates:
            volume_claims.append({
                "name": claim.metadata.name,
                "storage_class": claim.spec.storage_class_name,
                "access_modes": claim.spec.access_modes,
                "storage": str(claim.spec.resources.requests.get("storage", "Unknown")) if claim.spec.resources and claim.spec.resources.requests else "Unknown"
            })
    
    statefulset_description = {
        "name": statefulset.metadata.name,
        "namespace": statefulset.metadata.namespace,
        "labels": statefulset.metadata.labels or {},
        "annotations": statefulset.metadata.annotations or {},
        "replicas": statefulset.spec.replicas,
        "ready_replicas": statefulset.status.ready_replicas or 0,
        "current_replicas": statefulset.status.current_replicas or 0,
        "updated_replicas": statefulset.status.updated_replicas or 0,
        "service_name": statefulset.spec.service_name,
        "selector": statefulset.spec.selector.match_labels,
        "containers": containers,
        "volume_claim_templates": volume_claims,
        "creation_timestamp": statefulset.metadata.creation_timestamp.isoformat() if statefulset.metadata.creation_timestamp else None
    }
    
    return {"status": "success", "data": statefulset_description}


describe_statefulset_tool = StructuredTool.from_function(
    func=describe_statefulset,
    name="describe_kubernetes_statefulset",
    description="Retrieves detailed information about a specific Kubernetes StatefulSet including containers, volumes, and status.",
    args_schema=StatefulSetInputSchema
)


# ===============================================================================
#                            DAEMONSET TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def list_all_daemonsets() -> Dict[str, Any]:
    """Lists all DaemonSets across all namespaces."""
    apps_v1 = get_apps_v1_api()
    daemonsets = apps_v1.list_daemon_set_for_all_namespaces(timeout_seconds=10)
    
    result = [
        {
            "name": ds.metadata.name,
            "namespace": ds.metadata.namespace,
            "desired_number_scheduled": ds.status.desired_number_scheduled or 0,
            "current_number_scheduled": ds.status.current_number_scheduled or 0,
            "number_ready": ds.status.number_ready or 0,
            "labels": ds.metadata.labels or {},
            "creation_timestamp": ds.metadata.creation_timestamp.isoformat() if ds.metadata.creation_timestamp else None
        }
        for ds in daemonsets.items
    ]
    return {"status": "success", "data": result}


list_all_daemonsets_tool = StructuredTool.from_function(
    func=list_all_daemonsets,
    name="list_all_kubernetes_daemonsets",
    description="Lists all DaemonSets in a Kubernetes cluster across all namespaces with their scheduling and readiness status.",
    args_schema=NoArgumentsInputSchema
)


@_handle_k8s_exceptions
def list_daemonsets_in_namespace(namespace: str) -> Dict[str, Any]:
    """Lists all DaemonSets in a specific namespace."""
    apps_v1 = get_apps_v1_api()
    daemonsets = apps_v1.list_namespaced_daemon_set(namespace=namespace, timeout_seconds=10)
    
    result = [
        {
            "name": ds.metadata.name,
            "namespace": ds.metadata.namespace,
            "desired_number_scheduled": ds.status.desired_number_scheduled or 0,
            "current_number_scheduled": ds.status.current_number_scheduled or 0,
            "number_ready": ds.status.number_ready or 0,
            "labels": ds.metadata.labels or {},
            "creation_timestamp": ds.metadata.creation_timestamp.isoformat() if ds.metadata.creation_timestamp else None
        }
        for ds in daemonsets.items
    ]
    return {"status": "success", "data": result}


list_daemonsets_in_namespace_tool = StructuredTool.from_function(
    func=list_daemonsets_in_namespace,
    name="list_daemonsets_in_namespace",
    description="Lists all DaemonSets in a specific Kubernetes namespace with their scheduling and readiness status.",
    args_schema=NamespaceInputSchema
)


@_handle_k8s_exceptions
def create_statefulset(
    namespace: str,
    name: str,
    container_image: str,
    replicas: int = 1,
    container_port: Optional[int] = None,
    labels: Optional[Dict[str, str]] = None,
    env_vars: Optional[Dict[str, str]] = None,
    env_from_secret: Optional[str] = None,
    env_from_configmap: Optional[str] = None,
    cpu_request: Optional[str] = None,
    memory_request: Optional[str] = None,
    cpu_limit: Optional[str] = None,
    memory_limit: Optional[str] = None,
    volume_claim_templates: Optional[List] = None,
) -> Dict[str, Any]:
    """Creates a Kubernetes StatefulSet in the specified namespace."""
    from kubernetes import client as k8s_client
    apps_v1 = get_apps_v1_api()

    base_labels = {"app": name}
    if labels:
        base_labels.update(labels)

    ports = [k8s_client.V1ContainerPort(container_port=container_port)] if container_port else []

    # Environment variables
    env = []
    if env_vars:
        for k, v in env_vars.items():
            env.append(k8s_client.V1EnvVar(name=k, value=v))

    # envFrom sources
    env_from = []
    if env_from_secret:
        env_from.append(k8s_client.V1EnvFromSource(
            secret_ref=k8s_client.V1SecretEnvSource(name=env_from_secret)
        ))
    if env_from_configmap:
        env_from.append(k8s_client.V1EnvFromSource(
            config_map_ref=k8s_client.V1ConfigMapEnvSource(name=env_from_configmap)
        ))

    # Resource requests/limits
    resources = None
    requests = {k: v for k, v in [("cpu", cpu_request), ("memory", memory_request)] if v}
    limits = {k: v for k, v in [("cpu", cpu_limit), ("memory", memory_limit)] if v}
    if requests or limits:
        resources = k8s_client.V1ResourceRequirements(
            requests=requests or None,
            limits=limits or None,
        )

    # Volume mounts derived from claim templates
    volume_mounts = []
    k8s_volume_claim_templates = []
    if volume_claim_templates:
        for vct in volume_claim_templates:
            # vct may be a VolumeClaimTemplate pydantic model or a dict
            if hasattr(vct, "claim_name"):
                claim_name = vct.claim_name
                storage = vct.storage
                storage_class = vct.storage_class
                access_modes = vct.access_modes
                mount_path = vct.mount_path
            else:
                claim_name = vct["claim_name"]
                storage = vct.get("storage", "1Gi")
                storage_class = vct.get("storage_class")
                access_modes = vct.get("access_modes", ["ReadWriteOnce"])
                mount_path = vct["mount_path"]

            volume_mounts.append(k8s_client.V1VolumeMount(
                name=claim_name,
                mount_path=mount_path,
            ))
            k8s_volume_claim_templates.append(k8s_client.V1PersistentVolumeClaim(
                metadata=k8s_client.V1ObjectMeta(name=claim_name),
                spec=k8s_client.V1PersistentVolumeClaimSpec(
                    access_modes=access_modes,
                    storage_class_name=storage_class,
                    resources=k8s_client.V1ResourceRequirements(requests={"storage": storage}),
                ),
            ))

    container = k8s_client.V1Container(
        name=name,
        image=container_image,
        ports=ports,
        env=env or None,
        env_from=env_from or None,
        resources=resources,
        volume_mounts=volume_mounts or None,
    )
    pod_template = k8s_client.V1PodTemplateSpec(
        metadata=k8s_client.V1ObjectMeta(labels=base_labels),
        spec=k8s_client.V1PodSpec(containers=[container]),
    )
    statefulset_spec = k8s_client.V1StatefulSetSpec(
        replicas=replicas,
        selector=k8s_client.V1LabelSelector(match_labels={"app": name}),
        template=pod_template,
        service_name=name,
        volume_claim_templates=k8s_volume_claim_templates or None,
    )
    statefulset = k8s_client.V1StatefulSet(
        api_version="apps/v1",
        kind="StatefulSet",
        metadata=k8s_client.V1ObjectMeta(name=name, namespace=namespace, labels=base_labels),
        spec=statefulset_spec,
    )

    response = apps_v1.create_namespaced_stateful_set(namespace=namespace, body=statefulset)
    return {"status": "success", "data": {"name": response.metadata.name, "namespace": response.metadata.namespace}}


create_statefulset_tool = StructuredTool.from_function(
    func=create_statefulset,
    name="create_statefulset",
    description=(
        "Creates a Kubernetes StatefulSet. Supports: replicas, container_port, labels, "
        "env_vars (dict of key-value env vars), env_from_secret (inject all keys from a Secret), "
        "env_from_configmap (inject all keys from a ConfigMap), cpu_request, memory_request, cpu_limit, memory_limit, "
        "and volume_claim_templates (list of {claim_name, storage, storage_class, access_modes, mount_path} for persistent storage)."
    ),
    args_schema=CreateStatefulSetInputSchema,
)


@_handle_k8s_exceptions
def get_statefulset_pods(namespace: str, statefulset_name: str) -> Dict[str, Any]:
    """Gets pods belonging to a StatefulSet with their current phase and readiness."""
    apps_v1 = get_apps_v1_api()
    core_v1 = get_core_v1_api()

    statefulset = apps_v1.read_namespaced_stateful_set(name=statefulset_name, namespace=namespace)
    selector = statefulset.spec.selector.match_labels or {}
    label_selector = ",".join(f"{k}={v}" for k, v in selector.items())

    pods = core_v1.list_namespaced_pod(namespace=namespace, label_selector=label_selector, timeout_seconds=10)

    pod_list = []
    for pod in pods.items:
        container_statuses = []
        if pod.status.container_statuses:
            for cs in pod.status.container_statuses:
                container_statuses.append({
                    "name": cs.name,
                    "ready": cs.ready,
                    "restart_count": cs.restart_count,
                    "image": cs.image,
                })
        pod_list.append({
            "name": pod.metadata.name,
            "phase": pod.status.phase,
            "ready": all(cs["ready"] for cs in container_statuses) if container_statuses else False,
            "node": pod.spec.node_name,
            "container_statuses": container_statuses,
        })

    return {
        "status": "success",
        "data": {
            "statefulset_name": statefulset_name,
            "namespace": namespace,
            "desired_replicas": statefulset.spec.replicas,
            "ready_replicas": statefulset.status.ready_replicas or 0,
            "current_replicas": statefulset.status.current_replicas or 0,
            "pods": pod_list,
        },
    }


get_statefulset_pods_tool = StructuredTool.from_function(
    func=get_statefulset_pods,
    name="get_statefulset_pods",
    description=(
        "Returns all pods belonging to a specific Kubernetes StatefulSet, with each pod's phase "
        "(Running/Pending/etc.), readiness, restart count, and container statuses. "
        "Use this to check StatefulSet pod status, whether pods are ready, and replica counts."
    ),
    args_schema=StatefulSetInputSchema,
)


@_handle_k8s_exceptions
def scale_statefulset(namespace: str, statefulset_name: str, replicas: int) -> Dict[str, Any]:
    """Scales a Kubernetes StatefulSet to the specified number of replicas."""
    apps_v1 = get_apps_v1_api()
    patch = {"spec": {"replicas": replicas}}
    apps_v1.patch_namespaced_stateful_set_scale(name=statefulset_name, namespace=namespace, body=patch)
    return {
        "status": "success",
        "data": {"name": statefulset_name, "namespace": namespace, "replicas": replicas},
    }


scale_statefulset_tool = StructuredTool.from_function(
    func=scale_statefulset,
    name="scale_statefulset",
    description="Scales a Kubernetes StatefulSet to the specified number of replicas.",
    args_schema=ScaleStatefulSetInputSchema,
)


@_handle_k8s_exceptions
def delete_statefulset(namespace: str, statefulset_name: str) -> Dict[str, Any]:
    """Deletes a Kubernetes StatefulSet from the specified namespace."""
    apps_v1 = get_apps_v1_api()
    apps_v1.delete_namespaced_stateful_set(name=statefulset_name, namespace=namespace)
    return {
        "status": "success",
        "data": {"name": statefulset_name, "namespace": namespace, "action": "deleted"},
    }


delete_statefulset_tool = StructuredTool.from_function(
    func=delete_statefulset,
    name="delete_statefulset",
    description="Deletes a Kubernetes StatefulSet from the specified namespace.",
    args_schema=StatefulSetInputSchema,
)


# ===============================================================================
#                           TOOL COLLECTION
# ===============================================================================

# Export all statefulset and daemonset tools for easy import
statefulset_daemonset_tools = [
    list_statefulsets_tool,
    list_statefulsets_in_namespace_tool,
    describe_statefulset_tool,
    get_statefulset_pods_tool,
    scale_statefulset_tool,
    create_statefulset_tool,
    delete_statefulset_tool,
    list_all_daemonsets_tool,
    list_daemonsets_in_namespace_tool,
]

