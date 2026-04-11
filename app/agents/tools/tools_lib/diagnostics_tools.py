"""
Structured diagnostics tools.

All tools are read-only. All return structured data (JSON-serialised Pydantic
models) that other agents or the supervisor can consume programmatically.
Raw API output is never returned — every tool parses and shapes its response.

Tools:
  - top_nodes         : Node CPU/memory usage from metrics-server + capacity
  - top_pods          : Per-pod CPU/memory + container restart counts
  - events_watch      : Filterable, sorted, structured cluster event list
  - describe_resource : Generic structured describe for any common resource kind

Error handling: all four tools return typed error dicts on resource-not-found
or cluster-unreachable — never raise unhandled exceptions to the LLM.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from kubernetes.client.exceptions import ApiException
from langchain_core.tools import StructuredTool
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, Field

from app.agents.tools.tools_lib._base import (
    get_apps_v1_api,
    get_core_v1_api,
    get_custom_objects_api,
    get_batch_v1_api,
    _handle_k8s_exceptions,
    _parse_cpu_to_millicores,
    _parse_memory_to_mib,
)
from app.core.config import settings
from app.utils.logger_config import setup_logging
from app.utils.metrics import tool_calls_total

logger = setup_logging(app_name="kubeintellect")
_tracer = trace.get_tracer("kubeintellect.tools")


# ── Shared helpers ────────────────────────────────────────────────────────────

def _pct(used: float, total: float) -> float:
    """Return used/total as a rounded percentage, or 0.0 if total is zero."""
    if total <= 0:
        return 0.0
    return round(used / total * 100, 1)


def _ts(dt: Any) -> Optional[str]:
    """Convert a datetime or None to ISO-8601 string."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def _safe_get(obj: Any, *attrs: str, default: Any = None) -> Any:
    """Safely chain attribute access, returning default if any step is None."""
    curr = obj
    for attr in attrs:
        if curr is None:
            return default
        curr = getattr(curr, attr, None)
    return curr if curr is not None else default


def _format_resources(resources: Any) -> Dict[str, Any]:
    """Format a container ResourceRequirements into a readable dict."""
    if resources is None:
        return {"requests": None, "limits": None}
    return {
        "requests": dict(resources.requests) if resources.requests else None,
        "limits": dict(resources.limits) if resources.limits else None,
    }


def _is_metrics_server_available() -> bool:
    try:
        get_custom_objects_api().list_cluster_custom_object(
            group="metrics.k8s.io", version="v1beta1", plural="nodes"
        )
        return True
    except Exception:
        return False


_METRICS_SERVER_UNAVAILABLE = {
    "status": "error",
    "error_type": "metrics_server_unavailable",
    "message": (
        "metrics-server is not installed or not reachable. "
        "Install it with: kubectl apply -f "
        "https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml "
        "Alternatively, use query_prometheus for resource usage data."
    ),
}


# ── Pydantic output models ────────────────────────────────────────────────────

class NodeMetrics(BaseModel):
    name: str
    roles: List[str]
    status: str                         # Ready | NotReady | Unknown
    cpu_usage_millicores: float
    cpu_allocatable_millicores: float
    cpu_percent: float
    memory_usage_mib: float
    memory_allocatable_mib: float
    memory_percent: float
    kernel_version: Optional[str] = None
    os_image: Optional[str] = None
    container_runtime: Optional[str] = None


class ContainerMetrics(BaseModel):
    name: str
    cpu_millicores: float
    memory_mib: float


class PodMetrics(BaseModel):
    namespace: str
    name: str
    phase: str
    node: Optional[str]
    containers: List[ContainerMetrics]
    total_cpu_millicores: float
    total_memory_mib: float
    restart_count: int


class KubeEvent(BaseModel):
    type: str                  # Warning | Normal
    reason: str
    message: str
    namespace: str
    involved_object_kind: str
    involved_object_name: str
    involved_object_namespace: Optional[str]
    count: int
    first_time: Optional[str]
    last_time: Optional[str]
    source_component: Optional[str]
    source_host: Optional[str]


class ResourceCondition(BaseModel):
    type: str
    status: str
    reason: Optional[str]
    message: Optional[str]
    last_transition: Optional[str]


class DescribeResult(BaseModel):
    kind: str
    name: str
    namespace: str
    created_at: Optional[str]
    labels: Dict[str, str]
    annotations_count: int           # full annotations omitted to save tokens
    spec_summary: Dict[str, Any]
    status_summary: Dict[str, Any]
    conditions: List[ResourceCondition]
    recent_events: List[KubeEvent]
    anomalies: List[str]             # human-readable list of detected issues


# ── Top-level output models ───────────────────────────────────────────────────

class TopNodesOutput(BaseModel):
    status: str
    node_count: Optional[int] = None
    sort_by: Optional[str] = None
    nodes: Optional[List[Dict[str, Any]]] = None
    error_type: Optional[str] = None
    message: Optional[str] = None


class TopPodsOutput(BaseModel):
    status: str
    namespace: Optional[str] = None
    pod_count: Optional[int] = None
    sort_by: Optional[str] = None
    pods: Optional[List[Dict[str, Any]]] = None
    error_type: Optional[str] = None
    message: Optional[str] = None


class EventsWatchOutput(BaseModel):
    status: str
    namespace: Optional[str] = None
    filters: Optional[Dict[str, Any]] = None
    total_returned: Optional[int] = None
    warning_count: Optional[int] = None
    events: Optional[List[Dict[str, Any]]] = None
    error_type: Optional[str] = None
    message: Optional[str] = None


class DescribeResourceOutput(BaseModel):
    status: str
    data: Optional[Dict[str, Any]] = None
    anomaly_count: Optional[int] = None
    warning_event_count: Optional[int] = None
    error_type: Optional[str] = None
    message: Optional[str] = None


# ── top_nodes ─────────────────────────────────────────────────────────────────

class TopNodesInput(BaseModel):
    sort_by: str = Field(
        default="cpu_percent",
        description="Sort key: cpu_percent, memory_percent, cpu_usage_millicores, memory_usage_mib.",
    )


@_handle_k8s_exceptions
def top_nodes(sort_by: str = "cpu_percent") -> str:
    """Return structured CPU and memory usage for every node in the cluster.

    Combines metrics-server live usage data with node capacity from the
    Kubernetes API to compute usage percentages.

    Returns structured NodeMetrics objects — not raw text.
    Requires metrics-server to be installed.
    """
    with _tracer.start_as_current_span("top_nodes") as span:
        try:
            if not _is_metrics_server_available():
                output = TopNodesOutput(
                    status="error",
                    error_type="metrics_server_unavailable",
                    message=(
                        "metrics-server is not installed or not reachable. "
                        "Install it with: kubectl apply -f "
                        "https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml "
                        "Alternatively, use query_prometheus for resource usage data."
                    ),
                )
                span.set_status(StatusCode.ERROR, description=output.message)
                tool_calls_total.labels(tool="top_nodes", status="error").inc()
                return output.model_dump_json()

            custom_api = get_custom_objects_api()
            core_v1 = get_core_v1_api()

            raw_metrics = custom_api.list_cluster_custom_object(
                group="metrics.k8s.io", version="v1beta1", plural="nodes"
            )
            usage_by_node: Dict[str, Dict[str, float]] = {}
            for item in raw_metrics.get("items", []):
                node_name = item["metadata"]["name"]
                usage_by_node[node_name] = {
                    "cpu": _parse_cpu_to_millicores(item["usage"].get("cpu", "0")),
                    "memory": _parse_memory_to_mib(item["usage"].get("memory", "0")),
                }

            node_list = core_v1.list_node()
            results: List[Dict[str, Any]] = []

            for node in node_list.items:
                node_name = node.metadata.name
                roles = [
                    label.replace("node-role.kubernetes.io/", "")
                    for label in (node.metadata.labels or {})
                    if label.startswith("node-role.kubernetes.io/")
                ] or ["<none>"]
                allocatable = node.status.allocatable or {}
                cpu_alloc = _parse_cpu_to_millicores(allocatable.get("cpu", "0"))
                mem_alloc = _parse_memory_to_mib(allocatable.get("memory", "0"))
                usage = usage_by_node.get(node_name, {"cpu": 0.0, "memory": 0.0})
                node_status = "Unknown"
                for cond in (node.status.conditions or []):
                    if cond.type == "Ready":
                        node_status = "Ready" if cond.status == "True" else "NotReady"
                        break
                nm = NodeMetrics(
                    name=node_name, roles=roles, status=node_status,
                    cpu_usage_millicores=round(usage["cpu"], 1),
                    cpu_allocatable_millicores=round(cpu_alloc, 1),
                    cpu_percent=_pct(usage["cpu"], cpu_alloc),
                    memory_usage_mib=round(usage["memory"], 1),
                    memory_allocatable_mib=round(mem_alloc, 1),
                    memory_percent=_pct(usage["memory"], mem_alloc),
                    kernel_version=_safe_get(node.status, "node_info", "kernel_version"),
                    os_image=_safe_get(node.status, "node_info", "os_image"),
                    container_runtime=_safe_get(node.status, "node_info", "container_runtime_version"),
                )
                results.append(nm.model_dump())

            valid_sort_keys = {"cpu_percent", "memory_percent", "cpu_usage_millicores", "memory_usage_mib"}
            sk = sort_by if sort_by in valid_sort_keys else "cpu_percent"
            results.sort(key=lambda x: x.get(sk, 0), reverse=True)

            output = TopNodesOutput(status="success", node_count=len(results), sort_by=sk, nodes=results)
            span.set_status(StatusCode.OK)
            tool_calls_total.labels(tool="top_nodes", status="success").inc()
            return output.model_dump_json()
        except Exception as e:
            span.set_status(StatusCode.ERROR, description=str(e))
            tool_calls_total.labels(tool="top_nodes", status="error").inc()
            raise


top_nodes_tool = StructuredTool.from_function(
    func=top_nodes,
    name="top_nodes",
    description=(
        "Return structured CPU and memory usage for all cluster nodes. "
        "Combines metrics-server live data with node capacity to compute percentages. "
        "Returns NodeMetrics with: name, roles, status, cpu_usage_millicores, "
        "cpu_allocatable_millicores, cpu_percent, memory_usage_mib, "
        "memory_allocatable_mib, memory_percent. "
        "Requires metrics-server. Sort by cpu_percent (default), memory_percent, "
        "cpu_usage_millicores, or memory_usage_mib."
    ),
    args_schema=TopNodesInput,
)


# ── top_pods ──────────────────────────────────────────────────────────────────

class TopPodsInput(BaseModel):
    namespace: str = Field(
        default="",
        description=(
            "Namespace to query. "
            "Leave empty for all namespaces."
        ),
    )
    sort_by: str = Field(
        default="cpu_millicores",
        description="Sort key: cpu_millicores, memory_mib, restart_count.",
    )
    limit: int = Field(
        default=20,
        description="Maximum number of pods to return. Default 20.",
    )


@_handle_k8s_exceptions
def top_pods(
    namespace: str = "",
    sort_by: str = "cpu_millicores",
    limit: int = 20,
) -> str:
    """Return structured CPU and memory usage plus restart counts per pod.

    Requires metrics-server. Supports filtering by namespace and sorting
    by cpu_millicores (default), memory_mib, or restart_count.
    """
    with _tracer.start_as_current_span("top_pods") as span:
        span.set_attribute("k8s.namespace", namespace or "<all>")
        try:
            if not _is_metrics_server_available():
                output = TopPodsOutput(
                    status="error",
                    error_type="metrics_server_unavailable",
                    message=(
                        "metrics-server is not installed or not reachable. "
                        "Install it with: kubectl apply -f "
                        "https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml "
                        "Alternatively, use query_prometheus for resource usage data."
                    ),
                )
                span.set_status(StatusCode.ERROR, description=output.message)
                tool_calls_total.labels(tool="top_pods", status="error").inc()
                return output.model_dump_json()

            custom_api = get_custom_objects_api()
            core_v1 = get_core_v1_api()

            if namespace:
                raw_metrics = custom_api.list_namespaced_custom_object(
                    group="metrics.k8s.io", version="v1beta1",
                    namespace=namespace, plural="pods",
                )
            else:
                raw_metrics = custom_api.list_cluster_custom_object(
                    group="metrics.k8s.io", version="v1beta1", plural="pods"
                )

            metrics_lookup: Dict[tuple, Dict[str, Dict[str, float]]] = {}
            for item in raw_metrics.get("items", []):
                ns = item["metadata"]["namespace"]
                pod_name = item["metadata"]["name"]
                containers: Dict[str, Dict[str, float]] = {}
                for c in item.get("containers", []):
                    containers[c["name"]] = {
                        "cpu": _parse_cpu_to_millicores(c["usage"].get("cpu", "0")),
                        "memory": _parse_memory_to_mib(c["usage"].get("memory", "0")),
                    }
                metrics_lookup[(ns, pod_name)] = containers

            if namespace:
                pod_list = core_v1.list_namespaced_pod(namespace=namespace,
                                                        timeout_seconds=settings.K8S_API_TIMEOUT_SECONDS)
            else:
                pod_list = core_v1.list_pod_for_all_namespaces(timeout_seconds=settings.K8S_API_TIMEOUT_SECONDS)

            results: List[Dict[str, Any]] = []

            for pod in pod_list.items:
                pod_ns = pod.metadata.namespace
                pod_name = pod.metadata.name
                restart_count = 0
                if pod.status and pod.status.container_statuses:
                    for cs in pod.status.container_statuses:
                        restart_count += cs.restart_count or 0
                container_metrics_raw = metrics_lookup.get((pod_ns, pod_name), {})
                containers_list: List[Dict[str, Any]] = []
                total_cpu = 0.0
                total_mem = 0.0
                for c in (pod.spec.containers or []):
                    c_usage = container_metrics_raw.get(c.name, {"cpu": 0.0, "memory": 0.0})
                    c_cpu = round(c_usage["cpu"], 1)
                    c_mem = round(c_usage["memory"], 1)
                    total_cpu += c_cpu
                    total_mem += c_mem
                    containers_list.append(
                        ContainerMetrics(name=c.name, cpu_millicores=c_cpu, memory_mib=c_mem).model_dump()
                    )
                pm = PodMetrics(
                    namespace=pod_ns, name=pod_name,
                    phase=_safe_get(pod.status, "phase") or "Unknown",
                    node=_safe_get(pod.spec, "node_name"),
                    containers=containers_list,
                    total_cpu_millicores=round(total_cpu, 1),
                    total_memory_mib=round(total_mem, 1),
                    restart_count=restart_count,
                )
                results.append(pm.model_dump())

            valid_sort_keys = {"cpu_millicores": "total_cpu_millicores",
                               "memory_mib": "total_memory_mib",
                               "restart_count": "restart_count"}
            sk = valid_sort_keys.get(sort_by, "total_cpu_millicores")
            results.sort(key=lambda x: x.get(sk, 0), reverse=True)
            results = results[:limit]

            output = TopPodsOutput(
                status="success",
                namespace=namespace or "<all>",
                pod_count=len(results),
                sort_by=sort_by,
                pods=results,
            )
            span.set_status(StatusCode.OK)
            tool_calls_total.labels(tool="top_pods", status="success").inc()
            return output.model_dump_json()
        except Exception as e:
            span.set_status(StatusCode.ERROR, description=str(e))
            tool_calls_total.labels(tool="top_pods", status="error").inc()
            raise


top_pods_tool = StructuredTool.from_function(
    func=top_pods,
    name="top_pods",
    description=(
        "Return structured CPU and memory usage with restart counts for pods. "
        "Returns PodMetrics with: namespace, name, phase, node, "
        "containers (per-container CPU/memory), total_cpu_millicores, "
        "total_memory_mib, restart_count. "
        "Leave namespace empty for all namespaces. "
        "Sort by cpu_millicores (default), memory_mib, or restart_count. "
        "Requires metrics-server."
    ),
    args_schema=TopPodsInput,
)


# ── events_watch ──────────────────────────────────────────────────────────────

class EventsWatchInput(BaseModel):
    namespace: str = Field(
        default="",
        description="Namespace to query. Leave empty for all namespaces.",
    )
    resource_name: str = Field(
        default="",
        description=(
            "Filter by the name of the involved resource "
            "(e.g. a specific pod or deployment name). Leave empty for all resources."
        ),
    )
    reason: str = Field(
        default="",
        description=(
            "Filter by event reason "
            "(e.g. BackOff, OOMKilling, FailedScheduling, Unhealthy, Pulled, Created). "
            "Case-insensitive substring match. Leave empty for all reasons."
        ),
    )
    event_type: str = Field(
        default="",
        description=(
            "Filter by event type: Warning, Normal, or empty for both."
        ),
    )
    limit: int = Field(
        default=25,
        description="Maximum number of events to return, sorted newest-first. Default 25.",
    )


@_handle_k8s_exceptions
def events_watch(
    namespace: str = "",
    resource_name: str = "",
    reason: str = "",
    event_type: str = "",
    limit: int = 25,
) -> str:
    """Fetch, filter, and return structured cluster events sorted by timestamp descending.

    Returns structured KubeEvent objects — not raw kubectl describe output.
    """
    with _tracer.start_as_current_span("events_watch") as span:
        span.set_attribute("k8s.namespace", namespace or "<all>")
        try:
            core_v1 = get_core_v1_api()

            field_selector_parts: List[str] = []
            if resource_name:
                field_selector_parts.append(f"involvedObject.name={resource_name}")
            if event_type and event_type in ("Warning", "Normal"):
                field_selector_parts.append(f"type={event_type}")
            field_selector = ",".join(field_selector_parts) if field_selector_parts else None

            try:
                if namespace:
                    kwargs: Dict[str, Any] = {"namespace": namespace}
                    if field_selector:
                        kwargs["field_selector"] = field_selector
                    raw = core_v1.list_namespaced_event(**kwargs)
                else:
                    kwargs = {}
                    if field_selector:
                        kwargs["field_selector"] = field_selector
                    raw = core_v1.list_event_for_all_namespaces(**kwargs)
            except ApiException as e:
                if e.status == 404:
                    output = EventsWatchOutput(
                        status="error",
                        error_type="not_found",
                        message=(
                            "No events found"
                            + (f" for resource '{resource_name}'" if resource_name else "")
                            + (f" in namespace '{namespace}'" if namespace else "")
                            + "."
                        ),
                    )
                    span.set_status(StatusCode.ERROR, description=output.message)
                    tool_calls_total.labels(tool="events_watch", status="error").inc()
                    return output.model_dump_json()
                raise

            events: List[Any] = []
            for ev in raw.items:
                ev_reason = ev.reason or ""
                if reason and reason.lower() not in ev_reason.lower():
                    continue
                ts_dt = ev.last_timestamp or ev.event_time or ev.first_timestamp
                ke = KubeEvent(
                    type=ev.type or "Unknown",
                    reason=ev_reason,
                    message=(ev.message or "").strip(),
                    namespace=_safe_get(ev.metadata, "namespace") or "",
                    involved_object_kind=_safe_get(ev.involved_object, "kind") or "",
                    involved_object_name=_safe_get(ev.involved_object, "name") or "",
                    involved_object_namespace=_safe_get(ev.involved_object, "namespace"),
                    count=ev.count or 1,
                    first_time=_ts(ev.first_timestamp),
                    last_time=_ts(ts_dt),
                    source_component=_safe_get(ev.source, "component"),
                    source_host=_safe_get(ev.source, "host"),
                )
                events.append((ts_dt, ke.model_dump()))

            events.sort(key=lambda x: x[0] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            events = events[:limit]
            warning_count = sum(1 for _, e in events if e["type"] == "Warning")

            output = EventsWatchOutput(
                status="success",
                namespace=namespace or "<all>",
                filters={
                    "resource_name": resource_name or None,
                    "reason": reason or None,
                    "event_type": event_type or None,
                },
                total_returned=len(events),
                warning_count=warning_count,
                events=[e for _, e in events],
            )
            span.set_status(StatusCode.OK)
            tool_calls_total.labels(tool="events_watch", status="success").inc()
            return output.model_dump_json()
        except Exception as e:
            span.set_status(StatusCode.ERROR, description=str(e))
            tool_calls_total.labels(tool="events_watch", status="error").inc()
            raise


events_watch_tool = StructuredTool.from_function(
    func=events_watch,
    name="events_watch",
    description=(
        "Fetch structured Kubernetes events sorted newest-first. "
        "Filter by namespace, resource name (involvedObject.name), "
        "reason (BackOff, OOMKilling, FailedScheduling, Unhealthy, etc. — substring match), "
        "or event type (Warning/Normal). "
        "Returns KubeEvent with: type, reason, message, involved_object, count, timestamps, source. "
        "Use this instead of get_namespace_warning_events when you need cross-namespace "
        "queries or reason-based filtering."
    ),
    args_schema=EventsWatchInput,
)


# ── describe_resource ─────────────────────────────────────────────────────────

class DescribeResourceInput(BaseModel):
    namespace: str = Field(..., description="Kubernetes namespace.")
    kind: str = Field(
        ...,
        description=(
            "Resource kind: Pod, Deployment, StatefulSet, DaemonSet, Service, "
            "ConfigMap, Secret, Node, Job, PersistentVolumeClaim, or ReplicaSet."
        ),
    )
    name: str = Field(..., description="Resource name.")
    include_events: bool = Field(
        default=True,
        description="Include recent events for this resource. Default True.",
    )


# Spec extractors per kind ────────────────────────────────────────────────────

def _spec_deployment(obj: Any) -> Dict[str, Any]:
    spec = obj.spec
    return {
        "replicas": spec.replicas,
        "strategy": _safe_get(spec.strategy, "type") or "RollingUpdate",
        "selector": dict(spec.selector.match_labels) if spec.selector else {},
        "containers": [
            {
                "name": c.name,
                "image": c.image,
                "resources": _format_resources(c.resources),
                "env_count": len(c.env or []),
                "ports": [p.container_port for p in (c.ports or [])],
            }
            for c in (spec.template.spec.containers or [])
        ],
        "min_ready_seconds": spec.min_ready_seconds or 0,
        "revision_history_limit": spec.revision_history_limit or 10,
    }


def _status_deployment(obj: Any) -> Dict[str, Any]:
    st = obj.status
    return {
        "desired": obj.spec.replicas or 0,
        "updated": st.updated_replicas or 0,
        "ready": st.ready_replicas or 0,
        "available": st.available_replicas or 0,
        "unavailable": st.unavailable_replicas or 0,
        "observed_generation": st.observed_generation,
    }


def _spec_statefulset(obj: Any) -> Dict[str, Any]:
    spec = obj.spec
    return {
        "replicas": spec.replicas,
        "service_name": spec.service_name,
        "selector": dict(spec.selector.match_labels) if spec.selector else {},
        "update_strategy": _safe_get(spec.update_strategy, "type") or "RollingUpdate",
        "containers": [
            {"name": c.name, "image": c.image, "resources": _format_resources(c.resources)}
            for c in (spec.template.spec.containers or [])
        ],
        "volume_claim_templates": len(spec.volume_claim_templates or []),
    }


def _status_statefulset(obj: Any) -> Dict[str, Any]:
    st = obj.status
    return {
        "desired": obj.spec.replicas or 0,
        "ready": st.ready_replicas or 0,
        "current": st.current_replicas or 0,
        "updated": st.updated_replicas or 0,
        "current_revision": st.current_revision,
        "update_revision": st.update_revision,
    }


def _spec_daemonset(obj: Any) -> Dict[str, Any]:
    spec = obj.spec
    return {
        "selector": dict(spec.selector.match_labels) if spec.selector else {},
        "update_strategy": _safe_get(spec.update_strategy, "type") or "RollingUpdate",
        "containers": [
            {"name": c.name, "image": c.image, "resources": _format_resources(c.resources)}
            for c in (spec.template.spec.containers or [])
        ],
    }


def _status_daemonset(obj: Any) -> Dict[str, Any]:
    st = obj.status
    return {
        "desired": st.desired_number_scheduled or 0,
        "current": st.current_number_scheduled or 0,
        "ready": st.number_ready or 0,
        "updated": st.updated_number_scheduled or 0,
        "available": st.number_available or 0,
        "unavailable": st.number_unavailable or 0,
    }


def _spec_pod(obj: Any) -> Dict[str, Any]:
    spec = obj.spec
    return {
        "node": spec.node_name,
        "service_account": spec.service_account_name,
        "restart_policy": spec.restart_policy,
        "containers": [
            {
                "name": c.name,
                "image": c.image,
                "resources": _format_resources(c.resources),
                "ports": [p.container_port for p in (c.ports or [])],
                "env_count": len(c.env or []),
            }
            for c in (spec.containers or [])
        ],
        "volumes": [v.name for v in (spec.volumes or [])],
    }


def _status_pod(obj: Any) -> Dict[str, Any]:
    st = obj.status
    container_statuses = []
    for cs in (st.container_statuses or []):
        state_str = "unknown"
        state_detail = {}
        if cs.state:
            if cs.state.running:
                state_str = "running"
                state_detail = {"started_at": _ts(cs.state.running.started_at)}
            elif cs.state.waiting:
                state_str = "waiting"
                state_detail = {
                    "reason": cs.state.waiting.reason,
                    "message": cs.state.waiting.message,
                }
            elif cs.state.terminated:
                state_str = "terminated"
                state_detail = {
                    "reason": cs.state.terminated.reason,
                    "exit_code": cs.state.terminated.exit_code,
                }
        container_statuses.append({
            "name": cs.name,
            "ready": cs.ready,
            "restart_count": cs.restart_count or 0,
            "state": state_str,
            "state_detail": state_detail,
            "image": cs.image,
        })
    return {
        "phase": st.phase,
        "pod_ip": st.pod_ip,
        "host_ip": st.host_ip,
        "start_time": _ts(st.start_time),
        "container_statuses": container_statuses,
    }


def _spec_service(obj: Any) -> Dict[str, Any]:
    spec = obj.spec
    return {
        "type": spec.type,
        "selector": dict(spec.selector) if spec.selector else {},
        "cluster_ip": spec.cluster_ip,
        "ports": [
            {"name": p.name, "port": p.port, "target_port": str(p.target_port),
             "protocol": p.protocol, "node_port": p.node_port}
            for p in (spec.ports or [])
        ],
        "external_ips": spec.external_i_ps or [],
        "load_balancer_ip": spec.load_balancer_ip,
    }


def _spec_job(obj: Any) -> Dict[str, Any]:
    spec = obj.spec
    return {
        "completions": spec.completions,
        "parallelism": spec.parallelism,
        "backoff_limit": spec.backoff_limit,
        "containers": [
            {"name": c.name, "image": c.image}
            for c in (spec.template.spec.containers or [])
        ],
    }


def _status_job(obj: Any) -> Dict[str, Any]:
    st = obj.status
    return {
        "active": st.active or 0,
        "succeeded": st.succeeded or 0,
        "failed": st.failed or 0,
        "start_time": _ts(st.start_time),
        "completion_time": _ts(st.completion_time),
    }


def _spec_configmap(obj: Any) -> Dict[str, Any]:
    return {
        "key_count": len(obj.data or {}),
        "keys": sorted(obj.data.keys()) if obj.data else [],
        "binary_key_count": len(obj.binary_data or {}),
    }


def _spec_secret(obj: Any) -> Dict[str, Any]:
    return {
        "type": obj.type,
        "key_count": len(obj.data or {}),
        "keys": sorted(obj.data.keys()) if obj.data else [],
        "note": "Secret values are not shown for security reasons.",
    }


def _spec_pvc(obj: Any) -> Dict[str, Any]:
    spec = obj.spec
    return {
        "storage_class": spec.storage_class_name,
        "access_modes": spec.access_modes or [],
        "requested_storage": (spec.resources.requests or {}).get("storage") if spec.resources else None,
        "volume_mode": spec.volume_mode,
    }


def _status_pvc(obj: Any) -> Dict[str, Any]:
    st = obj.status
    return {
        "phase": st.phase,
        "capacity": dict(st.capacity) if st.capacity else {},
        "access_modes": st.access_modes or [],
        "bound_volume": obj.spec.volume_name,
    }


# Kind dispatch tables ────────────────────────────────────────────────────────

def _get_resource_for_describe(kind: str, name: str, namespace: str) -> Any:
    """Read a resource by kind/name/namespace. Raises ValueError for unsupported kinds."""
    apps_v1 = get_apps_v1_api()
    core_v1 = get_core_v1_api()
    batch_v1 = get_batch_v1_api()

    k = kind.lower()
    dispatch = {
        "deployment":            lambda: apps_v1.read_namespaced_deployment(name, namespace),
        "statefulset":           lambda: apps_v1.read_namespaced_stateful_set(name, namespace),
        "daemonset":             lambda: apps_v1.read_namespaced_daemon_set(name, namespace),
        "replicaset":            lambda: apps_v1.read_namespaced_replica_set(name, namespace),
        "pod":                   lambda: core_v1.read_namespaced_pod(name, namespace),
        "service":               lambda: core_v1.read_namespaced_service(name, namespace),
        "configmap":             lambda: core_v1.read_namespaced_config_map(name, namespace),
        "secret":                lambda: core_v1.read_namespaced_secret(name, namespace),
        "persistentvolumeclaim": lambda: core_v1.read_namespaced_persistent_volume_claim(name, namespace),
        "job":                   lambda: batch_v1.read_namespaced_job(name, namespace),
    }
    if k not in dispatch:
        raise ValueError(
            f"Unsupported kind '{kind}' for describe_resource. "
            f"Supported: {sorted(dispatch.keys())}"
        )
    return dispatch[k]()


def _extract_conditions(obj: Any) -> List[Dict[str, Any]]:
    """Extract .status.conditions from any resource, normalised."""
    conditions = _safe_get(obj, "status", "conditions") or []
    return [
        ResourceCondition(
            type=c.type,
            status=c.status,
            reason=getattr(c, "reason", None),
            message=getattr(c, "message", None),
            last_transition=_ts(getattr(c, "last_transition_time", None)),
        ).model_dump()
        for c in conditions
    ]


def _detect_anomalies(kind: str, obj: Any) -> List[str]:
    """Return a list of human-readable anomaly strings for common issues."""
    issues: List[str] = []
    k = kind.lower()

    if k == "deployment":
        spec_replicas = obj.spec.replicas or 0
        avail = obj.status.available_replicas or 0
        ready = obj.status.ready_replicas or 0
        if avail < spec_replicas:
            issues.append(
                f"Only {avail}/{spec_replicas} replicas available — rollout may be stalled."
            )
        if ready == 0 and spec_replicas > 0:
            issues.append("Zero ready replicas — workload is completely down.")
        for cond in (obj.status.conditions or []):
            if cond.type == "Progressing" and cond.status == "False":
                issues.append(f"Deployment not progressing: {cond.reason} — {cond.message}")
            if cond.type == "Available" and cond.status == "False":
                issues.append(f"Deployment not available: {cond.reason} — {cond.message}")

    elif k == "statefulset":
        desired = obj.spec.replicas or 0
        ready = obj.status.ready_replicas or 0
        if ready < desired:
            issues.append(f"Only {ready}/{desired} StatefulSet pods ready.")

    elif k == "daemonset":
        desired = obj.status.desired_number_scheduled or 0
        ready = obj.status.number_ready or 0
        unavail = obj.status.number_unavailable or 0
        if unavail > 0:
            issues.append(f"{unavail}/{desired} DaemonSet pods unavailable.")
        if ready < desired:
            issues.append(f"Only {ready}/{desired} DaemonSet pods ready.")

    elif k == "pod":
        phase = _safe_get(obj.status, "phase") or ""
        if phase not in ("Running", "Succeeded"):
            issues.append(f"Pod phase is '{phase}' (expected Running or Succeeded).")
        for cs in (obj.status.container_statuses or []):
            if cs.restart_count and cs.restart_count >= 5:
                issues.append(
                    f"Container '{cs.name}' has restarted {cs.restart_count} times — "
                    "possible CrashLoopBackOff."
                )
            if cs.state and cs.state.waiting:
                reason = cs.state.waiting.reason or ""
                if reason in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
                              "OOMKilled", "Error", "CreateContainerConfigError"):
                    issues.append(
                        f"Container '{cs.name}' waiting: {reason} — "
                        f"{cs.state.waiting.message or ''}"
                    )
        if not obj.spec.node_name:
            issues.append("Pod is not scheduled to any node (Pending or failed scheduling).")

    elif k == "job":
        if (obj.status.failed or 0) > 0:
            issues.append(
                f"Job has {obj.status.failed} failed pod(s). "
                "Check events for BackoffLimitExceeded or pod errors."
            )
        if not obj.status.completion_time and (obj.status.active or 0) == 0:
            issues.append("Job is not active and has not completed — may be stalled.")

    elif k == "persistentvolumeclaim":
        phase = _safe_get(obj.status, "phase") or ""
        if phase != "Bound":
            issues.append(
                f"PVC phase is '{phase}' (expected Bound). "
                "Check StorageClass and available PVs."
            )

    return issues


def _fetch_resource_events(
    kind: str, name: str, namespace: str, limit: int = 5
) -> List[Dict[str, Any]]:
    """Fetch recent events for a specific resource."""
    core_v1 = get_core_v1_api()
    try:
        field_selector = f"involvedObject.name={name},involvedObject.kind={kind}"
        if namespace:
            raw = core_v1.list_namespaced_event(
                namespace=namespace, field_selector=field_selector
            )
        else:
            raw = core_v1.list_event_for_all_namespaces(field_selector=field_selector)

        events = []
        for ev in raw.items:
            ts_dt = ev.last_timestamp or ev.event_time or ev.first_timestamp
            ke = KubeEvent(
                type=ev.type or "Unknown",
                reason=ev.reason or "",
                message=(ev.message or "").strip(),
                namespace=_safe_get(ev.metadata, "namespace") or "",
                involved_object_kind=_safe_get(ev.involved_object, "kind") or "",
                involved_object_name=_safe_get(ev.involved_object, "name") or "",
                involved_object_namespace=_safe_get(ev.involved_object, "namespace"),
                count=ev.count or 1,
                first_time=_ts(ev.first_timestamp),
                last_time=_ts(ts_dt),
                source_component=_safe_get(ev.source, "component"),
                source_host=_safe_get(ev.source, "host"),
            )
            events.append((ts_dt, ke.model_dump()))

        events.sort(
            key=lambda x: x[0] or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return [e for _, e in events[:limit]]
    except Exception:
        return []


def _extract_spec_status(kind: str, obj: Any) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (spec_summary, status_summary) for the given kind."""
    k = kind.lower()
    spec_dispatch = {
        "deployment":            _spec_deployment,
        "statefulset":           _spec_statefulset,
        "daemonset":             _spec_daemonset,
        "pod":                   _spec_pod,
        "service":               _spec_service,
        "job":                   _spec_job,
        "configmap":             _spec_configmap,
        "secret":                _spec_secret,
        "persistentvolumeclaim": _spec_pvc,
    }
    status_dispatch = {
        "deployment":            _status_deployment,
        "statefulset":           _status_statefulset,
        "daemonset":             _status_daemonset,
        "pod":                   _status_pod,
        "job":                   _status_job,
        "persistentvolumeclaim": _status_pvc,
    }

    spec_fn = spec_dispatch.get(k, lambda o: {"note": f"No spec extractor for kind '{kind}'"})
    status_fn = status_dispatch.get(k, lambda o: {"note": f"No status extractor for kind '{kind}'"})

    try:
        spec_sum = spec_fn(obj)
    except Exception as e:
        spec_sum = {"error": f"Could not extract spec: {e}"}

    try:
        status_sum = status_fn(obj)
    except Exception as e:
        status_sum = {"error": f"Could not extract status: {e}"}

    return spec_sum, status_sum


@_handle_k8s_exceptions
def describe_resource(
    namespace: str,
    kind: str,
    name: str,
    include_events: bool = True,
) -> str:
    """Return a structured describe of any common namespaced Kubernetes resource.

    Parses the resource into sections: metadata summary, spec summary, status
    summary, conditions, recent events (optional), and detected anomalies.

    Does NOT return raw kubectl describe text — every field is extracted
    and normalised so the supervisor can reason over it programmatically.
    """
    with _tracer.start_as_current_span("describe_resource") as span:
        span.set_attribute("k8s.namespace", namespace)
        span.set_attribute("k8s.resource.kind", kind)
        span.set_attribute("k8s.resource.name", name)
        try:
            try:
                obj = _get_resource_for_describe(kind, name, namespace)
            except ValueError as e:
                output = DescribeResourceOutput(
                    status="error", error_type="unsupported_kind", message=str(e)
                )
                span.set_status(StatusCode.ERROR, description=str(e))
                tool_calls_total.labels(tool="describe_resource", status="error").inc()
                return output.model_dump_json()
            except ApiException as e:
                if e.status == 404:
                    output = DescribeResourceOutput(
                        status="error", error_type="not_found",
                        message=(
                            f"{kind} '{name}' not found in namespace '{namespace}'. "
                            "Check the name and namespace are correct."
                        ),
                    )
                elif e.status in (401, 403):
                    output = DescribeResourceOutput(
                        status="error", error_type="permission_denied",
                        message=(
                            f"Permission denied reading {kind} '{name}' "
                            f"(HTTP {e.status}). Check RBAC."
                        ),
                    )
                else:
                    output = DescribeResourceOutput(
                        status="error", error_type="api_error",
                        message=f"Kubernetes API error {e.status}: {e.reason}",
                    )
                span.set_status(StatusCode.ERROR, description=output.message)
                tool_calls_total.labels(tool="describe_resource", status="error").inc()
                return output.model_dump_json()

            meta = obj.metadata
            labels: Dict[str, str] = dict(meta.labels or {})
            annotations_count = len(meta.annotations or {})
            created_at = _ts(meta.creation_timestamp)

            spec_summary, status_summary = _extract_spec_status(kind, obj)
            conditions = _extract_conditions(obj)
            recent_events: List[Dict[str, Any]] = []
            if include_events:
                recent_events = _fetch_resource_events(kind, name, namespace, limit=5)
            anomalies = _detect_anomalies(kind, obj)

            result = DescribeResult(
                kind=kind, name=name, namespace=namespace,
                created_at=created_at, labels=labels,
                annotations_count=annotations_count,
                spec_summary=spec_summary, status_summary=status_summary,
                conditions=conditions, recent_events=recent_events,
                anomalies=anomalies,
            )

            output = DescribeResourceOutput(
                status="success",
                data=result.model_dump(),
                anomaly_count=len(anomalies),
                warning_event_count=sum(1 for e in recent_events if e.get("type") == "Warning"),
            )
            span.set_status(StatusCode.OK)
            tool_calls_total.labels(tool="describe_resource", status="success").inc()
            return output.model_dump_json()
        except Exception as e:
            span.set_status(StatusCode.ERROR, description=str(e))
            tool_calls_total.labels(tool="describe_resource", status="error").inc()
            raise


describe_resource_tool = StructuredTool.from_function(
    func=describe_resource,
    name="describe_resource",
    description=(
        "Return a structured describe of any common namespaced Kubernetes resource. "
        "Supports: Pod, Deployment, StatefulSet, DaemonSet, Service, ConfigMap, "
        "Secret, PersistentVolumeClaim, Job, ReplicaSet. "
        "Returns sections: metadata summary, spec_summary, status_summary, "
        "conditions, recent_events (last 5), and anomalies (detected issues). "
        "Use this instead of kind-specific describe tools when you need a consistent "
        "interface across resource types, or when the supervisor needs to reason over "
        "structured output. include_events=False skips the event fetch for speed."
    ),
    args_schema=DescribeResourceInput,
)


# ── Exported list ─────────────────────────────────────────────────────────────

diagnostics_tools = [
    top_nodes_tool,
    top_pods_tool,
    events_watch_tool,
    describe_resource_tool,
]
