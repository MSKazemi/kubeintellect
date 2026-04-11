# app/agents/tools/ai_generated_tools/ai_generated_tools.py
import json
from functools import lru_cache
from typing import Any, Dict, List, Optional

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.services import kubernetes_service
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")


# ------------------- Exception Definitions -------------------
class KubernetesConfigurationError(Exception):
    """Raised when Kubernetes configuration cannot be loaded."""
    pass

class KubernetesAPIError(Exception):
    """Raised when a Kubernetes API call fails."""
    pass

# ------------------- Kubernetes Config Loader -------------------
def _load_k8s_config():
    try:
        logger.info("Trying to load local kubeconfig...")
        config.load_kube_config()
        logger.info("Loaded local kubeconfig.")
    except ConfigException:
        logger.warning("Local kubeconfig not found. Trying in-cluster config...")
        try:
            config.load_incluster_config()
            logger.info("Loaded in-cluster config.")
        except ConfigException as e:
            logger.error("Failed to load any Kubernetes config.")
            raise KubernetesConfigurationError("Kubernetes configuration could not be loaded.") from e

# ------------------- Kubernetes API Clients -------------------
@lru_cache()
def get_core_v1_api() -> client.CoreV1Api:
    _load_k8s_config()
    return client.CoreV1Api()

@lru_cache()
def get_apps_v1_api() -> client.AppsV1Api:
    _load_k8s_config()
    return client.AppsV1Api()

@lru_cache()
def get_networking_v1_api() -> client.NetworkingV1Api:
    _load_k8s_config()
    return client.NetworkingV1Api()


class ConnectivityCheckToolInput(BaseModel):
    timeout_seconds: Optional[int] = Field(default=5, description="Timeout for the Kubernetes API call in seconds.")
    max_retries: Optional[int] = Field(default=3, description="Maximum number of retry attempts.")
    retry_delay: Optional[float] = Field(default=1.0, description="Delay between retries in seconds.")

def _connectivity_check_func_for_tool(ConnectivityCheckToolInput: Optional[Dict[str, Any]] = None) -> str:
    result_dict = kubernetes_service.check_kubernetes_connectivity()
    return json.dumps(result_dict, indent=2)

connectivity_check_tool = StructuredTool.from_function(
    name="kubernetes_connectivity_check",
    func=_connectivity_check_func_for_tool,
    description="Checks connectivity to the configured Kubernetes cluster. Returns a JSON object with 'status' and 'message'. If successful, also includes 'nodes_count' and a sample of 'nodes'.",
    args_schema=ConnectivityCheckToolInput,
)


# <<< ALL GENERATED TOOL DEFINITIONS WILL BE APPENDED BELOW THIS LINE >>>


# --- List to hold all dynamically generated and registered tools ---
all_ai_generated_tools: List[StructuredTool] = [
    connectivity_check_tool,
]
