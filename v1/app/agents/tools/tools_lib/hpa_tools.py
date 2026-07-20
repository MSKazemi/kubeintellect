from typing import Dict, Any, Optional

from kubernetes import client
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.agents.tools.tools_lib._base import (
    get_autoscaling_v2_api,
    _handle_k8s_exceptions,
    NoArgumentsInputSchema,
    NamespaceInputSchema,
)


# ===============================================================================
#                               INPUT SCHEMAS
# ===============================================================================

class HPAInputSchema(BaseModel):
    """Schema for tools that require an HPA name and namespace."""
    namespace: str = Field(description="The Kubernetes namespace where the HPA is located.")
    hpa_name: str = Field(description="The name of the HorizontalPodAutoscaler.")


class CreateHPAInputSchema(BaseModel):
    """Schema for creating a HorizontalPodAutoscaler."""
    namespace: str = Field(description="The Kubernetes namespace where the HPA will be created.")
    hpa_name: str = Field(description="The name for the new HorizontalPodAutoscaler.")
    deployment_name: str = Field(description="The name of the Deployment this HPA will target.")
    min_replicas: int = Field(default=1, ge=1, description="Minimum number of replicas (default: 1).")
    max_replicas: int = Field(ge=1, description="Maximum number of replicas.")
    target_cpu_utilization_percent: int = Field(
        default=70, ge=1, le=100,
        description="Target average CPU utilization across pods as a percentage (default: 70)."
    )


class PatchHPAReplicasInputSchema(BaseModel):
    """Schema for updating min/max replicas on an existing HPA."""
    namespace: str = Field(description="The Kubernetes namespace where the HPA is located.")
    hpa_name: str = Field(description="The name of the HorizontalPodAutoscaler to update.")
    min_replicas: Optional[int] = Field(default=None, ge=1, description="New minimum replicas. Omit to leave unchanged.")
    max_replicas: Optional[int] = Field(default=None, ge=1, description="New maximum replicas. Omit to leave unchanged.")


# ===============================================================================
#                                  HPA TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def list_hpas(namespace: str) -> Dict[str, Any]:
    """Lists all HorizontalPodAutoscalers in a namespace."""
    autoscaling_v2 = get_autoscaling_v2_api()
    hpa_list = autoscaling_v2.list_namespaced_horizontal_pod_autoscaler(namespace=namespace)
    result = [
        {
            "name": hpa.metadata.name,
            "namespace": hpa.metadata.namespace,
            "target": hpa.spec.scale_target_ref.name,
            "min_replicas": hpa.spec.min_replicas,
            "max_replicas": hpa.spec.max_replicas,
            "current_replicas": hpa.status.current_replicas,
            "desired_replicas": hpa.status.desired_replicas,
        }
        for hpa in hpa_list.items
    ]
    return {"status": "success", "data": result}


list_hpas_tool = StructuredTool.from_function(
    func=list_hpas,
    name="list_hpas",
    description="List all HorizontalPodAutoscalers (HPAs) in a namespace, including their target, min/max replicas, and current state.",
    args_schema=NamespaceInputSchema,
)


@_handle_k8s_exceptions
def list_all_hpas() -> Dict[str, Any]:
    """Lists all HorizontalPodAutoscalers across all namespaces."""
    autoscaling_v2 = get_autoscaling_v2_api()
    hpa_list = autoscaling_v2.list_horizontal_pod_autoscaler_for_all_namespaces()
    result = [
        {
            "name": hpa.metadata.name,
            "namespace": hpa.metadata.namespace,
            "target": hpa.spec.scale_target_ref.name,
            "min_replicas": hpa.spec.min_replicas,
            "max_replicas": hpa.spec.max_replicas,
            "current_replicas": hpa.status.current_replicas,
            "desired_replicas": hpa.status.desired_replicas,
        }
        for hpa in hpa_list.items
    ]
    return {"status": "success", "data": result}


list_all_hpas_tool = StructuredTool.from_function(
    func=list_all_hpas,
    name="list_all_hpas",
    description="List all HorizontalPodAutoscalers (HPAs) across every namespace in the cluster.",
    args_schema=NoArgumentsInputSchema,
)


@_handle_k8s_exceptions
def describe_hpa(namespace: str, hpa_name: str) -> Dict[str, Any]:
    """Describes a specific HorizontalPodAutoscaler in detail."""
    autoscaling_v2 = get_autoscaling_v2_api()
    hpa = autoscaling_v2.read_namespaced_horizontal_pod_autoscaler(name=hpa_name, namespace=namespace)

    metrics = []
    if hpa.spec.metrics:
        for m in hpa.spec.metrics:
            if m.type == "Resource" and m.resource:
                target = m.resource.target
                metrics.append({
                    "type": "Resource",
                    "resource": m.resource.name,
                    "target_type": target.type,
                    "target_value": getattr(target, "average_utilization", None) or getattr(target, "average_value", None),
                })

    current_metrics = []
    if hpa.status.current_metrics:
        for m in hpa.status.current_metrics:
            if m.type == "Resource" and m.resource:
                current_metrics.append({
                    "type": "Resource",
                    "resource": m.resource.name,
                    "current_average_utilization": getattr(m.resource.current, "average_utilization", None),
                    "current_average_value": str(getattr(m.resource.current, "average_value", None) or ""),
                })

    conditions = []
    if hpa.status.conditions:
        for c in hpa.status.conditions:
            conditions.append({
                "type": c.type,
                "status": c.status,
                "reason": c.reason,
                "message": c.message,
            })

    result = {
        "name": hpa.metadata.name,
        "namespace": hpa.metadata.namespace,
        "target": {
            "kind": hpa.spec.scale_target_ref.kind,
            "name": hpa.spec.scale_target_ref.name,
        },
        "min_replicas": hpa.spec.min_replicas,
        "max_replicas": hpa.spec.max_replicas,
        "current_replicas": hpa.status.current_replicas,
        "desired_replicas": hpa.status.desired_replicas,
        "metrics": metrics,
        "current_metrics": current_metrics,
        "conditions": conditions,
        "last_scale_time": hpa.status.last_scale_time.isoformat() if hpa.status.last_scale_time else None,
    }
    return {"status": "success", "data": result}


describe_hpa_tool = StructuredTool.from_function(
    func=describe_hpa,
    name="describe_hpa",
    description="Show detailed information about a HorizontalPodAutoscaler including metrics, conditions, current and desired replica counts.",
    args_schema=HPAInputSchema,
)


@_handle_k8s_exceptions
def create_hpa(
    namespace: str,
    hpa_name: str,
    deployment_name: str,
    min_replicas: int = 1,
    max_replicas: int = 10,
    target_cpu_utilization_percent: int = 70,
) -> Dict[str, Any]:
    """Creates a HorizontalPodAutoscaler targeting a Deployment with CPU utilization metric."""
    autoscaling_v2 = get_autoscaling_v2_api()

    hpa_body = client.V2HorizontalPodAutoscaler(
        api_version="autoscaling/v2",
        kind="HorizontalPodAutoscaler",
        metadata=client.V1ObjectMeta(name=hpa_name, namespace=namespace),
        spec=client.V2HorizontalPodAutoscalerSpec(
            scale_target_ref=client.V2CrossVersionObjectReference(
                api_version="apps/v1",
                kind="Deployment",
                name=deployment_name,
            ),
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            metrics=[
                client.V2MetricSpec(
                    type="Resource",
                    resource=client.V2ResourceMetricSource(
                        name="cpu",
                        target=client.V2MetricTarget(
                            type="Utilization",
                            average_utilization=target_cpu_utilization_percent,
                        ),
                    ),
                )
            ],
        ),
    )

    response = autoscaling_v2.create_namespaced_horizontal_pod_autoscaler(
        namespace=namespace, body=hpa_body
    )
    return {
        "status": "success",
        "data": {
            "name": response.metadata.name,
            "namespace": response.metadata.namespace,
            "target": deployment_name,
            "min_replicas": min_replicas,
            "max_replicas": max_replicas,
            "target_cpu_utilization_percent": target_cpu_utilization_percent,
        },
    }


create_hpa_tool = StructuredTool.from_function(
    func=create_hpa,
    name="create_hpa",
    description="Create a HorizontalPodAutoscaler (HPA) for a Deployment with CPU utilization-based autoscaling.",
    args_schema=CreateHPAInputSchema,
)


@_handle_k8s_exceptions
def delete_hpa(namespace: str, hpa_name: str) -> Dict[str, Any]:
    """Deletes a HorizontalPodAutoscaler from a namespace."""
    autoscaling_v2 = get_autoscaling_v2_api()
    autoscaling_v2.delete_namespaced_horizontal_pod_autoscaler(name=hpa_name, namespace=namespace)
    return {"status": "success", "data": {"name": hpa_name, "namespace": namespace, "deleted": True}}


delete_hpa_tool = StructuredTool.from_function(
    func=delete_hpa,
    name="delete_hpa",
    description="Delete a HorizontalPodAutoscaler from a namespace.",
    args_schema=HPAInputSchema,
)


@_handle_k8s_exceptions
def patch_hpa_replicas(
    namespace: str,
    hpa_name: str,
    min_replicas: Optional[int] = None,
    max_replicas: Optional[int] = None,
) -> Dict[str, Any]:
    """Updates the min and/or max replicas of an existing HPA."""
    autoscaling_v2 = get_autoscaling_v2_api()

    patch_body: Dict[str, Any] = {"spec": {}}
    if min_replicas is not None:
        patch_body["spec"]["minReplicas"] = min_replicas
    if max_replicas is not None:
        patch_body["spec"]["maxReplicas"] = max_replicas

    if not patch_body["spec"]:
        return {"status": "error", "message": "No changes requested: provide min_replicas and/or max_replicas."}

    response = autoscaling_v2.patch_namespaced_horizontal_pod_autoscaler(
        name=hpa_name, namespace=namespace, body=patch_body
    )
    return {
        "status": "success",
        "data": {
            "name": response.metadata.name,
            "namespace": response.metadata.namespace,
            "min_replicas": response.spec.min_replicas,
            "max_replicas": response.spec.max_replicas,
        },
    }


patch_hpa_replicas_tool = StructuredTool.from_function(
    func=patch_hpa_replicas,
    name="patch_hpa_replicas",
    description="Update the minimum and/or maximum replica count of an existing HorizontalPodAutoscaler.",
    args_schema=PatchHPAReplicasInputSchema,
)


# ===============================================================================
#                           TOOL COLLECTION
# ===============================================================================

hpa_tools = [
    list_hpas_tool,
    list_all_hpas_tool,
    describe_hpa_tool,
    create_hpa_tool,
    delete_hpa_tool,
    patch_hpa_replicas_tool,
]
