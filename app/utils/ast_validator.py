"""
AST-based hallucination check for AI-generated Kubernetes tool code.

Parses generated Python code and validates all kubernetes.client.X attribute
accesses against a whitelist of known classes and API objects.
"""

import ast
from typing import List

from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")

# ---------------------------------------------------------------------------
# Whitelist of known kubernetes.client classes and API objects
# ---------------------------------------------------------------------------

K8S_CLIENT_WHITELIST = frozenset({
    # Core resource types
    "V1Pod",
    "V1Deployment",
    "V1Service",
    "V1ConfigMap",
    "V1Secret",
    "V1Namespace",
    "V1Node",
    "V1Role",
    "V1RoleBinding",
    "V1ClusterRole",
    "V1ClusterRoleBinding",
    "V1ServiceAccount",
    "V1StatefulSet",
    "V1DaemonSet",
    "V1ReplicaSet",
    "V1Job",
    "V1CronJob",
    "V1PersistentVolumeClaim",
    "V1PersistentVolume",
    "V1HorizontalPodAutoscaler",
    "V1NetworkPolicy",
    "V1ResourceQuota",
    "V1LimitRange",
    # Spec / sub-resource types
    "V1ObjectMeta",
    "V1Container",
    "V1PodSpec",
    "V1PodTemplateSpec",
    "V1LabelSelector",
    "V1EnvVar",
    "V1EnvFromSource",
    "V1ResourceRequirements",
    "V1Volume",
    "V1VolumeMount",
    "V1ContainerPort",
    "V1SecurityContext",
    "V1PodSecurityContext",
    "V1PolicyRule",
    "V1RoleRef",
    "V1Subject",
    "RbacV1Subject",
    "V1SelfSubjectAccessReview",
    "V1SelfSubjectAccessReviewSpec",
    "V1ResourceAttributes",
    "V1ServiceSpec",
    "V1ServicePort",
    "V1DeploymentSpec",
    "V1StatefulSetSpec",
    "V1DaemonSetSpec",
    "V1ReplicaSetSpec",
    "V1JobSpec",
    "V1CronJobSpec",
    "V1PersistentVolumeClaimSpec",
    "V1PersistentVolumeClaimVolumeSource",
    "V1PersistentVolumeSpec",
    "V1NetworkPolicySpec",
    "V1NetworkPolicyIngressRule",
    "V1NetworkPolicyEgressRule",
    "V1NetworkPolicyPeer",
    "V1ResourceQuotaSpec",
    "V1LimitRangeSpec",
    "V1LimitRangeItem",
    "V1ConfigMapKeySelector",
    "V1SecretKeySelector",
    "V1EnvVarSource",
    "V1ConfigMapEnvSource",
    "V1SecretEnvSource",
    "V1VolumeClaimTemplate",
    "V1PersistentVolumeClaimTemplate",
    "V1KeyToPath",
    "V1ConfigMapVolumeSource",
    "V1SecretVolumeSource",
    "V1EmptyDirVolumeSource",
    "V1HostPathVolumeSource",
    "V1PodAffinityTerm",
    "V1WeightedPodAffinityTerm",
    "V1NodeAffinity",
    "V1PodAffinity",
    "V1PodAntiAffinity",
    "V1Affinity",
    "V1Toleration",
    "V1NodeSelector",
    "V1NodeSelectorTerm",
    "V1NodeSelectorRequirement",
    "V1DeleteOptions",
    "V1Patch",
    "V1Status",
    "V1Probe",
    "V1HTTPGetAction",
    "V1TCPSocketAction",
    "V1ExecAction",
    "V1Lifecycle",
    "V1LifecycleHandler",
    # HPA v2 types
    "V2HorizontalPodAutoscaler",
    "V2HorizontalPodAutoscalerSpec",
    "V2MetricSpec",
    "V2ResourceMetricSource",
    "V2MetricTarget",
    "V2CrossVersionObjectReference",
    # API client objects
    "ApiClient",
    "CoreV1Api",
    "AppsV1Api",
    "RbacAuthorizationV1Api",
    "NetworkingV1Api",
    "AutoscalingV2Api",
    "AutoscalingV1Api",
    "BatchV1Api",
    "StorageV1Api",
    "CustomObjectsApi",
    "ApiextensionsV1Api",
    "PolicyV1Api",
    # Exceptions
    "exceptions",
    "ApiException",
    "ApiValueError",
    "ApiTypeError",
    # Configuration
    "Configuration",
})


class _K8sCallCollector(ast.NodeVisitor):
    """Collects attribute accesses on `kubernetes.client.X` or `client.X`."""

    def __init__(self):
        self.unknown: list[str] = []

    def visit_Attribute(self, node: ast.Attribute):
        # Match patterns like: kubernetes.client.Foo  or  client.Foo
        attr_name = node.attr
        value = node.value

        is_k8s_client = False

        # Direct: client.Foo
        if isinstance(value, ast.Name) and value.id in ("client", "k8s_client"):
            is_k8s_client = True

        # Chained: kubernetes.client.Foo
        elif (
            isinstance(value, ast.Attribute)
            and value.attr == "client"
            and isinstance(value.value, ast.Name)
            and value.value.id == "kubernetes"
        ):
            is_k8s_client = True

        if is_k8s_client and attr_name not in K8S_CLIENT_WHITELIST:
            self.unknown.append(attr_name)

        self.generic_visit(node)


def validate_k8s_api_calls(code_str: str) -> List[str]:
    """
    Parse *code_str* with the AST and return a list of unknown
    kubernetes.client attribute names (potential hallucinations).

    Args:
        code_str: Python source code string to validate.

    Returns:
        List of unrecognised attribute names (empty if all OK).
    """
    try:
        tree = ast.parse(code_str)
    except SyntaxError as e:
        logger.warning(f"AST validation: SyntaxError while parsing code: {e}")
        return [f"SyntaxError: {e}"]

    collector = _K8sCallCollector()
    collector.visit(tree)

    unknown = list(dict.fromkeys(collector.unknown))  # deduplicate, preserve order
    if unknown:
        logger.warning(f"AST validation found {len(unknown)} unknown k8s client call(s): {unknown}")
    return unknown


def format_ast_error_message(unknown_calls: List[str]) -> str:
    """
    Format a human-readable error message for the list of unknown API calls.

    Args:
        unknown_calls: List of unknown method/class names.

    Returns:
        Formatted error string suitable for returning to the CodeGenerator agent.
    """
    if not unknown_calls:
        return ""

    names = ", ".join(f"`{n}`" for n in unknown_calls)
    return (
        f"AST Hallucination Check FAILED. The following kubernetes.client "
        f"attribute(s) do not exist in the whitelist and are likely hallucinated: "
        f"{names}. "
        f"Please correct the code to use only valid kubernetes.client classes "
        f"(e.g. CoreV1Api, AppsV1Api, V1Pod, V1Deployment, V1Service, etc.)."
    )
