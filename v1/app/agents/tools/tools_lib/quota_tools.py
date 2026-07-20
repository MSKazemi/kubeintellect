"""
ResourceQuota and LimitRange static tools.

Covers create, list, describe, and delete for ResourceQuota and LimitRange
resources using the CoreV1 API.
"""

from typing import Dict, Any, Optional

from kubernetes import client
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.agents.tools.tools_lib._base import (
    get_core_v1_api,
    _handle_k8s_exceptions,
    NamespaceInputSchema,
    calculate_age,
)
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")


# ===============================================================================
#                           INPUT SCHEMAS
# ===============================================================================

class CreateResourceQuotaInput(BaseModel):
    namespace: str = Field(description="Namespace in which to create the ResourceQuota.")
    name: str = Field(description="Name for the ResourceQuota.")
    hard_limits: Dict[str, str] = Field(
        description=(
            "Hard resource limits as a dict. Examples: "
            "{'requests.cpu': '4', 'limits.cpu': '8', "
            "'requests.memory': '4Gi', 'limits.memory': '8Gi', 'pods': '20', "
            "'persistentvolumeclaims': '10', 'services': '10'}."
        ),
    )


class ResourceQuotaNameInput(BaseModel):
    namespace: str = Field(description="Namespace of the ResourceQuota.")
    name: str = Field(description="Name of the ResourceQuota.")


class CreateLimitRangeInput(BaseModel):
    namespace: str = Field(description="Namespace in which to create the LimitRange.")
    name: str = Field(description="Name for the LimitRange.")
    default_cpu_request: Optional[str] = Field(
        default=None, description="Default CPU request per container, e.g. '100m'."
    )
    default_cpu_limit: Optional[str] = Field(
        default=None, description="Default CPU limit per container, e.g. '500m'."
    )
    default_memory_request: Optional[str] = Field(
        default=None, description="Default memory request per container, e.g. '128Mi'."
    )
    default_memory_limit: Optional[str] = Field(
        default=None, description="Default memory limit per container, e.g. '512Mi'."
    )
    max_cpu: Optional[str] = Field(
        default=None, description="Maximum CPU allowed per container, e.g. '2'."
    )
    max_memory: Optional[str] = Field(
        default=None, description="Maximum memory allowed per container, e.g. '1Gi'."
    )


class LimitRangeNameInput(BaseModel):
    namespace: str = Field(description="Namespace of the LimitRange.")
    name: str = Field(description="Name of the LimitRange.")


# ===============================================================================
#                        RESOURCE QUOTA TOOL FUNCTIONS
# ===============================================================================

@_handle_k8s_exceptions
def create_resource_quota(
    namespace: str,
    name: str,
    hard_limits: Dict[str, str],
) -> Dict[str, Any]:
    """Creates (or replaces) a ResourceQuota in a namespace."""
    core_v1 = get_core_v1_api()

    quota_body = client.V1ResourceQuota(
        api_version="v1",
        kind="ResourceQuota",
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        spec=client.V1ResourceQuotaSpec(hard=hard_limits),
    )

    try:
        result = core_v1.create_namespaced_resource_quota(
            namespace=namespace, body=quota_body
        )
        action = "created"
    except client.exceptions.ApiException as e:
        if e.status == 409:
            result = core_v1.replace_namespaced_resource_quota(
                name=name, namespace=namespace, body=quota_body
            )
            action = "replaced"
        else:
            raise

    return {
        "status": "success",
        "action": action,
        "resource_quota": {
            "name": result.metadata.name,
            "namespace": result.metadata.namespace,
            "hard": hard_limits,
        },
    }


@_handle_k8s_exceptions
def list_resource_quotas(namespace: str) -> Dict[str, Any]:
    """Lists all ResourceQuotas in a namespace including usage vs limits."""
    core_v1 = get_core_v1_api()
    result = core_v1.list_namespaced_resource_quota(namespace=namespace, timeout_seconds=10)

    quotas = []
    for rq in result.items:
        hard = rq.status.hard or {} if rq.status else {}
        used = rq.status.used or {} if rq.status else {}
        quotas.append({
            "name": rq.metadata.name,
            "namespace": rq.metadata.namespace,
            "hard": hard,
            "used": used,
            "age": calculate_age(rq.metadata.creation_timestamp),
        })

    return {
        "status": "success",
        "namespace": namespace,
        "quota_count": len(quotas),
        "resource_quotas": quotas,
    }


@_handle_k8s_exceptions
def describe_resource_quota(namespace: str, name: str) -> Dict[str, Any]:
    """Describes a specific ResourceQuota showing hard limits and current usage."""
    core_v1 = get_core_v1_api()
    rq = core_v1.read_namespaced_resource_quota(name=name, namespace=namespace)

    hard = rq.status.hard or {} if rq.status else {}
    used = rq.status.used or {} if rq.status else {}

    # Build comparison rows
    usage_summary = {}
    for resource, limit in hard.items():
        usage_summary[resource] = {
            "hard": limit,
            "used": used.get(resource, "0"),
        }

    return {
        "status": "success",
        "resource_quota": {
            "name": rq.metadata.name,
            "namespace": rq.metadata.namespace,
            "age": calculate_age(rq.metadata.creation_timestamp),
            "usage": usage_summary,
        },
    }


@_handle_k8s_exceptions
def delete_resource_quota(namespace: str, name: str) -> Dict[str, Any]:
    """Deletes a ResourceQuota from a namespace."""
    core_v1 = get_core_v1_api()
    core_v1.delete_namespaced_resource_quota(name=name, namespace=namespace)
    return {
        "status": "success",
        "message": f"ResourceQuota '{name}' deleted from namespace '{namespace}'.",
    }


# ===============================================================================
#                        LIMIT RANGE TOOL FUNCTIONS
# ===============================================================================

@_handle_k8s_exceptions
def create_limit_range(
    namespace: str,
    name: str,
    default_cpu_request: Optional[str] = None,
    default_cpu_limit: Optional[str] = None,
    default_memory_request: Optional[str] = None,
    default_memory_limit: Optional[str] = None,
    max_cpu: Optional[str] = None,
    max_memory: Optional[str] = None,
) -> Dict[str, Any]:
    """Creates (or replaces) a LimitRange in a namespace."""
    core_v1 = get_core_v1_api()

    default = {}
    default_request = {}
    max_limits = {}

    if default_cpu_limit:
        default["cpu"] = default_cpu_limit
    if default_memory_limit:
        default["memory"] = default_memory_limit
    if default_cpu_request:
        default_request["cpu"] = default_cpu_request
    if default_memory_request:
        default_request["memory"] = default_memory_request
    if max_cpu:
        max_limits["cpu"] = max_cpu
    if max_memory:
        max_limits["memory"] = max_memory

    limit_item = client.V1LimitRangeItem(
        type="Container",
        default=default if default else None,
        default_request=default_request if default_request else None,
        max=max_limits if max_limits else None,
    )

    lr_body = client.V1LimitRange(
        api_version="v1",
        kind="LimitRange",
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        spec=client.V1LimitRangeSpec(limits=[limit_item]),
    )

    try:
        result = core_v1.create_namespaced_limit_range(
            namespace=namespace, body=lr_body
        )
        action = "created"
    except client.exceptions.ApiException as e:
        if e.status == 409:
            result = core_v1.replace_namespaced_limit_range(
                name=name, namespace=namespace, body=lr_body
            )
            action = "replaced"
        else:
            raise

    return {
        "status": "success",
        "action": action,
        "limit_range": {
            "name": result.metadata.name,
            "namespace": result.metadata.namespace,
            "default": default,
            "default_request": default_request,
            "max": max_limits,
        },
    }


@_handle_k8s_exceptions
def list_limit_ranges(namespace: str) -> Dict[str, Any]:
    """Lists all LimitRanges in a namespace."""
    core_v1 = get_core_v1_api()
    result = core_v1.list_namespaced_limit_range(namespace=namespace, timeout_seconds=10)

    limit_ranges = []
    for lr in result.items:
        limits = []
        if lr.spec and lr.spec.limits:
            for item in lr.spec.limits:
                limits.append({
                    "type": item.type,
                    "default": item.default or {},
                    "default_request": item.default_request or {},
                    "max": item.max or {},
                    "min": item.min or {},
                })
        limit_ranges.append({
            "name": lr.metadata.name,
            "namespace": lr.metadata.namespace,
            "limits": limits,
            "age": calculate_age(lr.metadata.creation_timestamp),
        })

    return {
        "status": "success",
        "namespace": namespace,
        "limit_range_count": len(limit_ranges),
        "limit_ranges": limit_ranges,
    }


@_handle_k8s_exceptions
def delete_limit_range(namespace: str, name: str) -> Dict[str, Any]:
    """Deletes a LimitRange from a namespace."""
    core_v1 = get_core_v1_api()
    core_v1.delete_namespaced_limit_range(name=name, namespace=namespace)
    return {
        "status": "success",
        "message": f"LimitRange '{name}' deleted from namespace '{namespace}'.",
    }


# ===============================================================================
#                           TOOL INSTANCES
# ===============================================================================

create_resource_quota_tool = StructuredTool.from_function(
    func=create_resource_quota,
    name="create_resource_quota",
    description=(
        "Create (or replace) a ResourceQuota in a namespace. "
        "Pass hard_limits as a dict such as: "
        "{'requests.cpu': '4', 'limits.cpu': '8', 'requests.memory': '4Gi', "
        "'limits.memory': '8Gi', 'pods': '20'}."
    ),
    args_schema=CreateResourceQuotaInput,
)

list_resource_quotas_tool = StructuredTool.from_function(
    func=list_resource_quotas,
    name="list_resource_quotas",
    description="List all ResourceQuotas in a namespace with current usage vs hard limits.",
    args_schema=NamespaceInputSchema,
)

describe_resource_quota_tool = StructuredTool.from_function(
    func=describe_resource_quota,
    name="describe_resource_quota",
    description="Describe a specific ResourceQuota showing per-resource hard limit and current usage.",
    args_schema=ResourceQuotaNameInput,
)

delete_resource_quota_tool = StructuredTool.from_function(
    func=delete_resource_quota,
    name="delete_resource_quota",
    description="Delete a ResourceQuota from a namespace.",
    args_schema=ResourceQuotaNameInput,
)

create_limit_range_tool = StructuredTool.from_function(
    func=create_limit_range,
    name="create_limit_range",
    description=(
        "Create (or replace) a LimitRange in a namespace. "
        "Sets per-container defaults for CPU and memory requests/limits, "
        "and optionally enforces maximum values."
    ),
    args_schema=CreateLimitRangeInput,
)

list_limit_ranges_tool = StructuredTool.from_function(
    func=list_limit_ranges,
    name="list_limit_ranges",
    description="List all LimitRanges in a namespace and their per-container default request/limit values.",
    args_schema=NamespaceInputSchema,
)

delete_limit_range_tool = StructuredTool.from_function(
    func=delete_limit_range,
    name="delete_limit_range",
    description="Delete a LimitRange from a namespace.",
    args_schema=LimitRangeNameInput,
)

quota_tools = [
    create_resource_quota_tool,
    list_resource_quotas_tool,
    describe_resource_quota_tool,
    delete_resource_quota_tool,
    create_limit_range_tool,
    list_limit_ranges_tool,
    delete_limit_range_tool,
]
