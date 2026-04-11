# app/services/kubernetes_service.py

from app.utils.logger_config import setup_logging
from functools import lru_cache
from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException
from kubernetes.client.exceptions import ApiException
from typing import List, Optional, Dict, Any
import time

logger = setup_logging(app_name="kubeintellect")

# ------------------- Exception Definitions -------------------
class KubernetesConfigurationError(Exception):
    """Raised when Kubernetes configuration cannot be loaded."""
    pass

class KubernetesAPIError(Exception):
    """Raised when a Kubernetes API call fails."""
    pass

# ------------------- Kubernetes Config Loader -------------------
@lru_cache(maxsize=1)
def _load_k8s_config():
    try:
        logger.debug("Trying to load local kubeconfig...")
        config.load_kube_config()
        logger.debug("Loaded local kubeconfig.")
    except ConfigException:
        logger.debug("Local kubeconfig not found. Trying in-cluster config...")
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
    logger.debug("Returning CoreV1Api client.")
    return client.CoreV1Api()

@lru_cache()
def get_apps_v1_api() -> client.AppsV1Api:
    _load_k8s_config()
    logger.debug("Returning AppsV1Api client.")
    return client.AppsV1Api()

@lru_cache()
def get_networking_v1_api() -> client.NetworkingV1Api:
    _load_k8s_config()
    logger.debug("Returning NetworkingV1Api client.")
    return client.NetworkingV1Api()


# ------------------- Check Kubernetes Connectivity -------------------
def check_kubernetes_connectivity(timeout_seconds: int = 5, max_retries: int = 3, retry_delay: float = 1.0) -> Dict[str, Any]:
    """
    Checks connectivity to the Kubernetes cluster by attempting to list nodes.
    Includes retry logic for transient issues.

    Args:
        timeout_seconds (int): The timeout for the Kubernetes API call in seconds.
        max_retries (int): The maximum number of retry attempts.
        retry_delay (float): The delay between retries in seconds.

    Returns:
        A dictionary with status, a message, and optionally node names on success.
    """
    logger.info("Performing Kubernetes connectivity check...")
    attempt = 0

    while attempt < max_retries:
        try:
            core_v1 = get_core_v1_api() # Use the centralized getter
            logger.info(f"Attempting to list nodes for connectivity check (attempt {attempt + 1})...")
            nodes_list = core_v1.list_node(timeout_seconds=timeout_seconds)
            node_names = [node.metadata.name for node in nodes_list.items]

            logger.info(f"Connectivity check successful. Found {len(node_names)} nodes.")
            return {
                "status": "success",
                "message": "Successfully connected to the Kubernetes cluster.",
                "nodes_count": len(node_names),
                "nodes_sample": node_names[:5] # Return a sample for brevity
            }

        except KubernetesConfigurationError as e_conf:
            logger.error(f"Connectivity check failed due to configuration error: {e_conf}", exc_info=True)
            # Configuration error is usually not transient, re-raise immediately
            return {"status": "failure", "message": f"Kubernetes configuration error: {str(e_conf)}"}
        except ApiException as e_api:
            logger.error(
                f"Connectivity check failed due to API error (attempt {attempt + 1}): "
                f"{e_api.status} {e_api.reason} - Body: {e_api.body}",
                exc_info=True
            )
            # If it's the last attempt, return the failure
            if attempt == max_retries - 1:
                return {
                    "status": "failure",
                    "message": f"Kubernetes API error during connectivity check after {max_retries} attempts: "
                               f"{e_api.reason} (Status: {e_api.status})",
                    "error_details": {
                        "status": e_api.status,
                        "reason": e_api.reason,
                        "body": e_api.body
                    }
                }
        except Exception as e: # Catch any other unexpected errors
            logger.error(
                f"Connectivity check failed due to an unexpected error (attempt {attempt + 1}): {e}",
                exc_info=True
            )
            # If it's the last attempt, return the failure
            if attempt == max_retries - 1:
                return {
                    "status": "failure",
                    "message": f"An unexpected error occurred during connectivity check after {max_retries} attempts: {str(e)}",
                }

        attempt += 1
        if attempt < max_retries:
            logger.info(f"Retrying Kubernetes connectivity check in {retry_delay} seconds (attempt {attempt + 1} of {max_retries})...")
            time.sleep(retry_delay)

    logger.error(f"Failed to connect to Kubernetes after {max_retries} attempts.")
    return {
        "status": "failure",
        "message": f"Failed to connect to Kubernetes cluster after {max_retries} attempts.",
    }

# ------------------- List Namespaces Function (Existing, for context) -------------------
def list_namespaces(timeout_seconds: int = 10, max_retries: int = 3, retry_delay: float = 1.0) -> Optional[List[str]]:
    namespace_names = []
    attempt = 0

    while attempt < max_retries:
        try:
            core_v1_api = get_core_v1_api()
            logger.info(f"Attempting to list namespaces (attempt {attempt + 1})...")
            namespaces_list_response = core_v1_api.list_namespace(timeout_seconds=timeout_seconds)
            for ns in namespaces_list_response.items:
                if ns.metadata and ns.metadata.name:
                    namespace_names.append(ns.metadata.name)
            logger.info(f"Successfully retrieved {len(namespace_names)} namespaces: {namespace_names}")
            return namespace_names

        except KubernetesConfigurationError:
            raise # Re-raise config error immediately

        except ApiException as e:
            logger.error(f"Kubernetes API Error while listing namespaces: {e.status} {e.reason} - Body: {e.body}", exc_info=True)

        except Exception as e:
            logger.error(f"An unexpected error occurred while listing namespaces: {e}", exc_info=True)

        attempt += 1
        if attempt < max_retries:
            logger.info(f"Retrying in {retry_delay} seconds...")
            time.sleep(retry_delay)

    logger.error("Failed to retrieve namespaces after multiple attempts.")
    return None

# --- -------------------------------List Pods Function -------------------------------
def list_pods_in_namespace(
    namespace: str = "default",
    timeout_seconds: int = 10,
    max_retries: int = 3,
    retry_delay: float = 1.0
) -> List[str]:
    """
    Lists all pod names in a given namespace.
    Includes retry logic for transient issues.

    Args:
        namespace (str): The namespace to list pods from.
        timeout_seconds (int): The timeout for the Kubernetes API call in seconds.
        max_retries (int): The maximum number of retry attempts.
        retry_delay (float): The delay between retries in seconds.

    Returns:
        A list of pod names. Returns an empty list on failure after retries,
        but logs the error.
    """
    pod_names = []
    attempt = 0

    while attempt < max_retries:
        try:
            core_v1 = get_core_v1_api() # Use the centralized getter
            logger.info(f"Attempting to list pods in namespace '{namespace}' (attempt {attempt + 1})...")
            pod_list = core_v1.list_namespaced_pod(namespace=namespace, timeout_seconds=timeout_seconds)
            for item in pod_list.items:
                pod_names.append(item.metadata.name)
            logger.info(f"Successfully retrieved {len(pod_names)} pods in namespace '{namespace}'.")
            return pod_names
        except KubernetesConfigurationError:
            logger.error("Kubernetes configuration error while trying to list pods.", exc_info=True)
            # Re-raise configuration error as it's not transient
            raise
        except ApiException as e:
            logger.error(
                f"API error listing pods in namespace '{namespace}' (attempt {attempt + 1}): "
                f"{e.status} {e.reason} - Body: {e.body}",
                exc_info=True
            )
            if attempt == max_retries - 1:
                logger.error(f"Failed to list pods in namespace '{namespace}' after {max_retries} attempts.")
                return [] # Return empty list on final failure
        except Exception as e:
            logger.error(
                f"Unexpected error listing pods in '{namespace}' (attempt {attempt + 1}): {e}",
                exc_info=True
            )
            if attempt == max_retries - 1:
                logger.error(f"Failed to list pods in namespace '{namespace}' after {max_retries} attempts due to unexpected error.")
                return [] # Return empty list on final failure
        
        attempt += 1
        if attempt < max_retries:
            logger.info(f"Retrying pod list in {retry_delay} seconds (attempt {attempt + 1} of {max_retries})...")
            time.sleep(retry_delay)
    
    # This part is reached if all retries fail for non-config errors.
    logger.error(f"Failed to list pods in namespace '{namespace}' after {max_retries} attempts.")
    return []

# --- Pod Event Function ---
def get_pod_events(
    pod_name: str,
    namespace: str,
    timeout_seconds: int = 10,
    max_retries: int = 3,
    retry_delay: float = 1.0
) -> Dict[str, Any]:
    """
    Retrieves events associated with a specified pod.
    Includes retry logic for transient issues.

    Args:
        pod_name (str): The name of the pod.
        namespace (str): The namespace of the pod.
        timeout_seconds (int): The timeout for the Kubernetes API call in seconds.
        max_retries (int): The maximum number of retry attempts.
        retry_delay (float): The delay between retries in seconds.

    Returns:
        A dictionary with status, a message, and a list of events (on success).
        Example success: {"status": "success", "events": [...]}
        Example failure: {"status": "failure", "message": "...", "error_details": {...}}
    """
    logger.info(f"Attempting to fetch events for pod '{pod_name}' in namespace '{namespace}'.")
    attempt = 0

    while attempt < max_retries:
        try:
            core_v1 = get_core_v1_api() # Use the centralized getter
            # Field selector to filter events for the specific pod
            field_selector = f"involvedObject.kind=Pod,involvedObject.name={pod_name},involvedObject.namespace={namespace}"
            event_list = core_v1.list_namespaced_event(
                namespace=namespace,
                field_selector=field_selector,
                timeout_seconds=timeout_seconds
            )
            
            pod_event_list: List[Dict[str, Any]] = []
            for event in event_list.items:
                pod_event_list.append({
                    "type": event.type,
                    "reason": event.reason,
                    "message": event.message,
                    "source_component": event.source.component if event.source else None,
                    "source_host": event.source.host if event.source else None,
                    "count": event.count,
                    "first_timestamp": event.first_timestamp.isoformat() if event.first_timestamp else None,
                    "last_timestamp": event.last_timestamp.isoformat() if event.last_timestamp else None,
                    "involved_object_kind": event.involved_object.kind,
                    "involved_object_name": event.involved_object.name,
                })
            
            logger.info(f"Successfully retrieved {len(pod_event_list)} events for pod '{pod_name}'.")
            return {"status": "success", "events": pod_event_list}

        except KubernetesConfigurationError as e_conf:
            logger.error("Kubernetes configuration error while trying to get pod events.", exc_info=True)
            return {
                "status": "failure",
                "message": f"Kubernetes configuration error: {str(e_conf)}"
            }
        except ApiException as e_api:
            logger.error(
                f"API error fetching events for pod '{pod_name}' (attempt {attempt + 1}): "
                f"{e_api.status} {e_api.reason} - Body: {e_api.body}",
                exc_info=True
            )
            if attempt == max_retries - 1:
                return {
                    "status": "failure",
                    "message": f"Kubernetes API error fetching events for pod '{pod_name}' after {max_retries} attempts: "
                               f"{e_api.reason} (Status: {e_api.status})",
                    "error_details": {
                        "status": e_api.status,
                        "reason": e_api.reason,
                        "body": e_api.body
                    }
                }
        except Exception as e:
            logger.error(
                f"Unexpected error fetching events for pod '{pod_name}' (attempt {attempt + 1}): {e}",
                exc_info=True
            )
            if attempt == max_retries - 1:
                return {
                    "status": "failure",
                    "message": f"Unexpected error fetching events for pod '{pod_name}' after {max_retries} attempts: {str(e)}",
                }
        
        attempt += 1
        if attempt < max_retries:
            logger.info(f"Retrying event fetch for pod '{pod_name}' in {retry_delay} seconds (attempt {attempt + 1} of {max_retries})...")
            time.sleep(retry_delay)
    
    logger.error(f"Failed to fetch events for pod '{pod_name}' after {max_retries} attempts.")
    return {
        "status": "failure",
        "message": f"Failed to fetch events for pod '{pod_name}' after {max_retries} attempts.",
    }

# ------------------------------ Pod Log Function ------------------------------
def get_pod_logs(
    name: str,
    namespace: str,
    tail_lines: Optional[int] = 50,
    previous: bool = False,
    container: Optional[str] = None, # New parameter for specifying container
    max_retries: int = 3,
    retry_delay: float = 1.0
) -> Dict[str, Any]:
    """
    Fetches logs for a specific pod, optionally for a specific container.
    Includes retry logic for transient issues.

    Args:
        name (str): The name of the pod.
        namespace (str): The namespace of the pod.
        tail_lines (Optional[int]): Number of lines from the end of the logs to show.
                                    If None, fetches all available logs.
        previous (bool): If True, fetch logs from the previous instance of the container.
        container (Optional[str]): The name of the container in the pod to fetch logs from.
                                   If None, fetches logs for the first container in the pod.
        max_retries (int): The maximum number of retry attempts.
        retry_delay (float): The delay between retries in seconds.

    Returns:
        A dictionary with status, a message, and logs (on success).
        Example success: {"status": "success", "logs": "..."}
        Example failure: {"status": "failure", "message": "...", "error_details": {...}}
    """
    logger.info(f"Attempting to fetch logs for pod '{name}' in namespace '{namespace}' "
                f"(container: {container if container else 'default'}, tail: {tail_lines}, previous: {previous}).")
    attempt = 0

    while attempt < max_retries:
        try:
            core_v1 = get_core_v1_api() # Use the centralized getter

            # Optional pre-check: Check if pod exists and get container name if not specified
            if container is None or attempt == 0: # Only do the pre-check on the first attempt or if container is still None
                try:
                    pod = core_v1.read_namespaced_pod(name=name, namespace=namespace)
                    if container is None:
                        if pod.spec and pod.spec.containers:
                            container = pod.spec.containers[0].name
                            logger.info(f"Container not specified, defaulting to first container: '{container}'.")
                        else:
                            logger.warning(f"Pod '{name}' has no containers defined.")
                            return {
                                "status": "failure",
                                "message": f"Error: Pod '{name}' in namespace '{namespace}' has no containers.",
                            }
                    
                    # Check pod phase for informational purposes
                    phase = pod.status.phase if pod.status else "Unknown"
                    if phase not in ["Running", "Succeeded", "Failed", "Unknown"]:
                        logger.warning(f"Pod '{name}' in namespace '{namespace}' is in phase '{phase}', logs might not be available or complete.")

                except ApiException as e_status:
                    if e_status.status == 404:
                        logger.warning(f"Pod '{name}' not found in namespace '{namespace}' during pre-check.")
                        return {
                            "status": "failure",
                            "message": f"Error: Pod '{name}' not found in namespace '{namespace}'.",
                            "error_details": {"status": e_status.status, "reason": e_status.reason}
                        }
                    logger.error(f"API error during pod pre-check for '{name}': {e_status.status} {e_status.reason} - Body: {e_status.body}", exc_info=True)
                    # Proceed to log fetching attempt, but log this issue
                    if attempt == max_retries - 1: # If this was the last attempt to get pod info
                        return {
                            "status": "failure",
                            "message": f"Error checking pod status for '{name}'. Status: {e_status.status}, Reason: {e_status.reason}",
                            "error_details": {"status": e_status.status, "reason": e_status.reason, "body": e_status.body}
                        }
                    time.sleep(retry_delay) # Give it a moment before retrying the log fetch
                    attempt += 1
                    continue # Skip to next retry attempt

            if not container: # Ensure container is set after pre-check
                return {
                    "status": "failure",
                    "message": f"Error: Could not determine container for pod '{name}' in namespace '{namespace}'.",
                }

            log_params = {
                "namespace": namespace,
                "name": name,
                "previous": previous,
                "timestamps": True, # Add timestamps for better readability
                "container": container # Use the specified or inferred container
            }
            if tail_lines is not None and tail_lines > 0:
                log_params["tail_lines"] = tail_lines
            
            logs = core_v1.read_namespaced_pod_log(**log_params)
            logger.info(f"Successfully fetched logs for pod '{name}' (container: {container}).")
            return {"status": "success", "logs": logs}

        except KubernetesConfigurationError as e_conf:
            logger.error("Kubernetes configuration error while trying to get pod logs.", exc_info=True)
            return {
                "status": "failure",
                "message": f"Kubernetes configuration error: {str(e_conf)}"
            }
        except ApiException as e_api:
            logger.error(
                f"API error fetching logs for pod '{name}' (attempt {attempt + 1}): "
                f"{e_api.status} {e_api.reason} - Body: {e_api.body}",
                exc_info=True
            )
            if e_api.status == 404:
                message = f"Error: Pod '{name}' or container '{container}' not found in namespace '{namespace}'."
            elif e_api.status == 400:
                message = f"Error: Bad request fetching logs for pod '{name}' (e.g. container not ready or log file empty). Reason: {e_api.reason}"
            else:
                message = f"Error: Kubernetes API error fetching logs for pod '{name}'. Status: {e_api.status}, Reason: {e_api.reason}"

            if attempt == max_retries - 1:
                return {
                    "status": "failure",
                    "message": message,
                    "error_details": {
                        "status": e_api.status,
                        "reason": e_api.reason,
                        "body": e_api.body
                    }
                }
        except Exception as e:
            logger.error(
                f"Unexpected error fetching logs for pod '{name}' (attempt {attempt + 1}): {e}",
                exc_info=True
            )
            if attempt == max_retries - 1:
                return {
                    "status": "failure",
                    "message": f"Error: Unexpected error fetching logs for pod '{name}'. Reason: {str(e)}",
                }
        
        attempt += 1
        if attempt < max_retries:
            logger.info(f"Retrying log fetch for pod '{name}' in {retry_delay} seconds (attempt {attempt + 1} of {max_retries})...")
            time.sleep(retry_delay)
    
    logger.error(f"Failed to fetch logs for pod '{name}' after {max_retries} attempts.")
    return {
        "status": "failure",
        "message": f"Failed to fetch logs for pod '{name}' after {max_retries} attempts.",
    }



# --- Pod Diagnostics Function ---
def get_pod_diagnostics(
    pod_name: str,
    namespace: str,
    tail_lines: Optional[int] = 100, # Configurable tail_lines for logs
    max_retries: int = 3,
    retry_delay: float = 1.0
) -> Dict[str, Any]:
    """
    Retrieves logs (filtered for errors/warnings) and events for a specified pod.
    Includes retry logic for transient issues for core pod details and leverages
    retries from `get_pod_logs` and `get_kubernetes_pod_events`.

    Args:
        pod_name (str): The name of the pod.
        namespace (str): The namespace of the pod.
        tail_lines (Optional[int]): Number of lines from the end of logs to fetch for diagnostics.
        max_retries (int): The maximum number of retry attempts for fetching pod details.
        retry_delay (float): The delay between retries for fetching pod details.

    Returns:
        A dictionary containing diagnostic information including filtered logs and events.
    """
    logger.info(f"Gathering diagnostics for pod '{pod_name}' in namespace '{namespace}'.")
    diagnostics: Dict[str, Any] = {
        "pod_name": pod_name,
        "namespace": namespace,
        "status": "unknown",
        "container_diagnostics": [],
        "pod_events": [],
        "error_message": None,
        "error_details": None
    }
    attempt = 0

    while attempt < max_retries:
        try:
            core_v1 = get_core_v1_api() # Use the centralized getter

            # Get pod details to retrieve container names
            pod_object = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
            container_names = [container.name for container in pod_object.spec.containers]
            diagnostics["pod_status"] = pod_object.status.phase if pod_object.status else "Unknown"
            diagnostics["status"] = "success_partial" # At least pod was found

            container_diags_list = []
            for container_name in container_names:
                current_container_diag = {"container_name": container_name, "errors": [], "warnings": [], "log_retrieval_error": None}
                
                # Use the improved get_pod_logs function
                log_result = get_pod_logs(
                    name=pod_name,
                    namespace=namespace,
                    container=container_name,
                    tail_lines=tail_lines,
                    # max_retries and retry_delay for logs are handled by get_pod_logs
                )

                if log_result["status"] == "success":
                    logs = log_result["logs"]
                    current_container_diag["errors"] = [line for line in logs.splitlines() if 'error' in line.lower()]
                    current_container_diag["warnings"] = [line for line in logs.splitlines() if 'warning' in line.lower()]
                else:
                    logger.warning(f"Failed to fetch logs for container '{container_name}' in pod '{pod_name}': {log_result.get('message')}")
                    current_container_diag["log_retrieval_error"] = log_result.get("message")
                    current_container_diag["log_error_details"] = log_result.get("error_details")
                container_diags_list.append(current_container_diag)
            diagnostics["container_diagnostics"] = container_diags_list
            
            # Get pod events
            events_result = get_pod_events(
                pod_name=pod_name,
                namespace=namespace,
            )

            if events_result["status"] == "success":
                diagnostics["pod_events"] = events_result["events"]
            else:
                logger.warning(f"Failed to fetch events for pod '{pod_name}': {events_result.get('message')}")
                diagnostics["pod_events_retrieval_error"] = events_result.get("message")
                diagnostics["pod_events_error_details"] = events_result.get("error_details")

            diagnostics["status"] = "success_full"
            logger.info(f"Successfully gathered diagnostics for pod '{pod_name}'.")
            return diagnostics

        except KubernetesConfigurationError as e_conf:
            logger.error(f"K8s configuration error during diagnostics for pod '{pod_name}': {e_conf}", exc_info=True)
            diagnostics["status"] = "error_config"
            diagnostics["error_message"] = "Kubernetes configuration error."
            diagnostics["error_details"] = str(e_conf)
            return diagnostics # Config errors are not transient, so return immediately

        except ApiException as e_api:
            logger.error(
                f"API error during diagnostics for pod '{pod_name}' (attempt {attempt + 1}): "
                f"{e_api.status} {e_api.reason} - Body: {e_api.body}",
                exc_info=True # Keep for debugging purposes in logs
            )
            
            # Immediate return for 404 Not Found, as it's not a transient error
            if e_api.status == 404:
                diagnostics["status"] = "error_api"
                diagnostics["error_message"] = f"Pod '{pod_name}' in namespace '{namespace}' not found."
                diagnostics["error_details"] = {
                    "status": e_api.status,
                    "reason": e_api.reason,
                    "body": e_api.body
                }
                return diagnostics # Exit immediately

            # For other API errors, retry if not the last attempt
            if attempt == max_retries - 1:
                diagnostics["status"] = "error_api"
                diagnostics["error_message"] = f"Kubernetes API error: {e_api.reason} (Status: {e_api.status})"
                diagnostics["error_details"] = {
                    "status": e_api.status,
                    "reason": e_api.reason,
                    "body": e_api.body
                }
                return diagnostics
        except Exception as e_generic:
            logger.error(
                f"Unexpected error during diagnostics for pod '{pod_name}' (attempt {attempt + 1}): {e_generic}",
                exc_info=True
            )
            if attempt == max_retries - 1:
                diagnostics["status"] = "error_unexpected"
                diagnostics["error_message"] = f"An unexpected error occurred: {str(e_generic)}"
                return diagnostics
        
        attempt += 1
        if attempt < max_retries:
            logger.info(f"Retrying diagnostics for pod '{pod_name}' in {retry_delay} seconds (attempt {attempt + 1} of {max_retries})...")
            time.sleep(retry_delay)
    
    logger.error(f"Failed to gather full diagnostics for pod '{pod_name}' after {max_retries} attempts.")
    diagnostics["status"] = "failure_overall"
    diagnostics["error_message"] = f"Failed to gather full diagnostics after {max_retries} attempts."
    return diagnostics


# ------------------- List All Pods Details -------------------
def list_all_pods_details(
    timeout_seconds: int = 30,
    max_retries: int = 3,
    retry_delay: float = 1.0
) -> Dict[str, Any]:
    """
    Retrieves detailed information about all pods across all namespaces.
    Includes retry logic for transient issues and returns a structured dictionary.

    Args:
        timeout_seconds (int): The timeout for the Kubernetes API call in seconds.
        max_retries (int): The maximum number of retry attempts.
        retry_delay (float): The delay between retries in seconds.

    Returns:
        A dictionary with status, a message, and a list of pod details (on success).
        Example success: {"status": "success", "message": "...", "pods_details": [...]}
        Example failure: {"status": "failure", "message": "...", "error_details": {...}}
    """
    logger.info("Fetching details for all pods in all namespaces...")
    pod_details_list: List[Dict[str, Any]] = []
    attempt = 0

    while attempt < max_retries:
        try:
            core_v1 = get_core_v1_api() # Use the centralized getter
            
            # Use watch=False for a one-time list.
            logger.info(f"Attempting to list all pods (attempt {attempt + 1})...")
            ret = core_v1.list_pod_for_all_namespaces(watch=False, timeout_seconds=timeout_seconds)
            
            for pod in ret.items:
                # Simplified container state extraction
                container_statuses_info = []
                if pod.status and pod.status.container_statuses:
                    for cs in pod.status.container_statuses:
                        state_info = "Unknown"
                        # Check if state exists before accessing its attributes
                        if cs.state:
                            if cs.state.running:
                                state_info = "Running"
                            elif cs.state.terminated:
                                state_info = f"Terminated (Reason: {cs.state.terminated.reason or 'N/A'})"
                            elif cs.state.waiting:
                                state_info = f"Waiting (Reason: {cs.state.waiting.reason or 'N/A'})"
                        
                        container_statuses_info.append({
                            "name": cs.name,
                            "image": cs.image,
                            "ready": cs.ready,
                            "restart_count": cs.restart_count,
                            "state": state_info,
                            # "last_state": cs.last_state.to_dict() if cs.last_state else None # Can be verbose
                        })
                
                pod_info = {
                    "name": pod.metadata.name,
                    "namespace": pod.metadata.namespace,
                    "status_phase": pod.status.phase if pod.status else "Unknown",
                    "node_name": pod.spec.node_name if pod.spec else "N/A",
                    "start_time": pod.status.start_time.isoformat() if pod.status and pod.status.start_time else None,
                    "pod_ip": pod.status.pod_ip if pod.status else "N/A",
                    "host_ip": pod.status.host_ip if pod.status else "N/A",
                    "containers": container_statuses_info,
                    "conditions": [cond.to_dict() for cond in pod.status.conditions] if pod.status and pod.status.conditions else [],
                    "labels": pod.metadata.labels if pod.metadata else None,
                    "creation_timestamp": pod.metadata.creation_timestamp.isoformat() if pod.metadata and pod.metadata.creation_timestamp else None,
                }
                pod_details_list.append(pod_info)
            
            logger.info(f"Successfully retrieved details for {len(pod_details_list)} pods.")
            return {
                "status": "success",
                "message": f"Successfully retrieved details for {len(pod_details_list)} pods.",
                "pods_details": pod_details_list
            }

        except KubernetesConfigurationError as e_conf:
            logger.error(f"Kubernetes configuration error while trying to list all pods: {e_conf}", exc_info=True)
            return {
                "status": "failure",
                "message": f"Kubernetes configuration error: {str(e_conf)}"
            }
        except ApiException as e_api:
            logger.error(
                f"API error listing all pods (attempt {attempt + 1}): "
                f"{e_api.status} {e_api.reason} - Body: {e_api.body}",
                exc_info=True
            )
            # For API errors, retry if not the last attempt
            if attempt == max_retries - 1:
                return {
                    "status": "failure",
                    "message": f"Kubernetes API error listing all pods after {max_retries} attempts: "
                               f"{e_api.reason} (Status: {e_api.status})",
                    "error_details": {
                        "status": e_api.status,
                        "reason": e_api.reason,
                        "body": e_api.body
                    }
                }
        except Exception as e:
            logger.error(
                f"Unexpected error listing all pods (attempt {attempt + 1}): {e}",
                exc_info=True
            )
            # For unexpected errors, retry if not the last attempt
            if attempt == max_retries - 1:
                return {
                    "status": "failure",
                    "message": f"An unexpected error occurred listing all pods after {max_retries} attempts: {str(e)}",
                }
        
        attempt += 1
        if attempt < max_retries:
            logger.info(f"Retrying list all pods in {retry_delay} seconds (attempt {attempt + 1} of {max_retries})...")
            time.sleep(retry_delay)
    
    logger.error(f"Failed to retrieve all pod details after {max_retries} attempts.")
    return {
        "status": "failure",
        "message": f"Failed to retrieve all pod details after {max_retries} attempts.",
    }

# ------------------- Deployment Functions -------------------

def get_deployment(
    name: str,
    namespace: str,
    timeout_seconds: int = 10, # This parameter applies to the overall attempt, not direct API call
    max_retries: int = 3,
    retry_delay: float = 1.0
) -> Dict[str, Any]:
    """
    Fetches a specific deployment by name and namespace.
    Includes retry logic for transient issues.

    Args:
        name (str): The name of the deployment.
        namespace (str): The namespace of the deployment.
        timeout_seconds (int): The timeout for each Kubernetes API call attempt in seconds.
                                (Note: Not all K8s client methods support this directly; used for retry loop duration).
        max_retries (int): The maximum number of retry attempts.
        retry_delay (float): The delay between retries in seconds.

    Returns:
        A dictionary with status, a message, and the V1Deployment data on success.
        Example success: {"status": "success", "message": "Deployment fetched.", "deployment_data": {...}}
        Example failure: {"status": "failure", "message": "...", "error_details": {...}}
    """
    logger.info(f"Attempting to fetch deployment '{name}' in namespace '{namespace}'.")
    attempt = 0

    while attempt < max_retries:
        try:
            apps_v1 = get_apps_v1_api()
            logger.info(f"Fetching deployment '{name}' (attempt {attempt + 1})...")
            # REMOVED timeout_seconds from this call as it's not supported by read_namespaced_deployment
            api_response = apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
            
            deployment_dict = client.ApiClient().sanitize_for_serialization(api_response)
            
            logger.info(f"Successfully fetched deployment '{name}' in namespace '{namespace}'.")
            return {
                "status": "success",
                "message": f"Deployment '{name}' fetched successfully.",
                "deployment_data": deployment_dict
            }
        except KubernetesConfigurationError as e_conf:
            logger.error("Kubernetes configuration error while trying to get deployment.", exc_info=True)
            return {
                "status": "failure",
                "message": f"Kubernetes configuration error: {str(e_conf)}"
            }
        except ApiException as e_api:
            logger.error(
                f"API error fetching deployment '{name}' (attempt {attempt + 1}): "
                f"{e_api.status} {e_api.reason} - Body: {e_api.body}",
                exc_info=True
            )
            if e_api.status == 404:
                return {
                    "status": "failure",
                    "message": f"Deployment '{name}' not found in namespace '{namespace}'.",
                    "error_details": {
                        "status": e_api.status,
                        "reason": e_api.reason,
                        "body": e_api.body
                    }
                }
            if attempt == max_retries - 1:
                return {
                    "status": "failure",
                    "message": f"Kubernetes API error fetching deployment '{name}' after {max_retries} attempts: "
                               f"{e_api.reason} (Status: {e_api.status})",
                    "error_details": {
                        "status": e_api.status,
                        "reason": e_api.reason,
                        "body": e_api.body
                    }
                }
        except Exception as e:
            logger.error(
                f"Unexpected error fetching deployment '{name}' (attempt {attempt + 1}): {e}",
                exc_info=True
            )
            if attempt == max_retries - 1:
                return {
                    "status": "failure",
                    "message": f"An unexpected error occurred fetching deployment '{name}' after {max_retries} attempts: {str(e)}",
                }
        
        attempt += 1
        if attempt < max_retries:
            logger.info(f"Retrying deployment fetch in {retry_delay} seconds (attempt {attempt + 1} of {max_retries})...")
            time.sleep(retry_delay)
    
    logger.error(f"Failed to fetch deployment '{name}' after {max_retries} attempts.")
    return {
        "status": "failure",
        "message": f"Failed to fetch deployment '{name}' after {max_retries} attempts.",
    }

def describe_deployment(name: str, namespace: str) -> Dict[str, Any]:
    """
    Fetches a deployment and returns a human-readable summary string.

    Args:
        name (str): The name of the deployment.
        namespace (str): The namespace of the deployment.

    Returns:
        A dictionary with status, a message, and the summary string on success.
        Example success: {"status": "success", "message": "Summary generated.", "summary_string": "..."}
        Example failure: {"status": "failure", "message": "...", "error_details": {...}}
    """
    logger.info(f"Attempting to describe deployment '{name}' in namespace '{namespace}'.")
    
    get_result = get_deployment(name, namespace)
    
    if get_result["status"] == "failure":
        return {
            "status": "failure",
            "message": f"Could not fetch deployment '{name}' for description: {get_result.get('message', 'Unknown error')}",
            "error_details": get_result.get("error_details")
        }

    deployment_data = get_result["deployment_data"]

    try:
        spec = deployment_data.get("spec", {})
        status = deployment_data.get("status", {})
        metadata = deployment_data.get("metadata", {})

        replicas_desired = spec.get("replicas", "N/A")
        replicas_available = status.get("availableReplicas", 0)
        replicas_ready = status.get("readyReplicas", 0)
        replicas_updated = status.get("updatedReplicas", 0)

        containers = []
        if "template" in spec and isinstance(spec["template"], dict) and \
           "spec" in spec["template"] and isinstance(spec["template"]["spec"], dict) and \
           "containers" in spec["template"]["spec"] and isinstance(spec["template"]["spec"]["containers"], list):
            for c in spec["template"]["spec"]["containers"]:
                containers.append(f"- Name: {c.get('name', 'N/A')}, Image: {c.get('image', 'N/A')}")
        container_summary = "\n  ".join(containers) if containers else "  No container details found."

        conditions = []
        if "conditions" in status and isinstance(status["conditions"], list):
            for cond in status["conditions"]:
                conditions.append(f"- Type: {cond.get('type')}, Status: {cond.get('status')}, Reason: {cond.get('reason', 'N/A')}, Message: {cond.get('message', 'N/A')}")
        condition_summary = "\n  ".join(conditions) if conditions else "  No conditions reported."

        summary = (
            f"Deployment Overview:\n"
            f"  Name: {metadata.get('name', name)}\n"
            f"  Namespace: {metadata.get('namespace', namespace)}\n"
            f"  UID: {metadata.get('uid', 'N/A')}\n"
            f"  Created: {metadata.get('creationTimestamp', 'N/A')}\n"
            f"  Replicas: {replicas_desired} desired, {replicas_updated} updated, {replicas_available} available, {replicas_ready} ready\n"
            f"  Containers:\n  {container_summary}\n"
            f"  Conditions:\n  {condition_summary}"
        )
        logger.info(f"Successfully generated description for deployment '{name}' in '{namespace}'.")
        return {
            "status": "success",
            "message": f"Description generated for deployment '{name}'.",
            "summary_string": summary
        }
    except Exception as e:
        logger.error(f"Error generating description for deployment '{name}': {e}", exc_info=True)
        return {
            "status": "failure",
            "message": f"Could not generate description for deployment '{name}'. Reason: {str(e)}",
        }

def scale_deployment(
    name: str,
    namespace: str,
    replicas: int,
    timeout_seconds: int = 10, # This parameter applies to the overall attempt
    max_retries: int = 3,
    retry_delay: float = 1.0
) -> Dict[str, Any]:
    """
    Scales a deployment to the specified number of replicas.
    Includes retry logic for transient issues.

    Args:
        name (str): The name of the deployment.
        namespace (str): The namespace of the deployment.
        replicas (int): The desired number of replicas.
        timeout_seconds (int): The timeout for each Kubernetes API call attempt in seconds.
                                (Note: Not all K8s client methods support this directly; used for retry loop duration).
        max_retries (int): The maximum number of retry attempts.
        retry_delay (float): The delay between retries in seconds.

    Returns:
        A dictionary with status, a message, and details about the scaling operation.
        Example success: {"status": "success", "message": "...", "desired_replicas": X, "actual_replicas": Y}
        Example failure: {"status": "failure", "message": "...", "error_details": {...}}
    """
    if replicas < 0:
        logger.error(f"Invalid replica count for scaling: {replicas}. Must be non-negative.")
        return {
            "status": "failure",
            "message": f"Invalid replica count: {replicas}. Replicas must be non-negative."
        }
    
    logger.info(f"Attempting to scale deployment '{name}' in namespace '{namespace}' to {replicas} replicas.")
    attempt = 0

    while attempt < max_retries:
        try:
            apps_v1 = get_apps_v1_api()
            body = {"spec": {"replicas": replicas}}
            
            logger.info(f"Scaling deployment '{name}' (attempt {attempt + 1}) to {replicas} replicas.")
            # REMOVED timeout_seconds from this call as it's not supported by patch_namespaced_deployment_scale
            api_response = apps_v1.patch_namespaced_deployment_scale(
                name=name,
                namespace=namespace,
                body=body
            )
            
            desired_replicas = api_response.spec.replicas if api_response.spec else None
            actual_replicas = api_response.status.replicas if api_response.status else None

            logger.info(f"Successfully scaled deployment '{name}' in namespace '{namespace}' to {replicas} replicas.")
            return {
                "status": "success",
                "message": f"Deployment '{name}' scaled to {replicas} replicas.",
                "desired_replicas": desired_replicas,
                "actual_replicas": actual_replicas
            }
        except KubernetesConfigurationError as e_conf:
            logger.error("Kubernetes configuration error while trying to scale deployment.", exc_info=True)
            return {
                "status": "failure",
                "message": f"Kubernetes configuration error: {str(e_conf)}"
            }
        except ApiException as e_api:
            logger.error(
                f"API error scaling deployment '{name}' (attempt {attempt + 1}): "
                f"{e_api.status} {e_api.reason} - Body: {e_api.body}",
                exc_info=True
            )
            if e_api.status == 404:
                return {
                    "status": "failure",
                    "message": f"Deployment '{name}' not found in namespace '{namespace}' for scaling operation.",
                    "error_details": {
                        "status": e_api.status,
                        "reason": e_api.reason,
                        "body": e_api.body
                    }
                }
            if attempt == max_retries - 1:
                return {
                    "status": "failure",
                    "message": f"Kubernetes API error scaling deployment '{name}' after {max_retries} attempts: "
                               f"{e_api.reason} (Status: {e_api.status})",
                    "error_details": {
                        "status": e_api.status,
                        "reason": e_api.reason,
                        "body": e_api.body
                    }
                }
        except Exception as e:
            logger.error(
                f"Unexpected error scaling deployment '{name}' (attempt {attempt + 1}): {e}",
                exc_info=True
            )
            if attempt == max_retries - 1:
                return {
                    "status": "failure",
                    "message": f"An unexpected error occurred scaling deployment '{name}' after {max_retries} attempts: {str(e)}",
                }
        
        attempt += 1
        if attempt < max_retries:
            logger.info(f"Retrying deployment scaling in {retry_delay} seconds (attempt {attempt + 1} of {max_retries})...")
            time.sleep(retry_delay)
    
    logger.error(f"Failed to scale deployment '{name}' after {max_retries} attempts.")
    return {
        "status": "failure",
        "message": f"Failed to scale deployment '{name}' after {max_retries} attempts.",
    }

# ------------------- CLI Debugging (Updated __main__ block for testing) -------------------
if __name__ == "__main__":
    print("--- Kubernetes Connectivity Check ---")
    connectivity_result = check_kubernetes_connectivity()
    print(connectivity_result)

    # print("\n--- Listing Pods in 'default' namespace ---")
    # pods = list_pods_in_namespace(namespace="default")
    # if pods:
    #     print(f"Found pods: {pods}")
        
    #     first_pod_name = pods[0]
    #     print(f"\n--- Fetching diagnostics for first pod '{first_pod_name}' in 'default' ---")
    #     diag_result = get_pod_diagnostics(pod_name=first_pod_name, namespace="default", tail_lines=20)
    #     print(diag_result)

    #     print(f"\n--- Fetching events for first pod '{first_pod_name}' in 'default' directly ---")
    #     events_result = get_kubernetes_pod_events(pod_name=first_pod_name, namespace="default")
    #     print(events_result)

    #     print("\n--- Fetching diagnostics for non-existent pod ---")
    #     non_existent_diag_result = get_pod_diagnostics(pod_name="non-existent-pod-123", namespace="default", tail_lines=10)
    #     print(non_existent_diag_result)

    #     print("\n--- Fetching events for non-existent pod ---")
    #     non_existent_events_result = get_kubernetes_pod_events(pod_name="non-existent-pod-456", namespace="default")
    #     print(non_existent_events_result)

    # else:
    #     print("No pods found or failed to list pods in 'default' namespace. Cannot run diagnostics examples.")

    # print("\n--- Listing All Pods Details ---")
    # all_pods_details_result = list_all_pods_details()
    # print(all_pods_details_result)

    # print("\n--- Deployment Functions Test ---")
    # test_deployment_name = "my-app-deployment" # Replace with an actual deployment name in your cluster
    # test_namespace = "default" # Replace with the namespace of your test deployment

    # # Test get_deployment
    # print(f"\nAttempting to get deployment '{test_deployment_name}' in '{test_namespace}'...")
    # deployment_get_result = get_deployment(name=test_deployment_name, namespace=test_namespace)
    # print(deployment_get_result)

    # if deployment_get_result["status"] == "success":
    #     # Test describe_deployment
    #     print(f"\nAttempting to describe deployment '{test_deployment_name}' in '{test_namespace}'...")
    #     deployment_describe_result = describe_deployment(name=test_deployment_name, namespace=test_namespace)
    #     print(deployment_describe_result)

    #     # Test scale_deployment
    #     current_replicas = deployment_get_result["deployment_data"]["spec"]["replicas"]
    #     new_replicas = 1 if current_replicas != 1 else 2 # Toggle between 1 and 2 for testing
    #     print(f"\nAttempting to scale deployment '{test_deployment_name}' to {new_replicas} replicas...")
    #     scale_result = scale_deployment(name=test_deployment_name, namespace=test_namespace, replicas=new_replicas)
    #     print(scale_result)
    # else:
    #     print(f"\nSkipping describe and scale tests as deployment '{test_deployment_name}' could not be fetched.")

    # # Test get_deployment for a non-existent one
    # print(f"\nAttempting to get non-existent deployment 'non-existent-dep' in '{test_namespace}'...")
    # non_existent_deployment_result = get_deployment(name="non-existent-dep", namespace=test_namespace)
    # print(non_existent_deployment_result)