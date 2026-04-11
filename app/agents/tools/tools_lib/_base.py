"""
Shared base for all Kubernetes tool modules.

Centralises the repeated boilerplate that was copy-pasted across every
tools_lib file: exception classes, kubeconfig loading, cached API-client
singletons, the error-handling decorator, the three common Pydantic input
schemas, and the age/resource-quantity helper functions used by many files.

Usage in a tool module
----------------------
from app.agents.tools.tools_lib._base import (
    KubernetesConfigurationError,
    KubernetesAPIError,
    get_core_v1_api,
    get_apps_v1_api,
    get_batch_v1_api,
    get_networking_v1_api,
    get_custom_objects_api,
    _handle_k8s_exceptions,
    NoArgumentsInputSchema,
    NamespaceInputSchema,
    NamespaceOptionalInputSchema,
    calculate_age,
)
"""

import json
from datetime import datetime
from functools import lru_cache, wraps
from typing import Optional

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException
from kubernetes.client.exceptions import ApiException
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, Field

from app.utils.logger_config import setup_logging
from app.utils.metrics import tool_calls_total

logger = setup_logging(app_name="kubeintellect")


# ===============================================================================
#                             EXCEPTION DEFINITIONS
# ===============================================================================

class KubernetesConfigurationError(Exception):
    """Raised when Kubernetes configuration cannot be loaded."""
    pass


class KubernetesAPIError(Exception):
    """Raised when a Kubernetes API call fails."""
    pass


# ===============================================================================
#                           KUBERNETES CONFIG LOADER
# ===============================================================================

@lru_cache(maxsize=1)
def _load_k8s_config() -> None:
    """Load Kubernetes configuration once (local kubeconfig first, then in-cluster).

    Cached so that parallel tool calls sharing the same process only pay the
    config-loading cost once, regardless of how many API-client singletons are
    initialised concurrently.
    """
    try:
        logger.debug("Trying to load local kubeconfig...")
        config.load_kube_config()
        logger.debug("Loaded local kubeconfig.")
    except ConfigException:
        logger.debug("Local kubeconfig not found. Trying in-cluster config...")
        try:
            config.load_incluster_config()
            logger.debug("Loaded in-cluster config.")
        except ConfigException as e:
            logger.error("Failed to load any Kubernetes config.")
            raise KubernetesConfigurationError(
                "Kubernetes configuration could not be loaded."
            ) from e


# ===============================================================================
#                           KUBERNETES API CLIENT SINGLETONS
# ===============================================================================

@lru_cache()
def get_core_v1_api() -> client.CoreV1Api:
    """Return a cached CoreV1Api client."""
    _load_k8s_config()
    return client.CoreV1Api()


@lru_cache()
def get_apps_v1_api() -> client.AppsV1Api:
    """Return a cached AppsV1Api client."""
    _load_k8s_config()
    return client.AppsV1Api()


@lru_cache()
def get_batch_v1_api() -> client.BatchV1Api:
    """Return a cached BatchV1Api client."""
    _load_k8s_config()
    return client.BatchV1Api()


@lru_cache()
def get_networking_v1_api() -> client.NetworkingV1Api:
    """Return a cached NetworkingV1Api client."""
    _load_k8s_config()
    return client.NetworkingV1Api()


@lru_cache()
def get_custom_objects_api() -> client.CustomObjectsApi:
    """Return a cached CustomObjectsApi client (metrics, CRDs)."""
    _load_k8s_config()
    return client.CustomObjectsApi()


@lru_cache()
def get_rbac_v1_api() -> client.RbacAuthorizationV1Api:
    """Return a cached RbacAuthorizationV1Api client."""
    _load_k8s_config()
    return client.RbacAuthorizationV1Api()


@lru_cache()
def get_authorization_v1_api() -> client.AuthorizationV1Api:
    """Return a cached AuthorizationV1Api client (access reviews)."""
    _load_k8s_config()
    return client.AuthorizationV1Api()


@lru_cache()
def get_autoscaling_v2_api() -> client.AutoscalingV2Api:
    """Return a cached AutoscalingV2Api client (HPA v2)."""
    _load_k8s_config()
    return client.AutoscalingV2Api()


# ===============================================================================
#                           ERROR-HANDLING DECORATOR
# ===============================================================================

# Maps ApiException HTTP status codes to a concrete next-action hint shown to
# the LLM.  Richer error messages directly improve agent self-correction rate
# (BuildingAIAgentsClouds §5 insight #5).
_API_STATUS_SUGGESTIONS: dict[int, str] = {
    400: "The request body is malformed. Check field names and values against the Kubernetes API spec.",
    401: "Authentication failed. Ensure the ServiceAccount token or kubeconfig credentials are valid.",
    403: "Permission denied. Check RBAC: use check_who_can or describe_service_account to inspect the ServiceAccount's roles.",
    404: "Resource not found. Verify the resource name and namespace; use a list tool to confirm it exists.",
    405: "Method not allowed for this resource type. Try a different operation (e.g. patch instead of replace).",
    409: "Resource already exists. Use a patch or update tool instead of create, or delete it first.",
    422: "The resource spec is semantically invalid. Check required fields and enum values against the API spec.",
    429: "API server rate limit hit. Wait a moment and retry the same operation.",
    500: "Kubernetes API server internal error. Check cluster health: use get_node_status or kubectl get nodes.",
    503: "API server unavailable. The cluster may be overloaded or the control plane is restarting.",
}

_DEFAULT_SUGGESTION = (
    "Review the error message above, correct the parameters, and retry. "
    "If the resource state is unclear, use a describe or list tool first."
)


def _handle_k8s_exceptions(func):
    """Decorator that converts Kubernetes exceptions into structured error dicts
    and adds entry/exit logging to every tool call.

    Every tool function returns either a success payload or a dict:
        {
            "status": "error",
            "message": "<error type>: <k8s api text>",
            "error_type": "<ExceptionClassName>",
            "suggested_action": "<concrete next step for the agent>",
        }

    The ``suggested_action`` field is intentionally written for the LLM: it
    describes *what to do next*, not just what went wrong.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        logger.debug("tool_call tool=%s args=%s kwargs=%s", func.__name__, args, kwargs)
        try:
            result = func(*args, **kwargs)
            if isinstance(result, dict) and result.get("status") == "error":
                logger.warning(
                    "tool_error tool=%s error_type=%s message=%s",
                    func.__name__,
                    result.get("error_type", "unknown"),
                    result.get("message", ""),
                )
            else:
                logger.debug("tool_ok tool=%s result_preview=%s", func.__name__, str(result)[:120])
            return result
        except ConfigException as e:
            msg = f"Kubernetes config error: {e}"
            logger.warning("tool_error tool=%s error_type=K8sConfigError message=%s", func.__name__, msg)
            trace.get_current_span().set_status(StatusCode.ERROR, description=msg)
            tool_calls_total.labels(tool=func.__name__, status="error").inc()
            return {
                "status": "error",
                "message": msg,
                "error_type": "K8sConfigError",
                "suggested_action": (
                    "Ensure KUBECONFIG is set correctly for local use, or that the Pod's "
                    "ServiceAccount has been bound to a Role with the required permissions."
                ),
            }
        except ApiException as e:
            error_message = f"K8s API error {e.status} ({e.reason})"
            if e.body:
                try:
                    details = json.loads(e.body)
                    api_msg = details.get("message", "")
                    if api_msg:
                        error_message += f": {api_msg}"
                except json.JSONDecodeError:
                    error_message += f": {e.body}"
            suggested = _API_STATUS_SUGGESTIONS.get(e.status, _DEFAULT_SUGGESTION)
            logger.warning(
                "tool_error tool=%s error_type=ApiException status=%s message=%s",
                func.__name__, e.status, error_message,
            )
            trace.get_current_span().set_status(StatusCode.ERROR, description=error_message)
            tool_calls_total.labels(tool=func.__name__, status="error").inc()
            return {
                "status": "error",
                "message": error_message,
                "error_type": "ApiException",
                "suggested_action": suggested,
            }
        except ImportError as e:
            msg = f"Missing dependency: {e}"
            logger.error("tool_error tool=%s error_type=ImportError message=%s", func.__name__, msg)
            trace.get_current_span().set_status(StatusCode.ERROR, description=msg)
            tool_calls_total.labels(tool=func.__name__, status="error").inc()
            return {
                "status": "error",
                "message": msg,
                "error_type": "ImportError",
                "suggested_action": "A required Python package is missing. Contact the administrator to install it.",
            }
        except Exception as e:
            msg = str(e)
            logger.error(
                "tool_error tool=%s error_type=%s message=%s",
                func.__name__, type(e).__name__, msg, exc_info=True,
            )
            trace.get_current_span().set_status(StatusCode.ERROR, description=msg)
            tool_calls_total.labels(tool=func.__name__, status="error").inc()
            return {
                "status": "error",
                "message": msg,
                "error_type": type(e).__name__,
                "suggested_action": _DEFAULT_SUGGESTION,
            }
    return wrapper


# ===============================================================================
#                           COMMON INPUT SCHEMAS
# ===============================================================================

class NoArgumentsInputSchema(BaseModel):
    """Schema for tools that take no arguments."""
    pass


class NamespaceInputSchema(BaseModel):
    """Schema for tools that require only a namespace."""
    namespace: str = Field(description="The Kubernetes namespace to query.")


class NamespaceOptionalInputSchema(BaseModel):
    """Schema for tools where namespace is optional."""
    namespace: Optional[str] = Field(
        default=None,
        description=(
            "The Kubernetes namespace to query. "
            "If not provided, queries all namespaces."
        ),
    )


# ===============================================================================
#                           SHARED HELPER FUNCTIONS
# ===============================================================================

def _parse_cpu_to_millicores(cpu_value: str) -> float:
    """Convert a Kubernetes CPU quantity string to millicores."""
    if not cpu_value or cpu_value == "0":
        return 0.0
    if cpu_value.endswith("n"):
        return float(cpu_value[:-1]) / 1e6
    elif cpu_value.endswith("m"):
        return float(cpu_value[:-1])
    else:
        try:
            return float(cpu_value) * 1000
        except ValueError:
            return 0.0


def _parse_memory_to_mib(memory_value: str) -> float:
    """Convert a Kubernetes memory quantity string to MiB."""
    if not memory_value or memory_value == "0":
        return 0.0
    if memory_value.endswith("Ki"):
        return float(memory_value[:-2]) / 1024
    elif memory_value.endswith("Mi"):
        return float(memory_value[:-2])
    elif memory_value.endswith("Gi"):
        return float(memory_value[:-2]) * 1024
    elif memory_value.endswith("Ti"):
        return float(memory_value[:-2]) * 1024 * 1024
    else:
        try:
            return float(memory_value) / (1024 * 1024)
        except ValueError:
            return 0.0


def _parse_storage_size(size_str: str) -> int:
    """Parse a Kubernetes storage size string to bytes."""
    if not size_str:
        return 0
    s = size_str.strip().upper()
    multipliers = {
        "PI": 1024**5, "TI": 1024**4, "GI": 1024**3, "MI": 1024**2, "KI": 1024,
        "P":  1024**5, "T":  1024**4, "G":  1024**3, "M":  1024**2, "K":  1024,
    }
    numeric = ""
    for i, ch in enumerate(s):
        if ch.isdigit() or ch == ".":
            numeric += ch
        else:
            unit = s[i:].strip()
            try:
                base = float(numeric) if numeric else 0.0
            except ValueError:
                return 0
            return int(base * multipliers.get(unit, 1))
    try:
        return int(float(numeric))
    except ValueError:
        return 0


def calculate_age(creation_timestamp) -> str:
    """Return a human-readable age string (e.g. '3d 5h') from a K8s timestamp.

    Accepts either a datetime object (as returned by the K8s Python client) or
    an ISO-8601 string (as found in serialised API responses).
    """
    if not creation_timestamp:
        return "Unknown"

    if isinstance(creation_timestamp, str):
        creation_time = datetime.fromisoformat(
            creation_timestamp.replace("Z", "+00:00")
        )
    else:
        creation_time = creation_timestamp

    age_delta = datetime.utcnow() - creation_time.replace(tzinfo=None)
    days = age_delta.days
    hours = age_delta.seconds // 3600
    return f"{days}d {hours}h" if days > 0 else f"{hours}h"
