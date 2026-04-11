"""
Kubernetes patch and mutation tools.

All mutation tools in this file follow a mandatory two-step HITL pattern:

  Step 1  dry_run=True  (default)
          Preview what would change — no write occurs. Returns a diff showing
          current state vs. proposed state. The agent MUST show this to the user.

  Step 2  dry_run=False
          Apply the change. The agent MUST only call this after the user has
          explicitly confirmed with "yes", "confirm", "proceed", or equivalent.

Tools in this file:
  - set_env          : add / update / remove env vars on a workload container
  - patch_resource   : strategic merge patch on any common namespaced resource
  - label_resource   : add / update / remove labels on any common resource
  - annotate_resource: add / update / remove annotations on any common resource
"""

import json
from typing import Any, Dict, Optional

from kubernetes.client.exceptions import ApiException
from langchain_core.tools import StructuredTool
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, Field

from app.agents.tools.tools_lib._base import (
    get_apps_v1_api,
    get_core_v1_api,
    _handle_k8s_exceptions,
)
from app.utils.logger_config import setup_logging
from app.utils.metrics import tool_calls_total

logger = setup_logging(app_name="kubeintellect")
_tracer = trace.get_tracer("kubeintellect.tools")


# ── Output model ─────────────────────────────────────────────────────────────

class PatchOutput(BaseModel):
    """Typed output for all patch/mutation operations."""
    status: str                                    # success | error | dry_run | dry_run_failed
    kind: Optional[str] = None
    name: Optional[str] = None
    namespace: Optional[str] = None
    message: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    diff: Optional[str] = None
    container: Optional[str] = None
    patch_body: Optional[Dict[str, Any]] = None
    server_validation: Optional[str] = None
    projected_resource_version: Optional[str] = None
    current_labels: Optional[Dict[str, Any]] = None
    proposed_labels: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    note: Optional[str] = None
    error_type: Optional[str] = None


# ── API dispatch table ────────────────────────────────────────────────────────
# Maps lowercase kind → (api_type, read_fn_name, patch_fn_name)
# api_type "apps"  → AppsV1Api
# api_type "core"  → CoreV1Api

_WORKLOAD_KINDS: dict[str, tuple[str, str, str]] = {
    "deployment":  ("apps", "read_namespaced_deployment",   "patch_namespaced_deployment"),
    "statefulset": ("apps", "read_namespaced_stateful_set", "patch_namespaced_stateful_set"),
    "daemonset":   ("apps", "read_namespaced_daemon_set",   "patch_namespaced_daemon_set"),
}

_ALL_KINDS: dict[str, tuple[str, str, str]] = {
    **_WORKLOAD_KINDS,
    "replicaset":            ("apps", "read_namespaced_replica_set",             "patch_namespaced_replica_set"),
    "pod":                   ("core", "read_namespaced_pod",                     "patch_namespaced_pod"),
    "service":               ("core", "read_namespaced_service",                 "patch_namespaced_service"),
    "configmap":             ("core", "read_namespaced_config_map",              "patch_namespaced_config_map"),
    "secret":                ("core", "read_namespaced_secret",                  "patch_namespaced_secret"),
    "serviceaccount":        ("core", "read_namespaced_service_account",         "patch_namespaced_service_account"),
    "persistentvolumeclaim": ("core", "read_namespaced_persistent_volume_claim", "patch_namespaced_persistent_volume_claim"),
}

SUPPORTED_KINDS = sorted(_ALL_KINDS.keys())


def _api(api_type: str):
    return get_apps_v1_api() if api_type == "apps" else get_core_v1_api()


def _read(kind: str, name: str, namespace: str):
    k = kind.lower()
    if k not in _ALL_KINDS:
        raise ValueError(f"Unsupported kind '{kind}'. Supported: {SUPPORTED_KINDS}")
    api_type, read_fn, _ = _ALL_KINDS[k]
    return getattr(_api(api_type), read_fn)(name=name, namespace=namespace)


def _patch(kind: str, name: str, namespace: str, body: dict, dry_run: bool) -> Any:
    k = kind.lower()
    if k not in _ALL_KINDS:
        raise ValueError(f"Unsupported kind '{kind}'. Supported: {SUPPORTED_KINDS}")
    api_type, _, patch_fn = _ALL_KINDS[k]
    kwargs: dict[str, Any] = {"name": name, "namespace": namespace, "body": body}
    if dry_run:
        # Server-side dry-run: validates and returns the resulting object without persisting.
        kwargs["dry_run"] = "All"
    return getattr(_api(api_type), patch_fn)(**kwargs)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _containers_path(kind: str) -> str:
    """Return 'pod' if kind is Pod (spec.containers), else 'workload' (spec.template.spec.containers)."""
    return "pod" if kind.lower() == "pod" else "workload"


def _get_containers(obj: Any, kind: str) -> list:
    """Extract the container list from a resource object."""
    if _containers_path(kind) == "pod":
        return obj.spec.containers or []
    return obj.spec.template.spec.containers or []


def _find_container(containers: list, container_name: str) -> tuple[Any, int]:
    """Return (container, index). If container_name is empty, returns the first container."""
    if not containers:
        return None, -1
    if not container_name:
        return containers[0], 0
    for i, c in enumerate(containers):
        if c.name == container_name:
            return c, i
    return None, -1


def _env_list_to_map(env_list: list | None) -> dict[str, str]:
    """Convert k8s EnvVar list to {name: value} dict. Refs shown as '<valueFrom>'."""
    if not env_list:
        return {}
    result = {}
    for e in env_list:
        if hasattr(e, "name"):
            name = e.name
            val = e.value if e.value is not None else "<valueFrom>"
        else:  # dict
            name = e.get("name", "")
            val = e.get("value") or "<valueFrom>"
        result[name] = val
    return result


def _env_map_to_list(env_map: dict[str, str]) -> list[dict]:
    """Convert {name: value} dict to k8s EnvVar list dicts."""
    return [{"name": k, "value": v} for k, v in env_map.items()]


def _build_env_diff(current: dict[str, str], proposed: dict[str, str]) -> str:
    """Human-readable diff between current and proposed env var maps."""
    added = {k: v for k, v in proposed.items() if k not in current}
    removed = {k: v for k, v in current.items() if k not in proposed}
    changed = {
        k: (current[k], proposed[k])
        for k in proposed
        if k in current and current[k] != proposed[k]
    }
    unchanged_count = len(proposed) - len(added) - len(changed)

    if not added and not removed and not changed:
        return "No changes — proposed env vars are identical to current state."

    lines = []
    for k, v in sorted(added.items()):
        lines.append(f"  + {k}={v!r}  ← ADDED")
    for k, (old, new) in sorted(changed.items()):
        lines.append(f"  ~ {k}: {old!r} → {new!r}  ← CHANGED")
    for k, v in sorted(removed.items()):
        lines.append(f"  - {k}={v!r}  ← REMOVED")
    lines.append(f"  (unchanged: {unchanged_count} env vars)")
    return "\n".join(lines)


def _build_kv_diff(label_or_annotation: str, current: dict, proposed: dict) -> str:
    """Diff for labels or annotations."""
    added = {k: v for k, v in proposed.items() if k not in current}
    removed = {k: v for k, v in current.items() if k not in proposed}
    changed = {
        k: (current[k], proposed[k])
        for k in proposed
        if k in current and current[k] != proposed[k]
    }
    unchanged_count = len(proposed) - len(added) - len(changed)

    if not added and not removed and not changed:
        return f"No changes — proposed {label_or_annotation} are identical to current state."

    lines = []
    for k, v in sorted(added.items()):
        lines.append(f"  + {k}={v!r}  ← ADDED")
    for k, (old, new) in sorted(changed.items()):
        lines.append(f"  ~ {k}: {old!r} → {new!r}  ← CHANGED")
    for k, v in sorted(removed.items()):
        lines.append(f"  - {k}={v!r}  ← REMOVED")
    lines.append(f"  (unchanged: {unchanged_count})")
    return "\n".join(lines)


# ── Input schemas ─────────────────────────────────────────────────────────────

class SetEnvInput(BaseModel):
    namespace: str = Field(..., description="Kubernetes namespace.")
    name: str = Field(..., description="Workload name (Deployment, StatefulSet, or DaemonSet).")
    kind: str = Field(
        default="Deployment",
        description="Workload kind: Deployment, StatefulSet, or DaemonSet.",
    )
    env_vars: Dict[str, Optional[str]] = Field(
        ...,
        description=(
            "Env vars to add, update, or remove. "
            "Format: {\"KEY\": \"value\"} to add/update, {\"KEY\": null} to remove. "
            "Only the specified keys are affected; all other env vars are preserved."
        ),
    )
    container_name: str = Field(
        default="",
        description=(
            "Name of the container to modify. "
            "Leave empty to target the first container."
        ),
    )
    dry_run: bool = Field(
        default=True,
        description=(
            "True (default): preview changes and show diff — NO write occurs. "
            "False: apply after user has confirmed the dry-run diff."
        ),
    )


class PatchResourceInput(BaseModel):
    namespace: str = Field(..., description="Kubernetes namespace.")
    kind: str = Field(
        ...,
        description=(
            f"Resource kind. Supported: {SUPPORTED_KINDS}"
        ),
    )
    name: str = Field(..., description="Resource name.")
    patch_body: Dict[str, Any] = Field(
        ...,
        description=(
            "Strategic merge patch body as a JSON-serialisable dict. "
            "Example: {\"spec\": {\"replicas\": 3}}"
        ),
    )
    dry_run: bool = Field(
        default=True,
        description=(
            "True (default): server-side dry-run — validates and previews result, NO write. "
            "False: apply after user has confirmed the dry-run preview."
        ),
    )


class LabelAnnotateInput(BaseModel):
    namespace: str = Field(..., description="Kubernetes namespace.")
    kind: str = Field(
        ...,
        description=f"Resource kind. Supported: {SUPPORTED_KINDS}",
    )
    name: str = Field(..., description="Resource name.")
    labels: Dict[str, Optional[str]] = Field(
        ...,
        description=(
            "Labels to add/update/remove. "
            "{\"key\": \"value\"} to set, {\"key\": null} to remove."
        ),
    )
    dry_run: bool = Field(
        default=True,
        description=(
            "True (default): preview diff — NO write. "
            "False: apply after user confirmation."
        ),
    )


class AnnotateInput(BaseModel):
    namespace: str = Field(..., description="Kubernetes namespace.")
    kind: str = Field(
        ...,
        description=f"Resource kind. Supported: {SUPPORTED_KINDS}",
    )
    name: str = Field(..., description="Resource name.")
    annotations: Dict[str, Optional[str]] = Field(
        ...,
        description=(
            "Annotations to add/update/remove. "
            "{\"key\": \"value\"} to set, {\"key\": null} to remove."
        ),
    )
    dry_run: bool = Field(
        default=True,
        description=(
            "True (default): preview diff — NO write. "
            "False: apply after user confirmation."
        ),
    )


# ── set_env ───────────────────────────────────────────────────────────────────

@_handle_k8s_exceptions
def set_env(
    namespace: str,
    name: str,
    kind: str = "Deployment",
    env_vars: Optional[Dict[str, Optional[str]]] = None,
    container_name: str = "",
    dry_run: bool = True,
) -> str:
    """Add, update, or remove environment variables on a running workload container.

    Two-step HITL:
      dry_run=True  → preview diff, no write.
      dry_run=False → apply after user confirms the diff.
    """
    with _tracer.start_as_current_span("set_env") as span:
        span.set_attribute("k8s.namespace", namespace)
        span.set_attribute("k8s.resource.kind", kind)
        span.set_attribute("k8s.resource.name", name)
        span.set_attribute("tool.dry_run", dry_run)
        try:
            if not env_vars:
                output = PatchOutput(status="error", message="env_vars must be a non-empty dict.",
                                     error_type="invalid_input")
                span.set_status(StatusCode.ERROR, description=output.message)
                tool_calls_total.labels(tool="set_env", status="error").inc()
                return output.model_dump_json()

            kind_lower = kind.lower()
            if kind_lower not in _WORKLOAD_KINDS:
                output = PatchOutput(
                    status="error",
                    message=f"set_env only supports Deployment, StatefulSet, DaemonSet — not '{kind}'.",
                    error_type="unsupported_kind",
                )
                span.set_status(StatusCode.ERROR, description=output.message)
                tool_calls_total.labels(tool="set_env", status="error").inc()
                return output.model_dump_json()

            obj = _read(kind, name, namespace)
            containers = _get_containers(obj, kind)
            container, idx = _find_container(containers, container_name)

            if container is None:
                names = [c.name for c in containers]
                output = PatchOutput(
                    status="error",
                    message=(
                        f"Container '{container_name}' not found in {kind} '{name}'. "
                        f"Available containers: {names}"
                    ),
                    error_type="container_not_found",
                )
                span.set_status(StatusCode.ERROR, description=output.message)
                tool_calls_total.labels(tool="set_env", status="error").inc()
                return output.model_dump_json()

            resolved_name = container.name
            current_env_map = _env_list_to_map(container.env)

            proposed_env_map = dict(current_env_map)
            for k, v in env_vars.items():
                if v is None:
                    proposed_env_map.pop(k, None)
                else:
                    proposed_env_map[k] = v

            diff_text = _build_env_diff(current_env_map, proposed_env_map)
            new_env_list = _env_map_to_list(proposed_env_map)

            if dry_run:
                output = PatchOutput(
                    status="dry_run",
                    kind=kind, name=name, namespace=namespace,
                    container=resolved_name, diff=diff_text,
                    message=(
                        "DRY RUN — no changes applied. "
                        "Confirm with the user, then call set_env with dry_run=False to apply."
                    ),
                )
                span.set_status(StatusCode.OK)
                tool_calls_total.labels(tool="set_env", status="success").inc()
                return output.model_dump_json()

            if _containers_path(kind) == "pod":
                patch_body = {"spec": {"containers": [{"name": resolved_name, "env": new_env_list}]}}
            else:
                patch_body = {"spec": {"template": {"spec": {
                    "containers": [{"name": resolved_name, "env": new_env_list}]
                }}}}

            _patch(kind, name, namespace, patch_body, dry_run=False)
            logger.info(
                f"set_env applied: {kind}/{name} container={resolved_name} "
                f"namespace={namespace} changes={list(env_vars.keys())}"
            )

            output = PatchOutput(
                status="success",
                data={
                    "kind": kind, "name": name, "namespace": namespace,
                    "container": resolved_name, "applied_changes": diff_text,
                    "message": (
                        "Env vars updated. A rolling restart will begin automatically "
                        "to pick up the new values."
                    ),
                },
            )
            span.set_status(StatusCode.OK)
            tool_calls_total.labels(tool="set_env", status="success").inc()
            return output.model_dump_json()
        except Exception as e:
            span.set_status(StatusCode.ERROR, description=str(e))
            tool_calls_total.labels(tool="set_env", status="error").inc()
            raise


set_env_tool = StructuredTool.from_function(
    func=set_env,
    name="set_env",
    description=(
        "Add, update, or remove environment variables on a running Deployment, StatefulSet, "
        "or DaemonSet container. Uses strategic merge patch — only specified keys are touched. "
        "ALWAYS call with dry_run=True first to preview the diff. "
        "Only call with dry_run=False after the user explicitly confirms."
    ),
    args_schema=SetEnvInput,
)


# ── patch_resource ────────────────────────────────────────────────────────────

@_handle_k8s_exceptions
def patch_resource(
    namespace: str,
    kind: str,
    name: str,
    patch_body: Optional[Dict[str, Any]] = None,
    dry_run: bool = True,
) -> str:
    """Apply a strategic merge patch to any common namespaced Kubernetes resource.

    Two-step HITL:
      dry_run=True  → server-side validate + preview result, no write.
      dry_run=False → apply after user confirms the preview.
    """
    with _tracer.start_as_current_span("patch_resource") as span:
        span.set_attribute("k8s.namespace", namespace)
        span.set_attribute("k8s.resource.kind", kind)
        span.set_attribute("k8s.resource.name", name)
        span.set_attribute("tool.dry_run", dry_run)
        try:
            if not patch_body:
                output = PatchOutput(status="error", message="patch_body must be a non-empty dict.",
                                     error_type="invalid_input")
                span.set_status(StatusCode.ERROR, description=output.message)
                tool_calls_total.labels(tool="patch_resource", status="error").inc()
                return output.model_dump_json()

            kind_lower = kind.lower()
            if kind_lower not in _ALL_KINDS:
                output = PatchOutput(
                    status="error",
                    message=f"Unsupported kind '{kind}'. Supported: {SUPPORTED_KINDS}",
                    error_type="unsupported_kind",
                )
                span.set_status(StatusCode.ERROR, description=output.message)
                tool_calls_total.labels(tool="patch_resource", status="error").inc()
                return output.model_dump_json()

            if dry_run:
                try:
                    projected = _patch(kind, name, namespace, patch_body, dry_run=True)
                    projected_dict = projected.to_dict() if hasattr(projected, "to_dict") else {}
                    output = PatchOutput(
                        status="dry_run",
                        kind=kind, name=name, namespace=namespace,
                        patch_body=patch_body,
                        server_validation="passed",
                        projected_resource_version=(
                            projected_dict.get("metadata", {}).get("resource_version", "N/A")
                            if projected_dict else "N/A"
                        ),
                        message=(
                            "DRY RUN — server-side validation passed, no changes applied. "
                            f"Patch body: {json.dumps(patch_body, indent=2)}. "
                            "Confirm with the user, then call patch_resource with dry_run=False to apply."
                        ),
                    )
                    span.set_status(StatusCode.OK)
                    tool_calls_total.labels(tool="patch_resource", status="success").inc()
                    return output.model_dump_json()
                except ApiException as e:
                    output = PatchOutput(
                        status="dry_run_failed",
                        kind=kind, name=name, namespace=namespace,
                        patch_body=patch_body,
                        error=f"Server-side validation failed: {e.status} {e.reason} — {e.body}",
                        message=(
                            "DRY RUN failed validation — the patch body is rejected by the API server. "
                            "Fix the patch body before applying."
                        ),
                    )
                    span.set_status(StatusCode.ERROR, description=output.error)
                    tool_calls_total.labels(tool="patch_resource", status="error").inc()
                    return output.model_dump_json()

            result = _patch(kind, name, namespace, patch_body, dry_run=False)
            logger.info(
                f"patch_resource applied: {kind}/{name} namespace={namespace} "
                f"patch_keys={list(patch_body.keys())}"
            )

            output = PatchOutput(
                status="success",
                data={
                    "kind": kind, "name": name, "namespace": namespace,
                    "applied_patch": patch_body,
                    "resource_version": (
                        result.metadata.resource_version if result.metadata else "N/A"
                    ),
                    "message": f"Patch applied successfully to {kind} '{name}'.",
                },
            )
            span.set_status(StatusCode.OK)
            tool_calls_total.labels(tool="patch_resource", status="success").inc()
            return output.model_dump_json()
        except Exception as e:
            span.set_status(StatusCode.ERROR, description=str(e))
            tool_calls_total.labels(tool="patch_resource", status="error").inc()
            raise


patch_resource_tool = StructuredTool.from_function(
    func=patch_resource,
    name="patch_resource",
    description=(
        "Apply a strategic merge patch to any common namespaced Kubernetes resource "
        f"({', '.join(SUPPORTED_KINDS)}). "
        "dry_run=True (default) runs server-side validation and returns the projected result "
        "WITHOUT applying — always do this first. "
        "Only call with dry_run=False after the user explicitly confirms the preview."
    ),
    args_schema=PatchResourceInput,
)


# ── label_resource ────────────────────────────────────────────────────────────

@_handle_k8s_exceptions
def label_resource(
    namespace: str,
    kind: str,
    name: str,
    labels: Optional[Dict[str, Optional[str]]] = None,
    dry_run: bool = True,
) -> str:
    """Add, update, or remove labels on any common namespaced Kubernetes resource.

    Two-step HITL:
      dry_run=True  → show diff of current vs. proposed labels, no write.
      dry_run=False → apply after user confirmation.
    """
    with _tracer.start_as_current_span("label_resource") as span:
        span.set_attribute("k8s.namespace", namespace)
        span.set_attribute("k8s.resource.kind", kind)
        span.set_attribute("k8s.resource.name", name)
        span.set_attribute("tool.dry_run", dry_run)
        try:
            if not labels:
                output = PatchOutput(status="error", message="labels must be a non-empty dict.",
                                     error_type="invalid_input")
                span.set_status(StatusCode.ERROR, description=output.message)
                tool_calls_total.labels(tool="label_resource", status="error").inc()
                return output.model_dump_json()

            kind_lower = kind.lower()
            if kind_lower not in _ALL_KINDS:
                output = PatchOutput(
                    status="error",
                    message=f"Unsupported kind '{kind}'. Supported: {SUPPORTED_KINDS}",
                    error_type="unsupported_kind",
                )
                span.set_status(StatusCode.ERROR, description=output.message)
                tool_calls_total.labels(tool="label_resource", status="error").inc()
                return output.model_dump_json()

            obj = _read(kind, name, namespace)
            current_labels: dict = obj.metadata.labels or {} if obj.metadata else {}

            proposed_labels = dict(current_labels)
            for k, v in labels.items():
                if v is None:
                    proposed_labels.pop(k, None)
                else:
                    proposed_labels[k] = v

            diff_text = _build_kv_diff("labels", current_labels, proposed_labels)

            if dry_run:
                output = PatchOutput(
                    status="dry_run",
                    kind=kind, name=name, namespace=namespace,
                    diff=diff_text,
                    current_labels=current_labels,
                    proposed_labels=proposed_labels,
                    message=(
                        "DRY RUN — no changes applied. "
                        "Confirm with the user, then call label_resource with dry_run=False to apply."
                    ),
                )
                span.set_status(StatusCode.OK)
                tool_calls_total.labels(tool="label_resource", status="success").inc()
                return output.model_dump_json()

            label_patch: dict[str, Any] = {k: v for k, v in labels.items()}
            patch_body = {"metadata": {"labels": label_patch}}
            _patch(kind, name, namespace, patch_body, dry_run=False)
            logger.info(
                f"label_resource applied: {kind}/{name} namespace={namespace} "
                f"label_keys={list(labels.keys())}"
            )

            output = PatchOutput(
                status="success",
                data={
                    "kind": kind, "name": name, "namespace": namespace,
                    "applied_changes": diff_text,
                    "resulting_labels": proposed_labels,
                    "message": f"Labels updated on {kind} '{name}'.",
                },
            )
            span.set_status(StatusCode.OK)
            tool_calls_total.labels(tool="label_resource", status="success").inc()
            return output.model_dump_json()
        except Exception as e:
            span.set_status(StatusCode.ERROR, description=str(e))
            tool_calls_total.labels(tool="label_resource", status="error").inc()
            raise


label_resource_tool = StructuredTool.from_function(
    func=label_resource,
    name="label_resource",
    description=(
        "Add, update, or remove labels on any common namespaced Kubernetes resource. "
        "Pass null as a value to remove a label. "
        "ALWAYS call with dry_run=True first to preview the diff. "
        "Only call with dry_run=False after the user explicitly confirms."
    ),
    args_schema=LabelAnnotateInput,
)


# ── annotate_resource ─────────────────────────────────────────────────────────

@_handle_k8s_exceptions
def annotate_resource(
    namespace: str,
    kind: str,
    name: str,
    annotations: Optional[Dict[str, Optional[str]]] = None,
    dry_run: bool = True,
) -> str:
    """Add, update, or remove annotations on any common namespaced Kubernetes resource.

    Two-step HITL:
      dry_run=True  → show diff of current vs. proposed annotations, no write.
      dry_run=False → apply after user confirmation.
    """
    with _tracer.start_as_current_span("annotate_resource") as span:
        span.set_attribute("k8s.namespace", namespace)
        span.set_attribute("k8s.resource.kind", kind)
        span.set_attribute("k8s.resource.name", name)
        span.set_attribute("tool.dry_run", dry_run)
        try:
            if not annotations:
                output = PatchOutput(status="error", message="annotations must be a non-empty dict.",
                                     error_type="invalid_input")
                span.set_status(StatusCode.ERROR, description=output.message)
                tool_calls_total.labels(tool="annotate_resource", status="error").inc()
                return output.model_dump_json()

            kind_lower = kind.lower()
            if kind_lower not in _ALL_KINDS:
                output = PatchOutput(
                    status="error",
                    message=f"Unsupported kind '{kind}'. Supported: {SUPPORTED_KINDS}",
                    error_type="unsupported_kind",
                )
                span.set_status(StatusCode.ERROR, description=output.message)
                tool_calls_total.labels(tool="annotate_resource", status="error").inc()
                return output.model_dump_json()

            obj = _read(kind, name, namespace)
            current_annotations: dict = obj.metadata.annotations or {} if obj.metadata else {}

            _K8S_INTERNAL_PREFIXES = (
                "kubectl.kubernetes.io/",
                "deployment.kubernetes.io/",
                "kubernetes.io/",
            )
            display_current = {
                k: v for k, v in current_annotations.items()
                if not any(k.startswith(p) for p in _K8S_INTERNAL_PREFIXES)
            }

            proposed_annotations = dict(current_annotations)
            for k, v in annotations.items():
                if v is None:
                    proposed_annotations.pop(k, None)
                else:
                    proposed_annotations[k] = v

            display_proposed = {
                k: v for k, v in proposed_annotations.items()
                if not any(k.startswith(p) for p in _K8S_INTERNAL_PREFIXES)
            }

            diff_text = _build_kv_diff("annotations", display_current, display_proposed)

            if dry_run:
                output = PatchOutput(
                    status="dry_run",
                    kind=kind, name=name, namespace=namespace,
                    diff=diff_text,
                    note="Internal Kubernetes annotations (kubectl.kubernetes.io/, etc.) are hidden from the diff.",
                    message=(
                        "DRY RUN — no changes applied. "
                        "Confirm with the user, then call annotate_resource with dry_run=False to apply."
                    ),
                )
                span.set_status(StatusCode.OK)
                tool_calls_total.labels(tool="annotate_resource", status="success").inc()
                return output.model_dump_json()

            annotation_patch: dict[str, Any] = {k: v for k, v in annotations.items()}
            patch_body = {"metadata": {"annotations": annotation_patch}}
            _patch(kind, name, namespace, patch_body, dry_run=False)
            logger.info(
                f"annotate_resource applied: {kind}/{name} namespace={namespace} "
                f"annotation_keys={list(annotations.keys())}"
            )

            output = PatchOutput(
                status="success",
                data={
                    "kind": kind, "name": name, "namespace": namespace,
                    "applied_changes": diff_text,
                    "message": f"Annotations updated on {kind} '{name}'.",
                },
            )
            span.set_status(StatusCode.OK)
            tool_calls_total.labels(tool="annotate_resource", status="success").inc()
            return output.model_dump_json()
        except Exception as e:
            span.set_status(StatusCode.ERROR, description=str(e))
            tool_calls_total.labels(tool="annotate_resource", status="error").inc()
            raise


annotate_resource_tool = StructuredTool.from_function(
    func=annotate_resource,
    name="annotate_resource",
    description=(
        "Add, update, or remove annotations on any common namespaced Kubernetes resource. "
        "Pass null as a value to remove an annotation. "
        "ALWAYS call with dry_run=True first to preview the diff. "
        "Only call with dry_run=False after the user explicitly confirms."
    ),
    args_schema=AnnotateInput,
)


# ── Exported list ─────────────────────────────────────────────────────────────

patch_tools = [
    set_env_tool,
    patch_resource_tool,
    label_resource_tool,
    annotate_resource_tool,
]
