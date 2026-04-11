from typing import Dict, Any, List, Optional

from langchain_core.tools import StructuredTool
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, Field

from app.agents.tools.tools_lib._base import (
    get_apps_v1_api,
    get_core_v1_api,
    get_networking_v1_api,
    _handle_k8s_exceptions,
    NamespaceInputSchema,
    NamespaceOptionalInputSchema,
)
from app.agents.tools.tools_lib.pod_tools import resolve_pod
from app.utils.logger_config import setup_logging
from app.utils.metrics import tool_calls_total

logger = setup_logging(app_name="kubeintellect")
_tracer = trace.get_tracer("kubeintellect.tools")


# ===============================================================================
#                               INPUT SCHEMAS
# ===============================================================================

class SecretListInputSchema(BaseModel):
    namespace: str = Field(description="The Kubernetes namespace to list secrets in.")
    include_data_keys: bool = Field(
        default=False,
        description="If True, include the key names (not values) stored in each Secret.",
    )


# ===============================================================================
#                           PRIVILEGED / HOST ACCESS CHECKS
# ===============================================================================

@_handle_k8s_exceptions
def check_privileged_pods(namespace: Optional[str] = None) -> Dict[str, Any]:
    """Finds pods that run at least one container with privileged: true."""
    core_v1 = get_core_v1_api()
    if namespace:
        pods = core_v1.list_namespaced_pod(namespace=namespace, timeout_seconds=10)
    else:
        pods = core_v1.list_pod_for_all_namespaces(timeout_seconds=10)

    flagged = []
    for pod in pods.items:
        privileged_containers = []
        for container in (pod.spec.containers or []) + (pod.spec.init_containers or []):
            sc = container.security_context
            if sc and sc.privileged:
                privileged_containers.append(container.name)
        if privileged_containers:
            flagged.append({
                "name": pod.metadata.name,
                "namespace": pod.metadata.namespace,
                "privileged_containers": privileged_containers,
            })

    return {
        "status": "success",
        "data": flagged,
        "summary": f"{len(flagged)} pod(s) with privileged containers found.",
    }


check_privileged_pods_tool = StructuredTool.from_function(
    func=check_privileged_pods,
    name="check_privileged_pods",
    description=(
        "Scans pods for containers running with privileged: true. "
        "Optionally scoped to a namespace; omit namespace to check the whole cluster."
    ),
    args_schema=NamespaceOptionalInputSchema,
)


@_handle_k8s_exceptions
def check_pods_with_host_network(namespace: Optional[str] = None) -> Dict[str, Any]:
    """Finds pods that use the host network namespace (hostNetwork: true)."""
    core_v1 = get_core_v1_api()
    if namespace:
        pods = core_v1.list_namespaced_pod(namespace=namespace, timeout_seconds=10)
    else:
        pods = core_v1.list_pod_for_all_namespaces(timeout_seconds=10)

    flagged = [
        {"name": pod.metadata.name, "namespace": pod.metadata.namespace}
        for pod in pods.items
        if pod.spec.host_network
    ]
    return {
        "status": "success",
        "data": flagged,
        "summary": f"{len(flagged)} pod(s) using host network found.",
    }


check_pods_with_host_network_tool = StructuredTool.from_function(
    func=check_pods_with_host_network,
    name="check_pods_with_host_network",
    description=(
        "Finds pods running with hostNetwork: true, which grants direct access to the node's network stack. "
        "Optionally scoped to a namespace."
    ),
    args_schema=NamespaceOptionalInputSchema,
)


@_handle_k8s_exceptions
def check_pods_with_host_pid(namespace: Optional[str] = None) -> Dict[str, Any]:
    """Finds pods that share the host PID namespace (hostPID: true)."""
    core_v1 = get_core_v1_api()
    if namespace:
        pods = core_v1.list_namespaced_pod(namespace=namespace, timeout_seconds=10)
    else:
        pods = core_v1.list_pod_for_all_namespaces(timeout_seconds=10)

    flagged = [
        {"name": pod.metadata.name, "namespace": pod.metadata.namespace}
        for pod in pods.items
        if pod.spec.host_pid
    ]
    return {
        "status": "success",
        "data": flagged,
        "summary": f"{len(flagged)} pod(s) using hostPID found.",
    }


check_pods_with_host_pid_tool = StructuredTool.from_function(
    func=check_pods_with_host_pid,
    name="check_pods_with_host_pid",
    description=(
        "Finds pods running with hostPID: true, which allows the container to see all processes on the host. "
        "Optionally scoped to a namespace."
    ),
    args_schema=NamespaceOptionalInputSchema,
)


# ===============================================================================
#                          SENSITIVE MOUNT / FILESYSTEM CHECKS
# ===============================================================================

_SENSITIVE_HOST_PATHS = {"/", "/etc", "/var/run/docker.sock", "/proc", "/sys", "/root", "/host"}


@_handle_k8s_exceptions
def list_pods_with_sensitive_mounts(namespace: Optional[str] = None) -> Dict[str, Any]:
    """Finds pods that mount sensitive host paths (e.g. /, /etc, /proc, docker socket)."""
    core_v1 = get_core_v1_api()
    if namespace:
        pods = core_v1.list_namespaced_pod(namespace=namespace, timeout_seconds=10)
    else:
        pods = core_v1.list_pod_for_all_namespaces(timeout_seconds=10)

    flagged = []
    for pod in pods.items:
        sensitive = []
        for volume in (pod.spec.volumes or []):
            if volume.host_path and volume.host_path.path in _SENSITIVE_HOST_PATHS:
                sensitive.append(volume.host_path.path)
        if sensitive:
            flagged.append({
                "name": pod.metadata.name,
                "namespace": pod.metadata.namespace,
                "sensitive_paths": sensitive,
            })

    return {
        "status": "success",
        "data": flagged,
        "summary": f"{len(flagged)} pod(s) with sensitive host path mounts found.",
    }


list_pods_with_sensitive_mounts_tool = StructuredTool.from_function(
    func=list_pods_with_sensitive_mounts,
    name="list_pods_with_sensitive_mounts",
    description=(
        "Finds pods that mount sensitive host paths (such as /, /etc, /proc, or the Docker socket). "
        "Optionally scoped to a namespace."
    ),
    args_schema=NamespaceOptionalInputSchema,
)


@_handle_k8s_exceptions
def check_containers_without_readonly_rootfs(namespace: Optional[str] = None) -> Dict[str, Any]:
    """Finds containers that do NOT set readOnlyRootFilesystem: true."""
    core_v1 = get_core_v1_api()
    if namespace:
        pods = core_v1.list_namespaced_pod(namespace=namespace, timeout_seconds=10)
    else:
        pods = core_v1.list_pod_for_all_namespaces(timeout_seconds=10)

    flagged = []
    for pod in pods.items:
        writable_containers = []
        for container in (pod.spec.containers or []):
            sc = container.security_context
            if not (sc and sc.read_only_root_filesystem):
                writable_containers.append(container.name)
        if writable_containers:
            flagged.append({
                "name": pod.metadata.name,
                "namespace": pod.metadata.namespace,
                "writable_containers": writable_containers,
            })

    return {
        "status": "success",
        "data": flagged,
        "summary": f"{len(flagged)} pod(s) have containers without readOnlyRootFilesystem.",
    }


check_containers_without_readonly_rootfs_tool = StructuredTool.from_function(
    func=check_containers_without_readonly_rootfs,
    name="check_containers_without_readonly_rootfs",
    description=(
        "Finds containers that do not set readOnlyRootFilesystem: true in their securityContext. "
        "Optionally scoped to a namespace."
    ),
    args_schema=NamespaceOptionalInputSchema,
)


# ===============================================================================
#                              RESOURCE LIMITS CHECK
# ===============================================================================

@_handle_k8s_exceptions
def check_pods_without_resource_limits(namespace: Optional[str] = None) -> Dict[str, Any]:
    """Finds containers that have no CPU or memory limits set."""
    core_v1 = get_core_v1_api()
    if namespace:
        pods = core_v1.list_namespaced_pod(namespace=namespace, timeout_seconds=10)
    else:
        pods = core_v1.list_pod_for_all_namespaces(timeout_seconds=10)

    flagged = []
    for pod in pods.items:
        unlimited = []
        for container in (pod.spec.containers or []):
            missing = []
            if not (container.resources and container.resources.limits):
                missing.append("no limits set")
            elif "cpu" not in (container.resources.limits or {}):
                missing.append("no cpu limit")
            elif "memory" not in (container.resources.limits or {}):
                missing.append("no memory limit")
            if missing:
                unlimited.append({"container": container.name, "issues": missing})
        if unlimited:
            flagged.append({
                "name": pod.metadata.name,
                "namespace": pod.metadata.namespace,
                "containers": unlimited,
            })

    return {
        "status": "success",
        "data": flagged,
        "summary": f"{len(flagged)} pod(s) have containers without full resource limits.",
    }


check_pods_without_resource_limits_tool = StructuredTool.from_function(
    func=check_pods_without_resource_limits,
    name="check_pods_without_resource_limits",
    description=(
        "Finds containers that are missing CPU or memory limits, which can lead to resource exhaustion. "
        "Optionally scoped to a namespace."
    ),
    args_schema=NamespaceOptionalInputSchema,
)


# ===============================================================================
#                           NETWORK POLICY COVERAGE CHECK
# ===============================================================================

@_handle_k8s_exceptions
def check_network_policies_coverage(namespace: str) -> Dict[str, Any]:
    """Reports which pods in a namespace are NOT covered by any NetworkPolicy."""
    core_v1 = get_core_v1_api()
    net_v1 = get_networking_v1_api()

    pods = core_v1.list_namespaced_pod(namespace=namespace, timeout_seconds=10)
    policies = net_v1.list_namespaced_network_policy(namespace=namespace, timeout_seconds=10)

    policy_selectors = []
    for policy in policies.items:
        sel = policy.spec.pod_selector
        policy_selectors.append(sel.match_labels or {})

    def is_covered(pod_labels: dict) -> bool:
        if not pod_labels:
            return False
        for selector_labels in policy_selectors:
            if not selector_labels:
                # empty selector matches all pods
                return True
            if all(pod_labels.get(k) == v for k, v in selector_labels.items()):
                return True
        return False

    uncovered = []
    for pod in pods.items:
        labels = pod.metadata.labels or {}
        if not is_covered(labels):
            uncovered.append({
                "name": pod.metadata.name,
                "labels": labels,
            })

    return {
        "status": "success",
        "data": {
            "namespace": namespace,
            "total_pods": len(pods.items),
            "total_policies": len(policies.items),
            "uncovered_pods": uncovered,
        },
        "summary": (
            f"{len(uncovered)}/{len(pods.items)} pods in '{namespace}' are not covered by any NetworkPolicy."
        ),
    }


check_network_policies_coverage_tool = StructuredTool.from_function(
    func=check_network_policies_coverage,
    name="check_network_policies_coverage",
    description=(
        "Reports which pods in a namespace are not selected by any NetworkPolicy. "
        "Uncovered pods can send and receive unrestricted traffic."
    ),
    args_schema=NamespaceInputSchema,
)


# ===============================================================================
#                                SECRETS LISTING
# ===============================================================================

@_handle_k8s_exceptions
def list_secrets_in_namespace(namespace: str, include_data_keys: bool = False) -> Dict[str, Any]:
    """Lists all Secrets in a namespace. Optionally includes key names (never values)."""
    core_v1 = get_core_v1_api()
    secrets = core_v1.list_namespaced_secret(namespace=namespace, timeout_seconds=10)

    result = []
    for secret in secrets.items:
        entry = {
            "name": secret.metadata.name,
            "namespace": secret.metadata.namespace,
            "type": secret.type,
            "creation_timestamp": (
                secret.metadata.creation_timestamp.isoformat()
                if secret.metadata.creation_timestamp else None
            ),
        }
        if include_data_keys:
            entry["data_keys"] = list(secret.data.keys()) if secret.data else []
        result.append(entry)

    return {"status": "success", "data": result}


list_secrets_in_namespace_tool = StructuredTool.from_function(
    func=list_secrets_in_namespace,
    name="list_secrets_in_namespace",
    description=(
        "Lists all Secrets in a Kubernetes namespace. "
        "Shows name, type, and creation timestamp. "
        "Set include_data_keys=true to also list key names (values are never returned)."
    ),
    args_schema=SecretListInputSchema,
)


# ===============================================================================
#                               SECRET EXISTS
# ===============================================================================

_DOCKERCONFIGJSON_TYPE = "kubernetes.io/dockerconfigjson"


class SecretExistsInput(BaseModel):
    namespace: str = Field(description="Kubernetes namespace to look in.")
    secret_name: str = Field(description="Name of the Secret to check.")


class SecretExistsOutput(BaseModel):
    status: str
    namespace: str
    secret_name: str
    exists: bool
    secret_type: Optional[str] = None
    key_names: Optional[List[str]] = None  # key names only — never values
    created_at: Optional[str] = None
    error_type: Optional[str] = None
    message: Optional[str] = None


@_handle_k8s_exceptions
def secret_exists(namespace: str, secret_name: str) -> str:
    """Check whether a specific Secret exists in a namespace.

    Returns key names only — never values, never base64-encoded content.
    """
    with _tracer.start_as_current_span("secret_exists") as span:
        try:
            from kubernetes.client.exceptions import ApiException
            core_v1 = get_core_v1_api()
            try:
                secret = core_v1.read_namespaced_secret(name=secret_name, namespace=namespace)
            except ApiException as e:
                if e.status == 404:
                    output = SecretExistsOutput(
                        status="success",
                        namespace=namespace,
                        secret_name=secret_name,
                        exists=False,
                        message="Secret not found.",
                    )
                    span.set_status(StatusCode.OK)
                    tool_calls_total.labels(tool="secret_exists", status="success").inc()
                    return output.model_dump_json()
                raise

            # Enforce key-names-only: never expose values
            key_names = sorted(secret.data.keys()) if secret.data else []
            created_at = (
                secret.metadata.creation_timestamp.isoformat()
                if secret.metadata.creation_timestamp else None
            )
            output = SecretExistsOutput(
                status="success",
                namespace=namespace,
                secret_name=secret_name,
                exists=True,
                secret_type=secret.type,
                key_names=key_names,
                created_at=created_at,
            )
            span.set_status(StatusCode.OK)
            tool_calls_total.labels(tool="secret_exists", status="success").inc()
            return output.model_dump_json()
        except Exception as e:
            span.set_status(StatusCode.ERROR, description=str(e))
            raise


secret_exists_tool = StructuredTool.from_function(
    func=secret_exists,
    name="secret_exists",
    description=(
        "Check whether a specific Kubernetes Secret exists in a namespace. "
        "Returns key names only — never values or base64-encoded content. "
        "Use to verify a secret is present before debugging ImagePullBackOff or missing credentials."
    ),
    args_schema=SecretExistsInput,
)


# ===============================================================================
#                          IMAGE PULL SECRET CHECK
# ===============================================================================

class SecretFinding(BaseModel):
    secret_name: str
    exists: bool
    secret_type: Optional[str] = None
    is_correct_type: bool
    issue: Optional[str] = None


class ImagePullSecretCheckInput(BaseModel):
    namespace: str = Field(description="Kubernetes namespace containing the Deployment.")
    deployment_name: str = Field(description="Name of the Deployment to inspect.")


class ImagePullSecretCheckOutput(BaseModel):
    status: str
    namespace: str
    deployment_name: str
    image_pull_secrets_configured: List[str]
    findings: List[SecretFinding]
    all_valid: bool
    summary: str
    error_type: Optional[str] = None
    message: Optional[str] = None


@_handle_k8s_exceptions
def image_pull_secret_check(namespace: str, deployment_name: str) -> str:
    """Check the imagePullSecrets on a Deployment.

    Cross-references each configured secret name against Secrets that actually
    exist in the namespace, and verifies the type is kubernetes.io/dockerconfigjson.
    Helps diagnose ImagePullBackOff and ErrImagePull failures.
    """
    with _tracer.start_as_current_span("image_pull_secret_check") as span:
        try:
            from kubernetes.client.exceptions import ApiException
            apps_v1 = get_apps_v1_api()
            core_v1 = get_core_v1_api()

            try:
                deployment = apps_v1.read_namespaced_deployment(
                    name=deployment_name, namespace=namespace
                )
            except ApiException as e:
                if e.status == 404:
                    output = ImagePullSecretCheckOutput(
                        status="error",
                        namespace=namespace,
                        deployment_name=deployment_name,
                        image_pull_secrets_configured=[],
                        findings=[],
                        all_valid=False,
                        summary=f"Deployment '{deployment_name}' not found in namespace '{namespace}'.",
                        error_type="not_found",
                        message=f"Deployment '{deployment_name}' not found.",
                    )
                    span.set_status(StatusCode.ERROR, description=output.message)
                    tool_calls_total.labels(tool="image_pull_secret_check", status="error").inc()
                    return output.model_dump_json()
                raise

            spec = deployment.spec.template.spec
            raw_pull_secrets = spec.image_pull_secrets or []
            configured_names = [s.name for s in raw_pull_secrets]

            findings: List[SecretFinding] = []
            for sname in configured_names:
                try:
                    secret = core_v1.read_namespaced_secret(name=sname, namespace=namespace)
                    is_correct = secret.type == _DOCKERCONFIGJSON_TYPE
                    issue: Optional[str] = None
                    if not is_correct:
                        issue = (
                            f"Wrong type: '{secret.type}'. "
                            f"Expected '{_DOCKERCONFIGJSON_TYPE}'."
                        )
                    findings.append(SecretFinding(
                        secret_name=sname,
                        exists=True,
                        secret_type=secret.type,
                        is_correct_type=is_correct,
                        issue=issue,
                    ))
                except ApiException as e:
                    if e.status == 404:
                        findings.append(SecretFinding(
                            secret_name=sname,
                            exists=False,
                            is_correct_type=False,
                            issue=f"Secret '{sname}' does not exist in namespace '{namespace}'.",
                        ))
                    else:
                        raise

            all_valid = bool(configured_names) and all(
                f.exists and f.is_correct_type for f in findings
            )

            if not configured_names:
                summary = (
                    f"Deployment '{deployment_name}' has no imagePullSecrets configured."
                )
            elif all_valid:
                summary = (
                    f"All {len(findings)} imagePullSecret(s) for '{deployment_name}' "
                    f"exist and are of the correct type."
                )
            else:
                issues = [f.issue for f in findings if f.issue]
                summary = (
                    f"Found {len(issues)} issue(s) with imagePullSecrets for "
                    f"'{deployment_name}': " + "; ".join(issues)
                )

            output = ImagePullSecretCheckOutput(
                status="success",
                namespace=namespace,
                deployment_name=deployment_name,
                image_pull_secrets_configured=configured_names,
                findings=findings,
                all_valid=all_valid,
                summary=summary,
            )
            span.set_status(StatusCode.OK)
            tool_calls_total.labels(tool="image_pull_secret_check", status="success").inc()
            return output.model_dump_json()
        except Exception as e:
            span.set_status(StatusCode.ERROR, description=str(e))
            raise


image_pull_secret_check_tool = StructuredTool.from_function(
    func=image_pull_secret_check,
    name="image_pull_secret_check",
    description=(
        "Inspect a Deployment's imagePullSecrets: verify each referenced Secret exists "
        "and has type kubernetes.io/dockerconfigjson. "
        "Primary diagnostic for ImagePullBackOff and ErrImagePull failures."
    ),
    args_schema=ImagePullSecretCheckInput,
)


# ===============================================================================
#                          NETWORK POLICY AUDIT
# ===============================================================================

class NetworkPolicyRule(BaseModel):
    from_or_to: List[Dict[str, Any]]  # list of peer selectors (namespaceSelector, podSelector, ipBlock)
    ports: List[Dict[str, Any]]


class NetworkPolicyMatch(BaseModel):
    policy_name: str
    policy_types: List[str]      # Ingress, Egress, or both
    ingress_rules: Optional[List[NetworkPolicyRule]] = None
    egress_rules: Optional[List[NetworkPolicyRule]] = None


class NetworkPolicyAuditInput(BaseModel):
    namespace: str = Field(description="Kubernetes namespace containing the pod.")
    pod_name: str = Field(description="Name of the pod to audit network policies for.")


class NetworkPolicyAuditOutput(BaseModel):
    status: str
    namespace: str
    pod_name: str
    pod_labels: Optional[Dict[str, str]] = None
    matching_policies: Optional[List[NetworkPolicyMatch]] = None
    ingress_covered: Optional[bool] = None
    egress_covered: Optional[bool] = None
    total_matching: Optional[int] = None
    summary: Optional[str] = None
    error_type: Optional[str] = None
    message: Optional[str] = None


def _labels_match_selector(pod_labels: Dict[str, str], selector) -> bool:
    """Return True if pod_labels satisfy selector.match_labels (matchExpressions not evaluated)."""
    if selector is None:
        return True
    match_labels = getattr(selector, "match_labels", None) or {}
    if not match_labels:
        return True  # empty selector matches everything
    return all(pod_labels.get(k) == v for k, v in match_labels.items())


def _peer_to_dict(peer) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    if getattr(peer, "pod_selector", None) is not None:
        ps = peer.pod_selector
        result["podSelector"] = {
            "matchLabels": getattr(ps, "match_labels", None) or {}
        }
    if getattr(peer, "namespace_selector", None) is not None:
        ns_sel = peer.namespace_selector
        result["namespaceSelector"] = {
            "matchLabels": getattr(ns_sel, "match_labels", None) or {}
        }
    if getattr(peer, "ip_block", None) is not None:
        ib = peer.ip_block
        result["ipBlock"] = {"cidr": ib.cidr, "except": ib._except or []}
    return result


def _port_to_dict(port) -> Dict[str, Any]:
    return {
        "protocol": getattr(port, "protocol", None) or "TCP",
        "port": str(getattr(port, "port", None) or ""),
    }


@_handle_k8s_exceptions
def network_policy_audit(namespace: str, pod_name: str) -> str:
    """Audit which NetworkPolicies apply to a specific pod.

    Inverse of check_network_policies_coverage: given a pod, return every
    NetworkPolicy whose pod selector matches it, with human-readable ingress and
    egress rules. Helps diagnose connectivity failures.
    """
    with _tracer.start_as_current_span("network_policy_audit") as span:
        try:
            from kubernetes.client.exceptions import ApiException
            core_v1 = get_core_v1_api()
            net_v1 = get_networking_v1_api()

            try:
                pod = resolve_pod(core_v1, pod_name=pod_name, namespace=namespace)
            except ApiException as e:
                if e.status == 404:
                    output = NetworkPolicyAuditOutput(
                        status="error",
                        namespace=namespace,
                        pod_name=pod_name,
                        error_type="not_found",
                        message=f"Pod '{pod_name}' not found in namespace '{namespace}'.",
                    )
                    span.set_status(StatusCode.ERROR, description=output.message)
                    tool_calls_total.labels(tool="network_policy_audit", status="error").inc()
                    return output.model_dump_json()
                raise

            pod_labels: Dict[str, str] = pod.metadata.labels or {}
            policies = net_v1.list_namespaced_network_policy(
                namespace=namespace, timeout_seconds=10
            )

            matches: List[NetworkPolicyMatch] = []
            for policy in policies.items:
                if not _labels_match_selector(pod_labels, policy.spec.pod_selector):
                    continue

                policy_types: List[str] = list(policy.spec.policy_types or [])
                if not policy_types:
                    # Default: if ingress rules present → Ingress; always Ingress if types omitted
                    policy_types = ["Ingress"]

                ingress_rules: Optional[List[NetworkPolicyRule]] = None
                if "Ingress" in policy_types and policy.spec.ingress is not None:
                    ingress_rules = []
                    for rule in (policy.spec.ingress or []):
                        ingress_rules.append(NetworkPolicyRule(
                            from_or_to=[_peer_to_dict(p) for p in (rule._from or [])],
                            ports=[_port_to_dict(p) for p in (rule.ports or [])],
                        ))

                egress_rules: Optional[List[NetworkPolicyRule]] = None
                if "Egress" in policy_types and policy.spec.egress is not None:
                    egress_rules = []
                    for rule in (policy.spec.egress or []):
                        egress_rules.append(NetworkPolicyRule(
                            from_or_to=[_peer_to_dict(p) for p in (rule.to or [])],
                            ports=[_port_to_dict(p) for p in (rule.ports or [])],
                        ))

                matches.append(NetworkPolicyMatch(
                    policy_name=policy.metadata.name,
                    policy_types=policy_types,
                    ingress_rules=ingress_rules,
                    egress_rules=egress_rules,
                ))

            ingress_covered = any("Ingress" in m.policy_types for m in matches)
            egress_covered = any("Egress" in m.policy_types for m in matches)

            if not matches:
                summary = (
                    f"No NetworkPolicies select pod '{pod_name}' — "
                    f"all ingress and egress traffic is unrestricted."
                )
            else:
                summary = (
                    f"{len(matches)} NetworkPolicy(ies) apply to '{pod_name}'. "
                    f"Ingress covered: {ingress_covered}. Egress covered: {egress_covered}."
                )

            output = NetworkPolicyAuditOutput(
                status="success",
                namespace=namespace,
                pod_name=pod_name,
                pod_labels=pod_labels,
                matching_policies=matches,
                ingress_covered=ingress_covered,
                egress_covered=egress_covered,
                total_matching=len(matches),
                summary=summary,
            )
            span.set_status(StatusCode.OK)
            tool_calls_total.labels(tool="network_policy_audit", status="success").inc()
            return output.model_dump_json()
        except Exception as e:
            span.set_status(StatusCode.ERROR, description=str(e))
            raise


network_policy_audit_tool = StructuredTool.from_function(
    func=network_policy_audit,
    name="network_policy_audit",
    description=(
        "Given a specific pod, return every NetworkPolicy that selects it, "
        "with human-readable ingress and egress rules. "
        "Use to diagnose connectivity failures: 'why can't my pod reach service X?'"
    ),
    args_schema=NetworkPolicyAuditInput,
)


# ===============================================================================
#                           TOOL COLLECTION
# ===============================================================================

security_tools = [
    check_privileged_pods_tool,
    check_pods_with_host_network_tool,
    check_pods_with_host_pid_tool,
    list_pods_with_sensitive_mounts_tool,
    check_containers_without_readonly_rootfs_tool,
    check_pods_without_resource_limits_tool,
    check_network_policies_coverage_tool,
    list_secrets_in_namespace_tool,
    secret_exists_tool,
    image_pull_secret_check_tool,
    network_policy_audit_tool,
]
