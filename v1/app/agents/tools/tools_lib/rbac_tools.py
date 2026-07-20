from typing import Dict, Any, List, Optional

from langchain_core.tools import StructuredTool
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, Field

from app.agents.tools.tools_lib._base import (
    get_core_v1_api,
    get_rbac_v1_api,
    get_authorization_v1_api,
    _handle_k8s_exceptions,
    NoArgumentsInputSchema,
    NamespaceInputSchema,
)
from app.utils.logger_config import setup_logging
from app.utils.metrics import tool_calls_total

logger = setup_logging(app_name="kubeintellect")
_tracer = trace.get_tracer("kubeintellect.tools")


# ===============================================================================
#                               INPUT SCHEMAS
# ===============================================================================

class RoleInputSchema(BaseModel):
    namespace: str = Field(description="The Kubernetes namespace.")
    role_name: str = Field(description="The name of the Role.")


class ClusterRoleInputSchema(BaseModel):
    cluster_role_name: str = Field(description="The name of the ClusterRole.")


class ServiceAccountInputSchema(BaseModel):
    namespace: str = Field(description="The Kubernetes namespace.")
    service_account_name: str = Field(description="The name of the ServiceAccount.")


class WhoCanInputSchema(BaseModel):
    verb: str = Field(description="The action to check, e.g. 'get', 'list', 'create', 'delete', 'patch'.")
    resource: str = Field(description="The Kubernetes resource type, e.g. 'pods', 'deployments', 'secrets'.")
    namespace: Optional[str] = Field(default=None, description="The namespace to check in. If omitted, checks cluster scope.")
    subresource: Optional[str] = Field(default=None, description="Optional subresource, e.g. 'log' for pods/log.")


# ===============================================================================
#                               ROLE TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def list_roles(namespace: str) -> Dict[str, Any]:
    """Lists all Roles in a given namespace."""
    rbac = get_rbac_v1_api()
    roles = rbac.list_namespaced_role(namespace=namespace, timeout_seconds=10)
    result = []
    for role in roles.items:
        result.append({
            "name": role.metadata.name,
            "namespace": role.metadata.namespace,
            "creation_timestamp": role.metadata.creation_timestamp.isoformat() if role.metadata.creation_timestamp else None,
            "rule_count": len(role.rules) if role.rules else 0,
        })
    return {"status": "success", "data": result}


list_roles_tool = StructuredTool.from_function(
    func=list_roles,
    name="list_roles",
    description="Lists all RBAC Roles in a specified namespace with their name and rule count.",
    args_schema=NamespaceInputSchema,
)


@_handle_k8s_exceptions
def describe_role(namespace: str, role_name: str) -> Dict[str, Any]:
    """Retrieves full details for a specific Role including all policy rules."""
    rbac = get_rbac_v1_api()
    role = rbac.read_namespaced_role(name=role_name, namespace=namespace)
    rules = []
    for rule in (role.rules or []):
        rules.append({
            "api_groups": rule.api_groups or [],
            "resources": rule.resources or [],
            "verbs": rule.verbs or [],
            "resource_names": rule.resource_names or [],
        })
    return {
        "status": "success",
        "data": {
            "name": role.metadata.name,
            "namespace": role.metadata.namespace,
            "labels": role.metadata.labels or {},
            "creation_timestamp": role.metadata.creation_timestamp.isoformat() if role.metadata.creation_timestamp else None,
            "rules": rules,
        }
    }


describe_role_tool = StructuredTool.from_function(
    func=describe_role,
    name="describe_role",
    description="Retrieves full details for a specific RBAC Role including all policy rules (api_groups, resources, verbs).",
    args_schema=RoleInputSchema,
)


@_handle_k8s_exceptions
def list_cluster_roles() -> Dict[str, Any]:
    """Lists all ClusterRoles in the cluster."""
    rbac = get_rbac_v1_api()
    cluster_roles = rbac.list_cluster_role(timeout_seconds=10)
    result = []
    for cr in cluster_roles.items:
        result.append({
            "name": cr.metadata.name,
            "creation_timestamp": cr.metadata.creation_timestamp.isoformat() if cr.metadata.creation_timestamp else None,
            "rule_count": len(cr.rules) if cr.rules else 0,
            "aggregation_rule": cr.aggregation_rule is not None,
        })
    return {"status": "success", "data": result}


list_cluster_roles_tool = StructuredTool.from_function(
    func=list_cluster_roles,
    name="list_cluster_roles",
    description="Lists all ClusterRoles in the cluster with their name and rule count.",
    args_schema=NoArgumentsInputSchema,
)


@_handle_k8s_exceptions
def describe_cluster_role(cluster_role_name: str) -> Dict[str, Any]:
    """Retrieves full details for a specific ClusterRole including all policy rules."""
    rbac = get_rbac_v1_api()
    cr = rbac.read_cluster_role(name=cluster_role_name)
    rules = []
    for rule in (cr.rules or []):
        rules.append({
            "api_groups": rule.api_groups or [],
            "resources": rule.resources or [],
            "verbs": rule.verbs or [],
            "resource_names": rule.resource_names or [],
            "non_resource_urls": rule.non_resource_ur_ls or [],
        })
    return {
        "status": "success",
        "data": {
            "name": cr.metadata.name,
            "labels": cr.metadata.labels or {},
            "creation_timestamp": cr.metadata.creation_timestamp.isoformat() if cr.metadata.creation_timestamp else None,
            "rules": rules,
        }
    }


describe_cluster_role_tool = StructuredTool.from_function(
    func=describe_cluster_role,
    name="describe_cluster_role",
    description="Retrieves full details for a specific ClusterRole including all policy rules.",
    args_schema=ClusterRoleInputSchema,
)


# ===============================================================================
#                           ROLE BINDING TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def list_role_bindings(namespace: str) -> Dict[str, Any]:
    """Lists all RoleBindings in a namespace with their subjects and role references."""
    rbac = get_rbac_v1_api()
    bindings = rbac.list_namespaced_role_binding(namespace=namespace, timeout_seconds=10)
    result = []
    for rb in bindings.items:
        subjects = []
        for s in (rb.subjects or []):
            subjects.append({"kind": s.kind, "name": s.name, "namespace": s.namespace})
        result.append({
            "name": rb.metadata.name,
            "namespace": rb.metadata.namespace,
            "role_ref": {"kind": rb.role_ref.kind, "name": rb.role_ref.name},
            "subjects": subjects,
        })
    return {"status": "success", "data": result}


list_role_bindings_tool = StructuredTool.from_function(
    func=list_role_bindings,
    name="list_role_bindings",
    description="Lists all RoleBindings in a namespace showing which subjects (users, groups, service accounts) are bound to which roles.",
    args_schema=NamespaceInputSchema,
)


@_handle_k8s_exceptions
def list_cluster_role_bindings() -> Dict[str, Any]:
    """Lists all ClusterRoleBindings in the cluster."""
    rbac = get_rbac_v1_api()
    bindings = rbac.list_cluster_role_binding(timeout_seconds=10)
    result = []
    for crb in bindings.items:
        subjects = []
        for s in (crb.subjects or []):
            subjects.append({"kind": s.kind, "name": s.name, "namespace": s.namespace})
        result.append({
            "name": crb.metadata.name,
            "role_ref": {"kind": crb.role_ref.kind, "name": crb.role_ref.name},
            "subjects": subjects,
        })
    return {"status": "success", "data": result}


list_cluster_role_bindings_tool = StructuredTool.from_function(
    func=list_cluster_role_bindings,
    name="list_cluster_role_bindings",
    description="Lists all ClusterRoleBindings in the cluster showing which subjects are bound to which ClusterRoles.",
    args_schema=NoArgumentsInputSchema,
)


# ===============================================================================
#                          SERVICE ACCOUNT TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def list_service_accounts(namespace: str) -> Dict[str, Any]:
    """Lists all ServiceAccounts in a namespace."""
    core_v1 = get_core_v1_api()
    sas = core_v1.list_namespaced_service_account(namespace=namespace, timeout_seconds=10)
    result = []
    for sa in sas.items:
        result.append({
            "name": sa.metadata.name,
            "namespace": sa.metadata.namespace,
            "secret_count": len(sa.secrets) if sa.secrets else 0,
            "creation_timestamp": sa.metadata.creation_timestamp.isoformat() if sa.metadata.creation_timestamp else None,
        })
    return {"status": "success", "data": result}


list_service_accounts_tool = StructuredTool.from_function(
    func=list_service_accounts,
    name="list_service_accounts",
    description="Lists all ServiceAccounts in a specified namespace.",
    args_schema=NamespaceInputSchema,
)


@_handle_k8s_exceptions
def describe_service_account(namespace: str, service_account_name: str) -> Dict[str, Any]:
    """Retrieves details for a specific ServiceAccount including its secrets and image pull secrets."""
    core_v1 = get_core_v1_api()
    sa = core_v1.read_namespaced_service_account(name=service_account_name, namespace=namespace)
    return {
        "status": "success",
        "data": {
            "name": sa.metadata.name,
            "namespace": sa.metadata.namespace,
            "labels": sa.metadata.labels or {},
            "annotations": sa.metadata.annotations or {},
            "secrets": [s.name for s in (sa.secrets or [])],
            "image_pull_secrets": [s.name for s in (sa.image_pull_secrets or [])],
            "creation_timestamp": sa.metadata.creation_timestamp.isoformat() if sa.metadata.creation_timestamp else None,
        }
    }


describe_service_account_tool = StructuredTool.from_function(
    func=describe_service_account,
    name="describe_service_account",
    description="Retrieves details for a specific ServiceAccount including its associated secrets and image pull secrets.",
    args_schema=ServiceAccountInputSchema,
)


# ===============================================================================
#                           ACCESS REVIEW TOOL
# ===============================================================================

@_handle_k8s_exceptions
def check_who_can(verb: str, resource: str, namespace: Optional[str] = None,
                  subresource: Optional[str] = None) -> Dict[str, Any]:
    """Checks whether the current service account can perform a verb on a resource (SelfSubjectAccessReview)."""
    auth_v1 = get_authorization_v1_api()
    from kubernetes import client as k8s_client

    resource_attrs = k8s_client.V1ResourceAttributes(
        verb=verb,
        resource=resource,
        namespace=namespace,
        subresource=subresource,
    )
    spec = k8s_client.V1SelfSubjectAccessReviewSpec(resource_attributes=resource_attrs)
    review = k8s_client.V1SelfSubjectAccessReview(spec=spec)
    result = auth_v1.create_self_subject_access_review(body=review)

    return {
        "status": "success",
        "data": {
            "verb": verb,
            "resource": resource,
            "namespace": namespace or "(cluster-scoped)",
            "subresource": subresource,
            "allowed": result.status.allowed,
            "reason": result.status.reason,
            "evaluation_error": result.status.evaluation_error,
        }
    }


check_who_can_tool = StructuredTool.from_function(
    func=check_who_can,
    name="check_who_can",
    description="Checks whether the KubeIntellect service account is allowed to perform a specific verb on a resource (uses SelfSubjectAccessReview). Useful for diagnosing permission errors.",
    args_schema=WhoCanInputSchema,
)


# ===============================================================================
#                           CREATE TOOLS
# ===============================================================================

class CreateServiceAccountInputSchema(BaseModel):
    namespace: str = Field(description="The Kubernetes namespace where the ServiceAccount will be created.")
    name: str = Field(description="The name of the ServiceAccount.")


class CreateRoleInputSchema(BaseModel):
    namespace: str = Field(description="The Kubernetes namespace where the Role will be created.")
    name: str = Field(description="The name of the Role.")
    verbs: list = Field(description="List of verbs to allow, e.g. ['get', 'list', 'watch'].")
    resources: list = Field(description="List of resources to allow, e.g. ['pods', 'pods/log'].")
    api_groups: list = Field(default=[""], description="API groups for the rule. Default is [''] for core resources.")


class CreateRoleBindingInputSchema(BaseModel):
    namespace: str = Field(description="The Kubernetes namespace where the RoleBinding will be created.")
    name: str = Field(description="The name of the RoleBinding.")
    role_name: str = Field(description="The name of the Role to bind.")
    service_account_name: str = Field(description="The name of the ServiceAccount to bind the Role to.")
    service_account_namespace: Optional[str] = Field(default=None, description="Namespace of the ServiceAccount. Defaults to the same namespace as the RoleBinding.")


@_handle_k8s_exceptions
def create_service_account(namespace: str, name: str) -> Dict[str, Any]:
    """Creates a ServiceAccount in the specified namespace."""
    from kubernetes import client as k8s_client
    core_v1 = get_core_v1_api()
    sa = k8s_client.V1ServiceAccount(
        api_version="v1",
        kind="ServiceAccount",
        metadata=k8s_client.V1ObjectMeta(name=name, namespace=namespace),
    )
    resp = core_v1.create_namespaced_service_account(namespace=namespace, body=sa)
    return {
        "status": "success",
        "message": f"ServiceAccount '{name}' created in namespace '{namespace}'.",
        "data": {"name": resp.metadata.name, "namespace": resp.metadata.namespace},
    }


create_service_account_tool = StructuredTool.from_function(
    func=create_service_account,
    name="create_service_account",
    description="Creates a Kubernetes ServiceAccount in the specified namespace.",
    args_schema=CreateServiceAccountInputSchema,
)


@_handle_k8s_exceptions
def create_role(namespace: str, name: str, verbs: list, resources: list, api_groups: list = None) -> Dict[str, Any]:
    """Creates a Role in the specified namespace with the given RBAC rules."""
    from kubernetes import client as k8s_client
    rbac = get_rbac_v1_api()
    if api_groups is None:
        api_groups = [""]
    rule = k8s_client.V1PolicyRule(
        api_groups=api_groups,
        verbs=verbs,
        resources=resources,
    )
    role = k8s_client.V1Role(
        api_version="rbac.authorization.k8s.io/v1",
        kind="Role",
        metadata=k8s_client.V1ObjectMeta(name=name, namespace=namespace),
        rules=[rule],
    )
    resp = rbac.create_namespaced_role(namespace=namespace, body=role)
    return {
        "status": "success",
        "message": f"Role '{name}' created in namespace '{namespace}'.",
        "data": {"name": resp.metadata.name, "namespace": resp.metadata.namespace, "verbs": verbs, "resources": resources},
    }


create_role_tool = StructuredTool.from_function(
    func=create_role,
    name="create_role",
    description="Creates a Kubernetes Role in the specified namespace with the specified verbs and resources.",
    args_schema=CreateRoleInputSchema,
)


@_handle_k8s_exceptions
def create_role_binding(namespace: str, name: str, role_name: str, service_account_name: str, service_account_namespace: str = None) -> Dict[str, Any]:
    """Creates a RoleBinding that binds a Role to a ServiceAccount in the specified namespace."""
    from kubernetes import client as k8s_client
    rbac = get_rbac_v1_api()
    sa_namespace = service_account_namespace or namespace
    # kubernetes client v35+ uses RbacV1Subject instead of V1Subject
    SubjectClass = getattr(k8s_client, 'RbacV1Subject', None) or getattr(k8s_client, 'V1Subject', None)
    binding = k8s_client.V1RoleBinding(
        api_version="rbac.authorization.k8s.io/v1",
        kind="RoleBinding",
        metadata=k8s_client.V1ObjectMeta(name=name, namespace=namespace),
        role_ref=k8s_client.V1RoleRef(
            api_group="rbac.authorization.k8s.io",
            kind="Role",
            name=role_name,
        ),
        subjects=[
            SubjectClass(
                kind="ServiceAccount",
                name=service_account_name,
                namespace=sa_namespace,
            )
        ],
    )
    resp = rbac.create_namespaced_role_binding(namespace=namespace, body=binding)
    return {
        "status": "success",
        "message": f"RoleBinding '{name}' created in namespace '{namespace}', binding Role '{role_name}' to ServiceAccount '{service_account_name}'.",
        "data": {"name": resp.metadata.name, "namespace": resp.metadata.namespace},
    }


create_role_binding_tool = StructuredTool.from_function(
    func=create_role_binding,
    name="create_role_binding",
    description="Creates a Kubernetes RoleBinding in the specified namespace, binding a Role to a ServiceAccount.",
    args_schema=CreateRoleBindingInputSchema,
)


class DeleteRoleInputSchema(BaseModel):
    namespace: str = Field(..., description="Kubernetes namespace containing the Role.")
    name: str = Field(..., description="Name of the Role to delete.")


@_handle_k8s_exceptions
def delete_role(namespace: str, name: str) -> Dict[str, Any]:
    from kubernetes import client as k8s_client
    rbac_v1 = k8s_client.RbacAuthorizationV1Api()
    rbac_v1.delete_namespaced_role(name=name, namespace=namespace)
    return {"status": "success", "message": f"Role '{name}' deleted from namespace '{namespace}'."}


delete_role_tool = StructuredTool.from_function(
    func=delete_role,
    name="delete_role",
    description="Deletes a Kubernetes Role from the specified namespace.",
    args_schema=DeleteRoleInputSchema,
)


class DeleteRoleBindingInputSchema(BaseModel):
    namespace: str = Field(..., description="Kubernetes namespace containing the RoleBinding.")
    name: str = Field(..., description="Name of the RoleBinding to delete.")


@_handle_k8s_exceptions
def delete_role_binding(namespace: str, name: str) -> Dict[str, Any]:
    from kubernetes import client as k8s_client
    rbac_v1 = k8s_client.RbacAuthorizationV1Api()
    rbac_v1.delete_namespaced_role_binding(name=name, namespace=namespace)
    return {"status": "success", "message": f"RoleBinding '{name}' deleted from namespace '{namespace}'."}


delete_role_binding_tool = StructuredTool.from_function(
    func=delete_role_binding,
    name="delete_role_binding",
    description="Deletes a Kubernetes RoleBinding from the specified namespace.",
    args_schema=DeleteRoleBindingInputSchema,
)


class DeleteServiceAccountInputSchema(BaseModel):
    namespace: str = Field(..., description="Kubernetes namespace containing the ServiceAccount.")
    name: str = Field(..., description="Name of the ServiceAccount to delete.")


@_handle_k8s_exceptions
def delete_service_account(namespace: str, name: str) -> Dict[str, Any]:
    core_v1 = get_core_v1_api()
    core_v1.delete_namespaced_service_account(name=name, namespace=namespace)
    return {"status": "success", "message": f"ServiceAccount '{name}' deleted from namespace '{namespace}'."}


delete_service_account_tool = StructuredTool.from_function(
    func=delete_service_account,
    name="delete_service_account",
    description="Deletes a Kubernetes ServiceAccount from the specified namespace.",
    args_schema=DeleteServiceAccountInputSchema,
)


# ===============================================================================
#                             RBAC WHO-CAN (enumerate all rules for a subject)
# ===============================================================================

class RbacWhoCanInput(BaseModel):
    subject_kind: str = Field(
        description="Kind of the subject: User, ServiceAccount, or Group."
    )
    subject_name: str = Field(description="Name of the subject.")
    subject_namespace: Optional[str] = Field(
        default=None,
        description="Namespace of the subject. Required when subject_kind is ServiceAccount.",
    )


class RbacPermissionEntry(BaseModel):
    verbs: List[str]
    resource: str
    api_group: str
    namespace_scope: str  # namespace name, or "*" for cluster-scoped bindings


class RbacWhoCanOutput(BaseModel):
    status: str
    subject_kind: str
    subject_name: str
    subject_namespace: Optional[str] = None
    rules: Optional[List[RbacPermissionEntry]] = None
    total_rules: Optional[int] = None
    summary: Optional[str] = None
    error_type: Optional[str] = None
    message: Optional[str] = None


def _subject_matches(subject, kind: str, name: str, subject_namespace: Optional[str]) -> bool:
    """Return True if a binding subject matches the requested subject identity."""
    if getattr(subject, "kind", None) != kind:
        return False
    if getattr(subject, "name", None) != name:
        return False
    if kind == "ServiceAccount":
        return getattr(subject, "namespace", None) == (subject_namespace or "default")
    return True


def _expand_cluster_role_rules(rbac, role_name: str, namespace_scope: str) -> List[RbacPermissionEntry]:
    """Expand a ClusterRole's rules into RbacPermissionEntry list."""
    try:
        cr = rbac.read_cluster_role(name=role_name)
    except Exception:
        return []
    entries: List[RbacPermissionEntry] = []
    for rule in (cr.rules or []):
        api_groups = rule.api_groups or [""]
        resources = rule.resources or []
        verbs = rule.verbs or []
        for resource in resources:
            entries.append(RbacPermissionEntry(
                verbs=verbs,
                resource=resource,
                api_group=api_groups[0] if api_groups else "",
                namespace_scope=namespace_scope,
            ))
    return entries


def _expand_role_rules(rbac, role_name: str, role_namespace: str) -> List[RbacPermissionEntry]:
    """Expand a namespaced Role's rules into RbacPermissionEntry list."""
    try:
        role = rbac.read_namespaced_role(name=role_name, namespace=role_namespace)
    except Exception:
        return []
    entries: List[RbacPermissionEntry] = []
    for rule in (role.rules or []):
        api_groups = rule.api_groups or [""]
        resources = rule.resources or []
        verbs = rule.verbs or []
        for resource in resources:
            entries.append(RbacPermissionEntry(
                verbs=verbs,
                resource=resource,
                api_group=api_groups[0] if api_groups else "",
                namespace_scope=role_namespace,
            ))
    return entries


@_handle_k8s_exceptions
def rbac_who_can(
    subject_kind: str,
    subject_name: str,
    subject_namespace: Optional[str] = None,
) -> str:
    """Enumerate all RBAC permissions granted to a subject.

    Walks ClusterRoleBindings and RoleBindings to find all bindings that
    reference the subject, then expands the referenced Roles and ClusterRoles
    into a structured list of permission rules.
    """
    with _tracer.start_as_current_span("rbac_who_can") as span:
        try:
            rbac = get_rbac_v1_api()
            rules: List[RbacPermissionEntry] = []

            # --- ClusterRoleBindings ---
            crbs = rbac.list_cluster_role_binding(timeout_seconds=10)
            for crb in crbs.items:
                for subject in (crb.subjects or []):
                    if not _subject_matches(subject, subject_kind, subject_name, subject_namespace):
                        continue
                    role_ref = crb.role_ref
                    if role_ref.kind == "ClusterRole":
                        rules.extend(
                            _expand_cluster_role_rules(rbac, role_ref.name, namespace_scope="*")
                        )

            # --- Namespaced RoleBindings (all namespaces) ---
            rbs = rbac.list_role_binding_for_all_namespaces(timeout_seconds=10)
            for rb in rbs.items:
                rb_ns = rb.metadata.namespace
                for subject in (rb.subjects or []):
                    if not _subject_matches(subject, subject_kind, subject_name, subject_namespace):
                        continue
                    role_ref = rb.role_ref
                    if role_ref.kind == "Role":
                        rules.extend(_expand_role_rules(rbac, role_ref.name, rb_ns))
                    elif role_ref.kind == "ClusterRole":
                        # ClusterRole bound via RoleBinding → namespace-scoped grant
                        rules.extend(
                            _expand_cluster_role_rules(rbac, role_ref.name, namespace_scope=rb_ns)
                        )

            if not rules:
                summary = (
                    f"No RBAC bindings found for {subject_kind} '{subject_name}'. "
                    f"The subject has no permissions in this cluster."
                )
            else:
                summary = (
                    f"{subject_kind} '{subject_name}' has {len(rules)} permission rule(s) "
                    f"across all bindings."
                )

            output = RbacWhoCanOutput(
                status="success",
                subject_kind=subject_kind,
                subject_name=subject_name,
                subject_namespace=subject_namespace,
                rules=[r.model_dump() for r in rules],  # type: ignore[arg-type]
                total_rules=len(rules),
                summary=summary,
            )
            span.set_status(StatusCode.OK)
            tool_calls_total.labels(tool="rbac_who_can", status="success").inc()
            return output.model_dump_json()
        except Exception as e:
            span.set_status(StatusCode.ERROR, description=str(e))
            raise


rbac_who_can_tool = StructuredTool.from_function(
    func=rbac_who_can,
    name="rbac_who_can",
    description=(
        "Enumerate all RBAC permissions granted to a specific subject (User, ServiceAccount, or Group). "
        "Walks ClusterRoleBindings and RoleBindings to find all applicable rules. "
        "Use to answer 'what can this service account do?' or debug unexpected access."
    ),
    args_schema=RbacWhoCanInput,
)


# ===============================================================================
#                             RBAC CHECK (single subject+verb+resource check)
# ===============================================================================

class RbacCheckInput(BaseModel):
    subject_kind: str = Field(
        description="Kind of the subject: User, ServiceAccount, or Group."
    )
    subject_name: str = Field(description="Name of the subject.")
    verb: str = Field(
        description="Verb to check, e.g. 'get', 'list', 'create', 'delete', 'patch'."
    )
    resource: str = Field(
        description="Kubernetes resource type, e.g. 'pods', 'deployments', 'secrets'."
    )
    namespace: Optional[str] = Field(
        default=None,
        description="Namespace to check in. If omitted, checks cluster scope.",
    )
    subject_namespace: Optional[str] = Field(
        default=None,
        description="Namespace of the subject. Required when subject_kind is ServiceAccount.",
    )


class RbacCheckOutput(BaseModel):
    status: str
    subject_kind: str
    subject_name: str
    verb: str
    resource: str
    namespace: str
    allowed: Optional[bool] = None
    reason: Optional[str] = None
    evaluation_error: Optional[str] = None
    error_type: Optional[str] = None
    message: Optional[str] = None


@_handle_k8s_exceptions
def rbac_check(
    subject_kind: str,
    subject_name: str,
    verb: str,
    resource: str,
    namespace: Optional[str] = None,
    subject_namespace: Optional[str] = None,
) -> str:
    """Check whether a specific subject is allowed to perform a verb on a resource.

    Uses SubjectAccessReview to query the API server's authorizer directly.
    Requires subjectaccessreviews/create at cluster scope on the KubeIntellect
    service account (see rbac.yaml — added for this tool).
    """
    with _tracer.start_as_current_span("rbac_check") as span:
        try:
            from kubernetes import client as k8s_client
            auth_v1 = get_authorization_v1_api()

            # Resolve subject identity string for SubjectAccessReview
            if subject_kind == "ServiceAccount":
                sa_ns = subject_namespace or namespace or "default"
                user = f"system:serviceaccount:{sa_ns}:{subject_name}"
                groups: Optional[List[str]] = None
            elif subject_kind == "Group":
                user = None
                groups = [subject_name]
            else:  # User
                user = subject_name
                groups = None

            resource_attrs = k8s_client.V1ResourceAttributes(
                verb=verb,
                resource=resource,
                namespace=namespace,
            )
            spec = k8s_client.V1SubjectAccessReviewSpec(
                resource_attributes=resource_attrs,
                user=user,
                groups=groups,
            )
            review = k8s_client.V1SubjectAccessReview(spec=spec)
            result = auth_v1.create_subject_access_review(body=review)

            ns_display = namespace or "(cluster-scoped)"
            output = RbacCheckOutput(
                status="success",
                subject_kind=subject_kind,
                subject_name=subject_name,
                verb=verb,
                resource=resource,
                namespace=ns_display,
                allowed=result.status.allowed,
                reason=result.status.reason or None,
                evaluation_error=result.status.evaluation_error or None,
            )
            span.set_status(StatusCode.OK)
            tool_calls_total.labels(tool="rbac_check", status="success").inc()
            return output.model_dump_json()
        except Exception as e:
            span.set_status(StatusCode.ERROR, description=str(e))
            raise


rbac_check_tool = StructuredTool.from_function(
    func=rbac_check,
    name="rbac_check",
    description=(
        "Check whether a specific subject (User, ServiceAccount, or Group) is allowed "
        "to perform a verb (get/list/create/delete/patch/…) on a Kubernetes resource. "
        "Uses SubjectAccessReview for an authoritative answer from the API server. "
        "Use to diagnose 'why is my service account getting 403?' or RBAC misconfiguration."
    ),
    args_schema=RbacCheckInput,
)


# ===============================================================================
#                           TOOL COLLECTION
# ===============================================================================

rbac_tools = [
    list_roles_tool,
    describe_role_tool,
    list_cluster_roles_tool,
    describe_cluster_role_tool,
    list_role_bindings_tool,
    list_cluster_role_bindings_tool,
    list_service_accounts_tool,
    describe_service_account_tool,
    check_who_can_tool,
    create_service_account_tool,
    create_role_tool,
    create_role_binding_tool,
    delete_role_tool,
    delete_role_binding_tool,
    delete_service_account_tool,
    rbac_who_can_tool,
    rbac_check_tool,
]
