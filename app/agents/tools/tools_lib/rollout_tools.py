"""
Generic multi-workload rollout operations.

Supplements deployment_tools.py (Deployment-only) with:
  - rollout_undo      : Deployment + StatefulSet rollback
  - rollout_pause     : Deployment + StatefulSet pause
  - rollout_resume    : Deployment + StatefulSet resume
  - rollout_history   : Deployment + StatefulSet revision list
  - rollout_status    : Deployment + StatefulSet + DaemonSet status
  - rollout_restart   : Deployment + StatefulSet + DaemonSet rolling restart

StatefulSet rollback uses ControllerRevision objects (apps/v1),
which is the same mechanism kubectl rollout undo uses internally.

Existing deployment_tools.py tools (rollout_undo_deployment,
rollout_restart_deployment, get_deployment_rollout_status) are kept
unchanged for backward compatibility; these generic tools supplement them.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from kubernetes import client
from kubernetes.client.exceptions import ApiException
from langchain_core.tools import StructuredTool
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, Field

from app.agents.tools.tools_lib._base import (
    get_apps_v1_api,
    _handle_k8s_exceptions,
)
from app.utils.logger_config import setup_logging
from app.utils.metrics import tool_calls_total

logger = setup_logging(app_name="kubeintellect")
_tracer = trace.get_tracer("kubeintellect.tools")


# ── Output model ──────────────────────────────────────────────────────────────

class RolloutOutput(BaseModel):
    """Typed output for all rollout operations."""
    status: str                                        # success | error | dry_run
    kind: Optional[str] = None
    name: Optional[str] = None
    namespace: Optional[str] = None
    message: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    annotation_to_patch: Optional[Dict[str, str]] = None  # dry_run restart only
    error_type: Optional[str] = None


# ── Input schemas ─────────────────────────────────────────────────────────────

class RolloutTargetInput(BaseModel):
    namespace: str = Field(..., description="Kubernetes namespace.")
    name: str = Field(..., description="Workload name.")
    kind: str = Field(
        default="Deployment",
        description="Workload kind: Deployment, StatefulSet, or DaemonSet.",
    )


class RolloutUndoInput(BaseModel):
    namespace: str = Field(..., description="Kubernetes namespace.")
    name: str = Field(..., description="Workload name.")
    kind: str = Field(
        default="Deployment",
        description="Workload kind: Deployment or StatefulSet.",
    )
    revision: int = Field(
        default=0,
        description="Target revision number. 0 means 'previous revision'.",
    )


class RolloutHistoryInput(BaseModel):
    namespace: str = Field(..., description="Kubernetes namespace.")
    name: str = Field(..., description="Workload name.")
    kind: str = Field(
        default="Deployment",
        description="Workload kind: Deployment or StatefulSet.",
    )
    revision: int = Field(
        default=0,
        description="If > 0, show details for that specific revision only.",
    )


# ── rollout_undo ──────────────────────────────────────────────────────────────

@_handle_k8s_exceptions
def rollout_undo(
    namespace: str,
    name: str,
    kind: str = "Deployment",
    revision: int = 0,
) -> str:
    """Roll back a Deployment or StatefulSet to a previous revision."""
    with _tracer.start_as_current_span("rollout_undo") as span:
        span.set_attribute("k8s.namespace", namespace)
        span.set_attribute("k8s.resource.kind", kind)
        span.set_attribute("k8s.resource.name", name)
        try:
            apps_v1 = get_apps_v1_api()
            kind_lower = kind.lower()

            if kind_lower == "deployment":
                raw = _rollout_undo_deployment(apps_v1, namespace, name, revision)
            elif kind_lower == "statefulset":
                raw = _rollout_undo_statefulset(apps_v1, namespace, name, revision)
            else:
                output = RolloutOutput(
                    status="error",
                    message=(
                        f"rollout_undo only supports Deployment and StatefulSet, not '{kind}'. "
                        "DaemonSets do not maintain a revision history that supports undo."
                    ),
                    error_type="unsupported_kind",
                )
                span.set_status(StatusCode.ERROR, description=output.message)
                tool_calls_total.labels(tool="rollout_undo", status="error").inc()
                return output.model_dump_json()

            output = RolloutOutput(**raw)
            if output.status == "error":
                span.set_status(StatusCode.ERROR, description=output.message)
                tool_calls_total.labels(tool="rollout_undo", status="error").inc()
            else:
                span.set_status(StatusCode.OK)
                tool_calls_total.labels(tool="rollout_undo", status="success").inc()
            return output.model_dump_json()
        except Exception as e:
            span.set_status(StatusCode.ERROR, description=str(e))
            tool_calls_total.labels(tool="rollout_undo", status="error").inc()
            raise


def _rollout_undo_deployment(
    apps_v1: client.AppsV1Api,
    namespace: str,
    name: str,
    revision: int,
) -> Dict[str, Any]:
    deployment = apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
    annotations = deployment.metadata.annotations or {}
    current_revision = int(annotations.get("deployment.kubernetes.io/revision", "0"))

    if current_revision == 0:
        return {"status": "error", "message": f"Deployment '{name}' has no revision history."}

    target_revision = revision if revision > 0 else current_revision - 1
    if target_revision < 1:
        return {
            "status": "error",
            "message": (
                f"Cannot roll back: current revision is {current_revision}, "
                "no earlier revision available."
            ),
        }

    selector = deployment.spec.selector.match_labels
    label_selector = ",".join(f"{k}={v}" for k, v in selector.items())
    rs_list = apps_v1.list_namespaced_replica_set(
        namespace=namespace, label_selector=label_selector
    )

    target_rs = None
    available_revisions = []
    for rs in rs_list.items:
        rs_rev = int(
            (rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "-1")
        )
        if rs_rev >= 0:
            available_revisions.append(rs_rev)
        if rs_rev == target_revision:
            target_rs = rs

    if target_rs is None:
        return {
            "status": "error",
            "message": (
                f"Revision {target_revision} not found for Deployment '{name}'. "
                f"Available revisions: {sorted(available_revisions)}"
            ),
        }

    patch_body = {"spec": {"template": target_rs.spec.template.to_dict()}}
    apps_v1.patch_namespaced_deployment(name=name, namespace=namespace, body=patch_body)

    return {
        "status": "success",
        "data": {
            "kind": "Deployment",
            "name": name,
            "namespace": namespace,
            "rolled_back_from_revision": current_revision,
            "rolled_back_to_revision": target_revision,
            "message": "Rollback triggered. Use rollout_status to monitor progress.",
        },
    }


def _rollout_undo_statefulset(
    apps_v1: client.AppsV1Api,
    namespace: str,
    name: str,
    revision: int,
) -> Dict[str, Any]:
    """Roll back a StatefulSet using its ControllerRevision history."""
    sts = apps_v1.read_namespaced_stateful_set(name=name, namespace=namespace)
    current_revision = sts.status.current_revision or ""

    selector = sts.spec.selector.match_labels or {}
    label_selector = ",".join(f"{k}={v}" for k, v in selector.items())
    cr_list = apps_v1.list_namespaced_controller_revision(
        namespace=namespace, label_selector=label_selector
    )

    if not cr_list.items:
        return {
            "status": "error",
            "message": (
                f"No ControllerRevisions found for StatefulSet '{name}'. "
                "The StatefulSet may not have revision history yet."
            ),
        }

    revisions = sorted(cr_list.items, key=lambda r: r.revision)
    revision_numbers = [r.revision for r in revisions]

    if revision > 0:
        target_cr = next((r for r in revisions if r.revision == revision), None)
        if target_cr is None:
            return {
                "status": "error",
                "message": (
                    f"Revision {revision} not found for StatefulSet '{name}'. "
                    f"Available revisions: {revision_numbers}"
                ),
            }
    else:
        previous = [r for r in revisions if r.name != current_revision]
        if not previous:
            return {
                "status": "error",
                "message": (
                    f"StatefulSet '{name}' has no previous revision to roll back to. "
                    f"Available revisions: {revision_numbers}"
                ),
            }
        target_cr = previous[-1]

    if target_cr.data is None:
        return {
            "status": "error",
            "message": f"ControllerRevision data is empty for revision {target_cr.revision}.",
        }

    revision_data = (
        target_cr.data.to_dict()
        if hasattr(target_cr.data, "to_dict")
        else target_cr.data
    )

    spec_patch = revision_data.get("spec", {})
    if not spec_patch:
        return {
            "status": "error",
            "message": (
                f"Could not extract spec from ControllerRevision revision "
                f"{target_cr.revision}. Data format may be unexpected: "
                f"{list(revision_data.keys())}"
            ),
        }

    apps_v1.patch_namespaced_stateful_set(
        name=name, namespace=namespace, body={"spec": spec_patch}
    )

    return {
        "status": "success",
        "data": {
            "kind": "StatefulSet",
            "name": name,
            "namespace": namespace,
            "rolled_back_from_revision": current_revision,
            "rolled_back_to_revision": target_cr.name,
            "revision_number": target_cr.revision,
            "message": "Rollback triggered. Use rollout_status to monitor progress.",
        },
    }


rollout_undo_tool = StructuredTool.from_function(
    func=rollout_undo,
    name="rollout_undo",
    description=(
        "Roll back a Deployment or StatefulSet to its previous revision or a specific revision. "
        "Equivalent to `kubectl rollout undo`. Deployment: uses ReplicaSet revision history. "
        "StatefulSet: uses ControllerRevision history. "
        "Always follow up with rollout_status to confirm the rollback is progressing."
    ),
    args_schema=RolloutUndoInput,
)


# ── rollout_pause ─────────────────────────────────────────────────────────────

@_handle_k8s_exceptions
def rollout_pause(
    namespace: str, name: str, kind: str = "Deployment"
) -> str:
    """Pause a rolling update on a Deployment or StatefulSet."""
    with _tracer.start_as_current_span("rollout_pause") as span:
        span.set_attribute("k8s.namespace", namespace)
        span.set_attribute("k8s.resource.kind", kind)
        span.set_attribute("k8s.resource.name", name)
        try:
            apps_v1 = get_apps_v1_api()
            kind_lower = kind.lower()

            if kind_lower == "deployment":
                apps_v1.patch_namespaced_deployment(
                    name=name, namespace=namespace, body={"spec": {"paused": True}}
                )
                output = RolloutOutput(
                    status="success",
                    data={"kind": kind, "name": name, "namespace": namespace,
                          "message": f"Deployment '{name}' rollout paused."},
                )
            elif kind_lower == "statefulset":
                sts = apps_v1.read_namespaced_stateful_set(name=name, namespace=namespace)
                total_replicas = sts.spec.replicas or 1
                apps_v1.patch_namespaced_stateful_set(
                    name=name, namespace=namespace,
                    body={"spec": {"updateStrategy": {"type": "RollingUpdate",
                                                       "rollingUpdate": {"partition": total_replicas}}}},
                )
                output = RolloutOutput(
                    status="success",
                    data={"kind": kind, "name": name, "namespace": namespace,
                          "partition_set_to": total_replicas,
                          "message": (f"StatefulSet '{name}' update paused (partition={total_replicas}). "
                                      "No pods will be updated until resumed.")},
                )
            else:
                output = RolloutOutput(
                    status="error",
                    message=f"rollout_pause supports Deployment and StatefulSet, not '{kind}'.",
                    error_type="unsupported_kind",
                )

            if output.status == "error":
                span.set_status(StatusCode.ERROR, description=output.message)
                tool_calls_total.labels(tool="rollout_pause", status="error").inc()
            else:
                span.set_status(StatusCode.OK)
                tool_calls_total.labels(tool="rollout_pause", status="success").inc()
            return output.model_dump_json()
        except Exception as e:
            span.set_status(StatusCode.ERROR, description=str(e))
            tool_calls_total.labels(tool="rollout_pause", status="error").inc()
            raise


rollout_pause_tool = StructuredTool.from_function(
    func=rollout_pause,
    name="rollout_pause",
    description=(
        "Pause a rolling update on a Deployment or StatefulSet. "
        "Deployment: sets spec.paused=True. "
        "StatefulSet: sets partition to replica count so no pods are updated. "
        "Equivalent to `kubectl rollout pause`."
    ),
    args_schema=RolloutTargetInput,
)


# ── rollout_resume ────────────────────────────────────────────────────────────

@_handle_k8s_exceptions
def rollout_resume(
    namespace: str, name: str, kind: str = "Deployment"
) -> str:
    """Resume a paused rolling update on a Deployment or StatefulSet."""
    with _tracer.start_as_current_span("rollout_resume") as span:
        span.set_attribute("k8s.namespace", namespace)
        span.set_attribute("k8s.resource.kind", kind)
        span.set_attribute("k8s.resource.name", name)
        try:
            apps_v1 = get_apps_v1_api()
            kind_lower = kind.lower()

            if kind_lower == "deployment":
                apps_v1.patch_namespaced_deployment(
                    name=name, namespace=namespace, body={"spec": {"paused": False}}
                )
                output = RolloutOutput(
                    status="success",
                    data={"kind": kind, "name": name, "namespace": namespace,
                          "message": f"Deployment '{name}' rollout resumed."},
                )
            elif kind_lower == "statefulset":
                apps_v1.patch_namespaced_stateful_set(
                    name=name, namespace=namespace,
                    body={"spec": {"updateStrategy": {"type": "RollingUpdate",
                                                       "rollingUpdate": {"partition": 0}}}},
                )
                output = RolloutOutput(
                    status="success",
                    data={"kind": kind, "name": name, "namespace": namespace,
                          "partition_set_to": 0,
                          "message": (f"StatefulSet '{name}' update resumed (partition=0). "
                                      "All pods are now eligible for updates.")},
                )
            else:
                output = RolloutOutput(
                    status="error",
                    message=f"rollout_resume supports Deployment and StatefulSet, not '{kind}'.",
                    error_type="unsupported_kind",
                )

            if output.status == "error":
                span.set_status(StatusCode.ERROR, description=output.message)
                tool_calls_total.labels(tool="rollout_resume", status="error").inc()
            else:
                span.set_status(StatusCode.OK)
                tool_calls_total.labels(tool="rollout_resume", status="success").inc()
            return output.model_dump_json()
        except Exception as e:
            span.set_status(StatusCode.ERROR, description=str(e))
            tool_calls_total.labels(tool="rollout_resume", status="error").inc()
            raise


rollout_resume_tool = StructuredTool.from_function(
    func=rollout_resume,
    name="rollout_resume",
    description=(
        "Resume a paused rolling update on a Deployment or StatefulSet. "
        "Deployment: clears spec.paused. "
        "StatefulSet: resets partition to 0 so all pods are update-eligible. "
        "Equivalent to `kubectl rollout resume`."
    ),
    args_schema=RolloutTargetInput,
)


# ── rollout_history ───────────────────────────────────────────────────────────

@_handle_k8s_exceptions
def rollout_history(
    namespace: str,
    name: str,
    kind: str = "Deployment",
    revision: int = 0,
) -> str:
    """List revision history for a Deployment or StatefulSet."""
    with _tracer.start_as_current_span("rollout_history") as span:
        span.set_attribute("k8s.namespace", namespace)
        span.set_attribute("k8s.resource.kind", kind)
        span.set_attribute("k8s.resource.name", name)
        try:
            apps_v1 = get_apps_v1_api()
            kind_lower = kind.lower()

            if kind_lower == "deployment":
                deployment = apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
                selector = deployment.spec.selector.match_labels or {}
                label_selector = ",".join(f"{k}={v}" for k, v in selector.items())
                rs_list = apps_v1.list_namespaced_replica_set(
                    namespace=namespace, label_selector=label_selector
                )
                current_revision = int(
                    (deployment.metadata.annotations or {}).get(
                        "deployment.kubernetes.io/revision", "0"
                    )
                )

                history = []
                for rs in rs_list.items:
                    rs_annotations = rs.metadata.annotations or {}
                    rev_str = rs_annotations.get("deployment.kubernetes.io/revision")
                    if rev_str is None:
                        continue
                    rev = int(rev_str)
                    containers = [
                        {"name": c.name, "image": c.image}
                        for c in (rs.spec.template.spec.containers or [])
                    ]
                    history.append({
                        "revision": rev,
                        "change_cause": rs_annotations.get("kubernetes.io/change-cause", "<none>"),
                        "containers": containers,
                        "created": (
                            rs.metadata.creation_timestamp.isoformat()
                            if rs.metadata.creation_timestamp else None
                        ),
                        "is_current": rev == current_revision,
                    })
                history.sort(key=lambda x: x["revision"])

                if revision > 0:
                    match = next((h for h in history if h["revision"] == revision), None)
                    if not match:
                        output = RolloutOutput(
                            status="error",
                            message=(
                                f"Revision {revision} not found. "
                                f"Available: {[h['revision'] for h in history]}"
                            ),
                        )
                        span.set_status(StatusCode.ERROR, description=output.message)
                        tool_calls_total.labels(tool="rollout_history", status="error").inc()
                        return output.model_dump_json()
                    output = RolloutOutput(status="success", data=match)
                else:
                    output = RolloutOutput(
                        status="success",
                        data={
                            "kind": "Deployment", "name": name, "namespace": namespace,
                            "current_revision": current_revision, "history": history,
                        },
                    )

            elif kind_lower == "statefulset":
                sts = apps_v1.read_namespaced_stateful_set(name=name, namespace=namespace)
                selector = sts.spec.selector.match_labels or {}
                label_selector = ",".join(f"{k}={v}" for k, v in selector.items())
                cr_list = apps_v1.list_namespaced_controller_revision(
                    namespace=namespace, label_selector=label_selector
                )

                history = sorted(
                    [
                        {
                            "revision": cr.revision,
                            "name": cr.name,
                            "created": (
                                cr.metadata.creation_timestamp.isoformat()
                                if cr.metadata.creation_timestamp else None
                            ),
                            "is_current": cr.name == sts.status.current_revision,
                            "is_update": cr.name == sts.status.update_revision,
                        }
                        for cr in cr_list.items
                    ],
                    key=lambda x: x["revision"],
                )

                if revision > 0:
                    match = next((h for h in history if h["revision"] == revision), None)
                    if not match:
                        output = RolloutOutput(
                            status="error",
                            message=(
                                f"Revision {revision} not found. "
                                f"Available: {[h['revision'] for h in history]}"
                            ),
                        )
                        span.set_status(StatusCode.ERROR, description=output.message)
                        tool_calls_total.labels(tool="rollout_history", status="error").inc()
                        return output.model_dump_json()
                    output = RolloutOutput(status="success", data=match)
                else:
                    output = RolloutOutput(
                        status="success",
                        data={
                            "kind": "StatefulSet", "name": name, "namespace": namespace,
                            "current_revision": sts.status.current_revision,
                            "update_revision": sts.status.update_revision,
                            "history": history,
                        },
                    )

            else:
                output = RolloutOutput(
                    status="error",
                    message=f"rollout_history supports Deployment and StatefulSet, not '{kind}'.",
                    error_type="unsupported_kind",
                )

            if output.status == "error":
                span.set_status(StatusCode.ERROR, description=output.message)
                tool_calls_total.labels(tool="rollout_history", status="error").inc()
            else:
                span.set_status(StatusCode.OK)
                tool_calls_total.labels(tool="rollout_history", status="success").inc()
            return output.model_dump_json()
        except Exception as e:
            span.set_status(StatusCode.ERROR, description=str(e))
            tool_calls_total.labels(tool="rollout_history", status="error").inc()
            raise


rollout_history_tool = StructuredTool.from_function(
    func=rollout_history,
    name="rollout_history",
    description=(
        "List revision history for a Deployment or StatefulSet. "
        "Deployment: shows each ReplicaSet revision with container images and change-cause. "
        "StatefulSet: shows ControllerRevisions with revision numbers. "
        "Pass revision > 0 to inspect a specific revision. "
        "Equivalent to `kubectl rollout history`."
    ),
    args_schema=RolloutHistoryInput,
)


# ── rollout_status (generic) ──────────────────────────────────────────────────

@_handle_k8s_exceptions
def rollout_status(
    namespace: str, name: str, kind: str = "Deployment"
) -> str:
    """Get rollout status for a Deployment, StatefulSet, or DaemonSet."""
    with _tracer.start_as_current_span("rollout_status") as span:
        span.set_attribute("k8s.namespace", namespace)
        span.set_attribute("k8s.resource.kind", kind)
        span.set_attribute("k8s.resource.name", name)
        try:
            apps_v1 = get_apps_v1_api()
            kind_lower = kind.lower()

            if kind_lower == "deployment":
                resolved_ns = namespace
                try:
                    d = apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
                except ApiException as exc:
                    if exc.status != 404:
                        raise
                    try:
                        all_deployments = apps_v1.list_deployment_for_all_namespaces(
                            field_selector=f"metadata.name={name}", timeout_seconds=10,
                        )
                    except ApiException as fallback_exc:
                        if fallback_exc.status == 403:
                            output = RolloutOutput(
                                status="error",
                                message=(
                                    f"Deployment '{name}' not found in namespace '{namespace}'. "
                                    "Cross-namespace search requires cluster-wide list permission (403 Forbidden)."
                                ),
                                error_type="PermissionDenied",
                            )
                            span.set_status(StatusCode.ERROR, description=output.message)
                            tool_calls_total.labels(tool="rollout_status", status="error").inc()
                            return output.model_dump_json()
                        raise
                    matches = all_deployments.items
                    if not matches:
                        output = RolloutOutput(
                            status="error",
                            message=f"Deployment '{name}' not found in namespace '{namespace}' or any other namespace.",
                            error_type="NotFound",
                        )
                        span.set_status(StatusCode.ERROR, description=output.message)
                        tool_calls_total.labels(tool="rollout_status", status="error").inc()
                        return output.model_dump_json()
                    if len(matches) > 1:
                        found_ns = [m.metadata.namespace for m in matches]
                        output = RolloutOutput(
                            status="error",
                            message=(
                                f"Deployment '{name}' not found in namespace '{namespace}'. "
                                f"Found in multiple namespaces: {found_ns}. Please specify the correct namespace."
                            ),
                            error_type="AmbiguousNamespace",
                        )
                        span.set_status(StatusCode.ERROR, description=output.message)
                        tool_calls_total.labels(tool="rollout_status", status="error").inc()
                        return output.model_dump_json()
                    d = matches[0]
                    resolved_ns = d.metadata.namespace
                    logger.info(
                        "tool:namespace_fallback original_ns=%s resolved_ns=%s deployment=%s",
                        namespace, resolved_ns, name,
                    )
                desired = d.spec.replicas or 0
                st = d.status
                updated = st.updated_replicas or 0
                available = st.available_replicas or 0
                ready = st.ready_replicas or 0
                unavailable = st.unavailable_replicas or 0
                complete = updated == desired and available == desired and ready == desired
                conditions = [
                    {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
                    for c in (st.conditions or [])
                ]
                data: Dict[str, Any] = {
                    "kind": "Deployment", "name": name, "namespace": resolved_ns,
                    "rollout_complete": complete, "desired": desired, "updated": updated,
                    "available": available, "ready": ready, "unavailable": unavailable,
                    "conditions": conditions,
                }
                if resolved_ns != namespace:
                    data["namespace_resolved"] = resolved_ns
                output = RolloutOutput(status="success", data=data)

            elif kind_lower == "statefulset":
                sts = apps_v1.read_namespaced_stateful_set(name=name, namespace=namespace)
                desired = sts.spec.replicas or 0
                st = sts.status
                ready = st.ready_replicas or 0
                current = st.current_replicas or 0
                updated = st.updated_replicas or 0
                complete = ready == desired and updated == desired
                output = RolloutOutput(
                    status="success",
                    data={
                        "kind": "StatefulSet", "name": name, "namespace": namespace,
                        "rollout_complete": complete, "desired": desired, "ready": ready,
                        "current": current, "updated": updated,
                        "current_revision": st.current_revision,
                        "update_revision": st.update_revision,
                    },
                )

            elif kind_lower == "daemonset":
                ds = apps_v1.read_namespaced_daemon_set(name=name, namespace=namespace)
                st = ds.status
                desired = st.desired_number_scheduled or 0
                updated = st.updated_number_scheduled or 0
                available = st.number_available or 0
                ready = st.number_ready or 0
                unavailable = st.number_unavailable or 0
                complete = updated == desired and available == desired
                output = RolloutOutput(
                    status="success",
                    data={
                        "kind": "DaemonSet", "name": name, "namespace": namespace,
                        "rollout_complete": complete, "desired": desired, "updated": updated,
                        "available": available, "ready": ready, "unavailable": unavailable,
                    },
                )

            else:
                output = RolloutOutput(
                    status="error",
                    message=(
                        f"Unsupported kind '{kind}'. "
                        "Use Deployment, StatefulSet, or DaemonSet."
                    ),
                    error_type="unsupported_kind",
                )

            if output.status == "error":
                span.set_status(StatusCode.ERROR, description=output.message)
                tool_calls_total.labels(tool="rollout_status", status="error").inc()
            else:
                span.set_status(StatusCode.OK)
                tool_calls_total.labels(tool="rollout_status", status="success").inc()
            return output.model_dump_json()
        except Exception as e:
            span.set_status(StatusCode.ERROR, description=str(e))
            tool_calls_total.labels(tool="rollout_status", status="error").inc()
            raise


rollout_status_tool = StructuredTool.from_function(
    func=rollout_status,
    name="rollout_status",
    description=(
        "Get rollout status for a Deployment, StatefulSet, or DaemonSet. "
        "Returns desired/ready/updated/available replica counts and whether "
        "the rollout is complete. Equivalent to `kubectl rollout status`."
    ),
    args_schema=RolloutTargetInput,
)


# ── rollout_restart (generic) ─────────────────────────────────────────────────

class RolloutRestartInput(BaseModel):
    namespace: str = Field(..., description="Kubernetes namespace.")
    name: str = Field(..., description="Workload name.")
    kind: str = Field(
        default="Deployment",
        description="Workload kind: Deployment, StatefulSet, or DaemonSet.",
    )
    dry_run: bool = Field(
        default=True,
        description=(
            "True (default): preview the annotation patch — no write. "
            "False: apply the rolling restart after user confirms."
        ),
    )


@_handle_k8s_exceptions
def rollout_restart(
    namespace: str,
    name: str,
    kind: str = "Deployment",
    dry_run: bool = True,
) -> str:
    """Trigger a rolling restart on a Deployment, StatefulSet, or DaemonSet.

    Two-step HITL:
      dry_run=True  → show what annotation would be patched, no write.
      dry_run=False → apply after user confirmation.
    """
    with _tracer.start_as_current_span("rollout_restart") as span:
        span.set_attribute("k8s.namespace", namespace)
        span.set_attribute("k8s.resource.kind", kind)
        span.set_attribute("k8s.resource.name", name)
        span.set_attribute("tool.dry_run", dry_run)
        try:
            kind_lower = kind.lower()

            if kind_lower not in ("deployment", "statefulset", "daemonset"):
                output = RolloutOutput(
                    status="error",
                    message=f"rollout_restart supports Deployment, StatefulSet, DaemonSet — not '{kind}'.",
                    error_type="unsupported_kind",
                )
                span.set_status(StatusCode.ERROR, description=output.message)
                tool_calls_total.labels(tool="rollout_restart", status="error").inc()
                return output.model_dump_json()

            restart_time = datetime.now(timezone.utc).isoformat()

            if dry_run:
                output = RolloutOutput(
                    status="dry_run",
                    kind=kind,
                    name=name,
                    namespace=namespace,
                    annotation_to_patch={"kubectl.kubernetes.io/restartedAt": restart_time},
                    message=(
                        f"DRY RUN — a rolling restart of {kind} '{name}' would be triggered "
                        f"by patching spec.template.metadata.annotations."
                        f"kubectl.kubernetes.io/restartedAt = {restart_time!r}. "
                        "No write occurred. "
                        "Confirm with the user, then call rollout_restart with dry_run=False to apply."
                    ),
                )
                span.set_status(StatusCode.OK)
                tool_calls_total.labels(tool="rollout_restart", status="success").inc()
                return output.model_dump_json()

            apps_v1 = get_apps_v1_api()
            patch_body = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {"kubectl.kubernetes.io/restartedAt": restart_time}
                        }
                    }
                }
            }

            if kind_lower == "deployment":
                apps_v1.patch_namespaced_deployment(name=name, namespace=namespace, body=patch_body)
            elif kind_lower == "statefulset":
                apps_v1.patch_namespaced_stateful_set(name=name, namespace=namespace, body=patch_body)
            elif kind_lower == "daemonset":
                apps_v1.patch_namespaced_daemon_set(name=name, namespace=namespace, body=patch_body)

            logger.info(
                f"rollout_restart applied: {kind}/{name} namespace={namespace} "
                f"restartedAt={restart_time}"
            )

            output = RolloutOutput(
                status="success",
                data={
                    "kind": kind, "name": name, "namespace": namespace,
                    "restarted_at": restart_time,
                    "message": (
                        f"Rolling restart triggered for {kind} '{name}'. "
                        "Use rollout_status to monitor progress."
                    ),
                },
            )
            span.set_status(StatusCode.OK)
            tool_calls_total.labels(tool="rollout_restart", status="success").inc()
            return output.model_dump_json()
        except Exception as e:
            span.set_status(StatusCode.ERROR, description=str(e))
            tool_calls_total.labels(tool="rollout_restart", status="error").inc()
            raise


rollout_restart_tool = StructuredTool.from_function(
    func=rollout_restart,
    name="rollout_restart",
    description=(
        "Trigger a rolling restart on a Deployment, StatefulSet, or DaemonSet "
        "by patching the restartedAt annotation. Equivalent to `kubectl rollout restart`. "
        "ALWAYS call with dry_run=True first to preview the patch. "
        "Only call with dry_run=False after user confirms."
    ),
    args_schema=RolloutRestartInput,
)


# ── Exported list ─────────────────────────────────────────────────────────────

rollout_tools = [
    rollout_undo_tool,
    rollout_pause_tool,
    rollout_resume_tool,
    rollout_history_tool,
    rollout_status_tool,
    rollout_restart_tool,
]
