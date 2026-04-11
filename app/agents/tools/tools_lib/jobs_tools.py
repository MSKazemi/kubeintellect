import json
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.agents.tools.tools_lib._base import (
    get_core_v1_api,
    get_batch_v1_api,
    _handle_k8s_exceptions,
    NoArgumentsInputSchema,
    NamespaceOptionalInputSchema,
    calculate_age as _calculate_age,
)
from app.services import kubernetes_service
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")

try:
    from croniter import croniter
    CRONITER_AVAILABLE = True
except ImportError:
    CRONITER_AVAILABLE = False


def _calculate_next_cron_run(schedule: str, last_time: Optional[datetime] = None):
    """Calculate the next run time for a cron schedule."""
    if not CRONITER_AVAILABLE:
        # Fallback logic without croniter
        if last_time:
            # Simple heuristic: assume every hour for complex schedules
            return last_time + timedelta(hours=1)
        return datetime.utcnow() + timedelta(hours=1)
    
    try:
        base_time = last_time if last_time else datetime.utcnow()
        cron = croniter(schedule, base_time)
        return cron.get_next(datetime)
    except Exception:
        # Fallback if cron parsing fails
        base_time = last_time if last_time else datetime.utcnow()
        return base_time + timedelta(hours=1)


def _parse_job_status(job_status):
    """Parse job status into a comprehensive status object."""
    status_info = {
        "active": job_status.active or 0,
        "succeeded": job_status.succeeded or 0,
        "failed": job_status.failed or 0,
        "completion_time": None,
        "start_time": None,
        "conditions": [],
        "overall_status": "Unknown"
    }
    
    if job_status.completion_time:
        status_info["completion_time"] = job_status.completion_time.isoformat()
    
    if job_status.start_time:
        status_info["start_time"] = job_status.start_time.isoformat()
    
    # Parse conditions
    if job_status.conditions:
        for condition in job_status.conditions:
            condition_info = {
                "type": condition.type,
                "status": condition.status,
                "reason": condition.reason,
                "message": condition.message,
                "last_transition_time": condition.last_transition_time.isoformat() if condition.last_transition_time else None
            }
            status_info["conditions"].append(condition_info)
            
            # Determine overall status
            if condition.type == "Complete" and condition.status == "True":
                status_info["overall_status"] = "Completed"
            elif condition.type == "Failed" and condition.status == "True":
                status_info["overall_status"] = "Failed"
    
    if status_info["active"] > 0:
        status_info["overall_status"] = "Running"
    elif status_info["overall_status"] == "Unknown" and status_info["succeeded"] > 0:
        status_info["overall_status"] = "Completed"
    
    return status_info


# ===============================================================================
#                               INPUT SCHEMAS
# ===============================================================================

class JobInputSchema(BaseModel):
    """Schema for job-specific operations."""
    namespace: str = Field(description="The Kubernetes namespace where the job is located.")
    job_name: str = Field(description="The name of the Kubernetes job.")


class CronJobInputSchema(BaseModel):
    """Schema for cronjob-specific operations."""
    namespace: str = Field(description="The Kubernetes namespace where the cronjob is located.")
    cronjob_name: str = Field(description="The name of the Kubernetes cronjob.")


class TimeRangeInputSchema(BaseModel):
    """Schema for tools that use time ranges."""
    hours: int = Field(default=24, description="The number of hours to look back. Defaults to 24 hours.")
    namespace: Optional[str] = Field(default=None, description="The Kubernetes namespace to query. If not provided, queries all namespaces.")


class JobStatusInputSchema(BaseModel):
    """Schema for filtering jobs by status."""
    namespace: Optional[str] = Field(default=None, description="The Kubernetes namespace to query. If not provided, queries all namespaces.")
    status: str = Field(description="Job status to filter by (Running, Completed, Failed, Pending).")


class ConnectivityCheckInputSchema(BaseModel):
    """Schema for connectivity check tool."""
    timeout_seconds: Optional[int] = Field(default=5, description="The timeout for the Kubernetes API call in seconds.")
    max_retries: Optional[int] = Field(default=3, description="The maximum number of retry attempts.")
    retry_delay: Optional[float] = Field(default=1.0, description="The delay between retries in seconds.")


# ===============================================================================
#                            CONNECTIVITY TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def _connectivity_check_func(tool_input: Optional[Dict[str, Any]] = None) -> str:
    """Check connectivity to the Kubernetes cluster by attempting to list nodes."""
    result_dict = kubernetes_service.check_kubernetes_connectivity()
    return json.dumps(result_dict, indent=2)


connectivity_check_tool = StructuredTool.from_function(
    name="kubernetes_connectivity_check",
    func=_connectivity_check_func,
    description="Checks connectivity to the configured Kubernetes cluster. Returns a JSON object with 'status' and 'message'. If successful, also includes 'nodes_count' and a sample of 'nodes'. Useful to verify if KubeIntellect can talk to the cluster before attempting other operations.",
    args_schema=ConnectivityCheckInputSchema
)


# ===============================================================================
#                               JOB TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def list_jobs(namespace: Optional[str] = None) -> Dict[str, Any]:
    """Lists all Kubernetes jobs in a namespace or across all namespaces."""
    batch_v1 = get_batch_v1_api()
    
    if namespace:
        job_list = batch_v1.list_namespaced_job(namespace=namespace, timeout_seconds=10)
    else:
        job_list = batch_v1.list_job_for_all_namespaces(timeout_seconds=10)
    
    jobs = []
    for job in job_list.items:
        job_info = {
            "name": job.metadata.name,
            "namespace": job.metadata.namespace,
            "creation_timestamp": job.metadata.creation_timestamp.isoformat() if job.metadata.creation_timestamp else None,
            "age": _calculate_age(job.metadata.creation_timestamp),
            "labels": job.metadata.labels or {},
            "status": _parse_job_status(job.status),
            "parallelism": job.spec.parallelism,
            "completions": job.spec.completions,
            "backoff_limit": job.spec.backoff_limit
        }
        
        # Add pod template information
        if job.spec.template.spec.containers:
            container = job.spec.template.spec.containers[0]  # Get first container
            job_info["container_image"] = container.image
            job_info["container_name"] = container.name
        
        jobs.append(job_info)
    
    # Group by namespace if querying all namespaces
    if not namespace:
        jobs_by_namespace = {}
        for job in jobs:
            ns = job["namespace"]
            if ns not in jobs_by_namespace:
                jobs_by_namespace[ns] = []
            jobs_by_namespace[ns].append(job)
        
        result_data = {
            "jobs_by_namespace": jobs_by_namespace,
            "total_job_count": len(jobs),
            "namespace_count": len(jobs_by_namespace)
        }
    else:
        result_data = {
            "namespace": namespace,
            "jobs": jobs,
            "job_count": len(jobs)
        }
    
    return {"status": "success", "data": result_data}


list_jobs_tool = StructuredTool.from_function(
    func=list_jobs,
    name="list_jobs",
    description="Lists all Kubernetes jobs in a namespace or across all namespaces with comprehensive job information including status, parallelism, and container details.",
    args_schema=NamespaceOptionalInputSchema
)


@_handle_k8s_exceptions
def describe_job(namespace: str, job_name: str) -> Dict[str, Any]:
    """Gets detailed information about a specific job."""
    batch_v1 = get_batch_v1_api()
    core_v1 = get_core_v1_api()
    
    job = batch_v1.read_namespaced_job(name=job_name, namespace=namespace)
    
    job_details = {
        "name": job.metadata.name,
        "namespace": job.metadata.namespace,
        "labels": job.metadata.labels or {},
        "annotations": job.metadata.annotations or {},
        "creation_timestamp": job.metadata.creation_timestamp.isoformat() if job.metadata.creation_timestamp else None,
        "age": _calculate_age(job.metadata.creation_timestamp),
        "uid": job.metadata.uid,
        "status": _parse_job_status(job.status),
        "spec": {
            "parallelism": job.spec.parallelism,
            "completions": job.spec.completions,
            "backoff_limit": job.spec.backoff_limit,
            "active_deadline_seconds": job.spec.active_deadline_seconds,
            "ttl_seconds_after_finished": job.spec.ttl_seconds_after_finished
        },
        "containers": [],
        "restart_policy": job.spec.template.spec.restart_policy
    }
    
    # Extract container information
    if job.spec.template.spec.containers:
        for container in job.spec.template.spec.containers:
            container_info = {
                "name": container.name,
                "image": container.image,
                "command": container.command,
                "args": container.args,
                "env_vars": []
            }
            
            # Extract environment variables
            if container.env:
                for env_var in container.env:
                    env_info = {"name": env_var.name}
                    if env_var.value:
                        env_info["value"] = env_var.value
                    elif env_var.value_from:
                        if env_var.value_from.config_map_key_ref:
                            env_info["value_from"] = f"ConfigMap/{env_var.value_from.config_map_key_ref.name}:{env_var.value_from.config_map_key_ref.key}"
                        elif env_var.value_from.secret_key_ref:
                            env_info["value_from"] = f"Secret/{env_var.value_from.secret_key_ref.name}:{env_var.value_from.secret_key_ref.key}"
                        elif env_var.value_from.field_ref:
                            env_info["value_from"] = f"FieldRef/{env_var.value_from.field_ref.field_path}"
                    
                    container_info["env_vars"].append(env_info)
            
            # Extract resource requirements
            if container.resources:
                container_info["resources"] = {}
                if container.resources.requests:
                    container_info["resources"]["requests"] = dict(container.resources.requests)
                if container.resources.limits:
                    container_info["resources"]["limits"] = dict(container.resources.limits)
            
            job_details["containers"].append(container_info)
    
    # Get associated pods
    try:
        pod_list = core_v1.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"job-name={job_name}",
            timeout_seconds=10
        )
        
        job_details["pods"] = []
        for pod in pod_list.items:
            pod_info = {
                "name": pod.metadata.name,
                "phase": pod.status.phase,
                "creation_timestamp": pod.metadata.creation_timestamp.isoformat() if pod.metadata.creation_timestamp else None,
                "node_name": pod.spec.node_name
            }
            
            # Get pod container statuses
            if pod.status.container_statuses:
                pod_info["container_statuses"] = []
                for container_status in pod.status.container_statuses:
                    status_info = {
                        "name": container_status.name,
                        "ready": container_status.ready,
                        "restart_count": container_status.restart_count,
                        "image": container_status.image
                    }
                    
                    if container_status.state:
                        if container_status.state.running:
                            status_info["state"] = "running"
                            status_info["started_at"] = container_status.state.running.started_at.isoformat()
                        elif container_status.state.terminated:
                            status_info["state"] = "terminated"
                            status_info["exit_code"] = container_status.state.terminated.exit_code
                            status_info["reason"] = container_status.state.terminated.reason
                            status_info["finished_at"] = container_status.state.terminated.finished_at.isoformat() if container_status.state.terminated.finished_at else None
                        elif container_status.state.waiting:
                            status_info["state"] = "waiting"
                            status_info["reason"] = container_status.state.waiting.reason
                            status_info["message"] = container_status.state.waiting.message
                    
                    pod_info["container_statuses"].append(status_info)
            
            job_details["pods"].append(pod_info)
        
        job_details["pod_count"] = len(job_details["pods"])
    
    except Exception as e:
        job_details["pods"] = []
        job_details["pod_count"] = 0
        job_details["pod_error"] = str(e)
    
    return {"status": "success", "data": job_details}


describe_job_tool = StructuredTool.from_function(
    func=describe_job,
    name="describe_job",
    description="Gets comprehensive detailed information about a specific Kubernetes job including status, containers, environment variables, resources, and associated pods.",
    args_schema=JobInputSchema
)


@_handle_k8s_exceptions
def list_failed_jobs(hours: int = 24, namespace: Optional[str] = None) -> Dict[str, Any]:
    """Lists all failed jobs within a specified time range."""
    batch_v1 = get_batch_v1_api()
    
    now = datetime.utcnow()
    time_threshold = now - timedelta(hours=hours)
    
    if namespace:
        job_list = batch_v1.list_namespaced_job(namespace=namespace, timeout_seconds=10)
    else:
        job_list = batch_v1.list_job_for_all_namespaces(timeout_seconds=10)
    
    failed_jobs = []
    
    for job in job_list.items:
        job_status = _parse_job_status(job.status)
        
        if job_status["overall_status"] == "Failed":
            # Check if failure is within time range
            failure_time = None
            
            for condition in job_status["conditions"]:
                if condition["type"] == "Failed" and condition["status"] == "True":
                    if condition["last_transition_time"]:
                        failure_time = datetime.fromisoformat(condition["last_transition_time"].replace('Z', '+00:00'))
                        failure_time = failure_time.replace(tzinfo=None)
                        
                        if failure_time >= time_threshold:
                            failed_job_info = {
                                "name": job.metadata.name,
                                "namespace": job.metadata.namespace,
                                "failure_time": condition["last_transition_time"],
                                "failure_reason": condition["reason"],
                                "failure_message": condition["message"],
                                "age": _calculate_age(job.metadata.creation_timestamp),
                                "attempts": {
                                    "active": job_status["active"],
                                    "succeeded": job_status["succeeded"],
                                    "failed": job_status["failed"]
                                },
                                "backoff_limit": job.spec.backoff_limit
                            }
                            
                            # Add container image information
                            if job.spec.template.spec.containers:
                                failed_job_info["container_image"] = job.spec.template.spec.containers[0].image
                            
                            failed_jobs.append(failed_job_info)
                        break
    
    # Group by namespace if querying all namespaces
    if not namespace:
        failed_jobs_by_namespace = {}
        for job in failed_jobs:
            ns = job["namespace"]
            if ns not in failed_jobs_by_namespace:
                failed_jobs_by_namespace[ns] = []
            failed_jobs_by_namespace[ns].append(job)
        
        result_data = {
            "time_range_hours": hours,
            "failed_jobs_by_namespace": failed_jobs_by_namespace,
            "total_failed_jobs": len(failed_jobs),
            "namespace_count": len(failed_jobs_by_namespace)
        }
    else:
        result_data = {
            "namespace": namespace,
            "time_range_hours": hours,
            "failed_jobs": failed_jobs,
            "failed_job_count": len(failed_jobs)
        }
    
    return {"status": "success", "data": result_data}


list_failed_jobs_tool = StructuredTool.from_function(
    func=list_failed_jobs,
    name="list_failed_jobs",
    description="Lists all failed Kubernetes jobs within a specified time range with detailed failure information including reasons, messages, and retry attempts.",
    args_schema=TimeRangeInputSchema
)


@_handle_k8s_exceptions
def list_jobs_by_status(status: str, namespace: Optional[str] = None) -> Dict[str, Any]:
    """Lists jobs filtered by their current status."""
    batch_v1 = get_batch_v1_api()
    
    valid_statuses = ["Running", "Completed", "Failed", "Pending"]
    if status not in valid_statuses:
        return {
            "status": "error",
            "message": f"Invalid job status. Must be one of: {', '.join(valid_statuses)}",
            "error_type": "ValidationError"
        }
    
    if namespace:
        job_list = batch_v1.list_namespaced_job(namespace=namespace, timeout_seconds=10)
    else:
        job_list = batch_v1.list_job_for_all_namespaces(timeout_seconds=10)
    
    filtered_jobs = []
    
    for job in job_list.items:
        job_status_info = _parse_job_status(job.status)
        
        if job_status_info["overall_status"] == status or (status == "Pending" and job_status_info["overall_status"] == "Unknown"):
            job_info = {
                "name": job.metadata.name,
                "namespace": job.metadata.namespace,
                "age": _calculate_age(job.metadata.creation_timestamp),
                "status": job_status_info,
                "parallelism": job.spec.parallelism,
                "completions": job.spec.completions,
                "backoff_limit": job.spec.backoff_limit
            }
            
            # Add container information
            if job.spec.template.spec.containers:
                container = job.spec.template.spec.containers[0]
                job_info["container_image"] = container.image
                job_info["container_name"] = container.name
            
            # Add duration for completed/failed jobs
            if job_status_info["start_time"] and job_status_info["completion_time"]:
                start_time = datetime.fromisoformat(job_status_info["start_time"].replace('Z', '+00:00'))
                completion_time = datetime.fromisoformat(job_status_info["completion_time"].replace('Z', '+00:00'))
                duration = completion_time - start_time
                job_info["duration"] = str(duration)
            
            filtered_jobs.append(job_info)
    
    # Group by namespace if querying all namespaces
    if not namespace:
        jobs_by_namespace = {}
        for job in filtered_jobs:
            ns = job["namespace"]
            if ns not in jobs_by_namespace:
                jobs_by_namespace[ns] = []
            jobs_by_namespace[ns].append(job)
        
        result_data = {
            "status_filter": status,
            "jobs_by_namespace": jobs_by_namespace,
            "total_job_count": len(filtered_jobs),
            "namespace_count": len(jobs_by_namespace)
        }
    else:
        result_data = {
            "namespace": namespace,
            "status_filter": status,
            "jobs": filtered_jobs,
            "job_count": len(filtered_jobs)
        }
    
    return {"status": "success", "data": result_data}


list_jobs_by_status_tool = StructuredTool.from_function(
    func=list_jobs_by_status,
    name="list_jobs_by_status",
    description="Lists Kubernetes jobs filtered by status (Running, Completed, Failed, Pending) with detailed job information and optional namespace filtering.",
    args_schema=JobStatusInputSchema
)


# ===============================================================================
#                              CRONJOB TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def list_cronjobs(namespace: Optional[str] = None) -> Dict[str, Any]:
    """Lists all CronJobs in a namespace or across all namespaces."""
    batch_v1 = get_batch_v1_api()
    
    if namespace:
        cronjob_list = batch_v1.list_namespaced_cron_job(namespace=namespace, timeout_seconds=10)
    else:
        cronjob_list = batch_v1.list_cron_job_for_all_namespaces(timeout_seconds=10)
    
    cronjobs = []
    for cronjob in cronjob_list.items:
        # Parse last schedule time
        last_schedule_time = None
        if cronjob.status.last_schedule_time:
            last_schedule_time = cronjob.status.last_schedule_time.replace(tzinfo=None)
        
        # Calculate next run time
        next_run_time = _calculate_next_cron_run(cronjob.spec.schedule, last_schedule_time)
        
        cronjob_info = {
            "name": cronjob.metadata.name,
            "namespace": cronjob.metadata.namespace,
            "schedule": cronjob.spec.schedule,
            "suspended": cronjob.spec.suspend or False,
            "timezone": getattr(cronjob.spec, 'time_zone', None),
            "concurrency_policy": cronjob.spec.concurrency_policy,
            "starting_deadline_seconds": cronjob.spec.starting_deadline_seconds,
            "successful_jobs_history_limit": cronjob.spec.successful_jobs_history_limit,
            "failed_jobs_history_limit": cronjob.spec.failed_jobs_history_limit,
            "creation_timestamp": cronjob.metadata.creation_timestamp.isoformat() if cronjob.metadata.creation_timestamp else None,
            "age": _calculate_age(cronjob.metadata.creation_timestamp),
            "labels": cronjob.metadata.labels or {},
            "last_schedule_time": cronjob.status.last_schedule_time.isoformat() if cronjob.status.last_schedule_time else None,
            "next_schedule_time": next_run_time.isoformat() if next_run_time else None,
            "active_jobs": len(cronjob.status.active) if cronjob.status.active else 0
        }
        
        # Add container information
        if cronjob.spec.job_template.spec.template.spec.containers:
            container = cronjob.spec.job_template.spec.template.spec.containers[0]
            cronjob_info["container_image"] = container.image
            cronjob_info["container_name"] = container.name
        
        # Add active job names
        if cronjob.status.active:
            cronjob_info["active_job_names"] = [job.name for job in cronjob.status.active]
        else:
            cronjob_info["active_job_names"] = []
        
        cronjobs.append(cronjob_info)
    
    # Group by namespace if querying all namespaces
    if not namespace:
        cronjobs_by_namespace = {}
        for cronjob in cronjobs:
            ns = cronjob["namespace"]
            if ns not in cronjobs_by_namespace:
                cronjobs_by_namespace[ns] = []
            cronjobs_by_namespace[ns].append(cronjob)
        
        result_data = {
            "cronjobs_by_namespace": cronjobs_by_namespace,
            "total_cronjob_count": len(cronjobs),
            "namespace_count": len(cronjobs_by_namespace),
            "croniter_available": CRONITER_AVAILABLE
        }
    else:
        result_data = {
            "namespace": namespace,
            "cronjobs": cronjobs,
            "cronjob_count": len(cronjobs),
            "croniter_available": CRONITER_AVAILABLE
        }
    
    return {"status": "success", "data": result_data}


list_cronjobs_tool = StructuredTool.from_function(
    func=list_cronjobs,
    name="list_cronjobs",
    description="Lists all Kubernetes CronJobs in a namespace or across all namespaces with comprehensive scheduling information including next run times and active jobs.",
    args_schema=NamespaceOptionalInputSchema
)


@_handle_k8s_exceptions
def describe_cronjob(namespace: str, cronjob_name: str) -> Dict[str, Any]:
    """Gets detailed information about a specific CronJob."""
    batch_v1 = get_batch_v1_api()
    
    cronjob = batch_v1.read_namespaced_cron_job(name=cronjob_name, namespace=namespace)
    
    # Parse last schedule time
    last_schedule_time = None
    if cronjob.status.last_schedule_time:
        last_schedule_time = cronjob.status.last_schedule_time.replace(tzinfo=None)
    
    # Calculate next run time
    next_run_time = _calculate_next_cron_run(cronjob.spec.schedule, last_schedule_time)
    
    cronjob_details = {
        "name": cronjob.metadata.name,
        "namespace": cronjob.metadata.namespace,
        "labels": cronjob.metadata.labels or {},
        "annotations": cronjob.metadata.annotations or {},
        "creation_timestamp": cronjob.metadata.creation_timestamp.isoformat() if cronjob.metadata.creation_timestamp else None,
        "age": _calculate_age(cronjob.metadata.creation_timestamp),
        "uid": cronjob.metadata.uid,
        "spec": {
            "schedule": cronjob.spec.schedule,
            "timezone": getattr(cronjob.spec, 'time_zone', None),
            "suspend": cronjob.spec.suspend or False,
            "concurrency_policy": cronjob.spec.concurrency_policy,
            "starting_deadline_seconds": cronjob.spec.starting_deadline_seconds,
            "successful_jobs_history_limit": cronjob.spec.successful_jobs_history_limit,
            "failed_jobs_history_limit": cronjob.spec.failed_jobs_history_limit
        },
        "status": {
            "last_schedule_time": cronjob.status.last_schedule_time.isoformat() if cronjob.status.last_schedule_time else None,
            "next_schedule_time": next_run_time.isoformat() if next_run_time else None,
            "active_jobs": len(cronjob.status.active) if cronjob.status.active else 0,
            "last_successful_time": cronjob.status.last_successful_time.isoformat() if cronjob.status.last_successful_time else None
        },
        "job_template": {
            "parallelism": cronjob.spec.job_template.spec.parallelism,
            "completions": cronjob.spec.job_template.spec.completions,
            "backoff_limit": cronjob.spec.job_template.spec.backoff_limit,
            "active_deadline_seconds": cronjob.spec.job_template.spec.active_deadline_seconds,
            "ttl_seconds_after_finished": cronjob.spec.job_template.spec.ttl_seconds_after_finished,
            "restart_policy": cronjob.spec.job_template.spec.template.spec.restart_policy
        },
        "containers": []
    }
    
    # Extract container information from job template
    if cronjob.spec.job_template.spec.template.spec.containers:
        for container in cronjob.spec.job_template.spec.template.spec.containers:
            container_info = {
                "name": container.name,
                "image": container.image,
                "command": container.command,
                "args": container.args,
                "env_vars": []
            }
            
            # Extract environment variables
            if container.env:
                for env_var in container.env:
                    env_info = {"name": env_var.name}
                    if env_var.value:
                        env_info["value"] = env_var.value
                    elif env_var.value_from:
                        if env_var.value_from.config_map_key_ref:
                            env_info["value_from"] = f"ConfigMap/{env_var.value_from.config_map_key_ref.name}:{env_var.value_from.config_map_key_ref.key}"
                        elif env_var.value_from.secret_key_ref:
                            env_info["value_from"] = f"Secret/{env_var.value_from.secret_key_ref.name}:{env_var.value_from.secret_key_ref.key}"
                    
                    container_info["env_vars"].append(env_info)
            
            # Extract resource requirements
            if container.resources:
                container_info["resources"] = {}
                if container.resources.requests:
                    container_info["resources"]["requests"] = dict(container.resources.requests)
                if container.resources.limits:
                    container_info["resources"]["limits"] = dict(container.resources.limits)
            
            cronjob_details["containers"].append(container_info)
    
    # Add active job details if any
    if cronjob.status.active:
        cronjob_details["active_jobs"] = []
        for job_ref in cronjob.status.active:
            job_info = {
                "name": job_ref.name,
                "namespace": job_ref.namespace,
                "uid": job_ref.uid
            }
            cronjob_details["active_jobs"].append(job_info)
    
    # Add schedule analysis
    cronjob_details["schedule_analysis"] = {
        "croniter_available": CRONITER_AVAILABLE,
        "schedule_valid": True  # We'll assume valid if we got this far
    }
    
    if CRONITER_AVAILABLE:
        try:
            from croniter import croniter
            cron = croniter(cronjob.spec.schedule, datetime.utcnow())
            
            # Get next 5 run times
            next_runs = []
            for i in range(5):
                next_time = cron.get_next(datetime)
                next_runs.append(next_time.isoformat())
            
            cronjob_details["schedule_analysis"]["next_5_runs"] = next_runs
        except Exception as e:
            cronjob_details["schedule_analysis"]["schedule_valid"] = False
            cronjob_details["schedule_analysis"]["error"] = str(e)
    
    return {"status": "success", "data": cronjob_details}


describe_cronjob_tool = StructuredTool.from_function(
    func=describe_cronjob,
    name="describe_cronjob",
    description="Gets comprehensive detailed information about a specific Kubernetes CronJob including schedule analysis, job template, containers, and active jobs.",
    args_schema=CronJobInputSchema
)


@_handle_k8s_exceptions
def list_suspended_cronjobs(namespace: Optional[str] = None) -> Dict[str, Any]:
    """Lists all suspended CronJobs."""
    batch_v1 = get_batch_v1_api()
    
    if namespace:
        cronjob_list = batch_v1.list_namespaced_cron_job(namespace=namespace, timeout_seconds=10)
    else:
        cronjob_list = batch_v1.list_cron_job_for_all_namespaces(timeout_seconds=10)
    
    suspended_cronjobs = []
    
    for cronjob in cronjob_list.items:
        if cronjob.spec.suspend:
            cronjob_info = {
                "name": cronjob.metadata.name,
                "namespace": cronjob.metadata.namespace,
                "schedule": cronjob.spec.schedule,
                "age": _calculate_age(cronjob.metadata.creation_timestamp),
                "last_schedule_time": cronjob.status.last_schedule_time.isoformat() if cronjob.status.last_schedule_time else None,
                "suspension_reason": "Manually suspended"
            }
            
            # Check annotations for suspension reason
            if cronjob.metadata.annotations:
                for key, value in cronjob.metadata.annotations.items():
                    if 'suspend' in key.lower() or 'disable' in key.lower():
                        cronjob_info["suspension_reason"] = f"{key}: {value}"
                        break
            
            suspended_cronjobs.append(cronjob_info)
    
    # Group by namespace if querying all namespaces
    if not namespace:
        suspended_by_namespace = {}
        for cronjob in suspended_cronjobs:
            ns = cronjob["namespace"]
            if ns not in suspended_by_namespace:
                suspended_by_namespace[ns] = []
            suspended_by_namespace[ns].append(cronjob)
        
        result_data = {
            "suspended_cronjobs_by_namespace": suspended_by_namespace,
            "total_suspended_count": len(suspended_cronjobs),
            "namespace_count": len(suspended_by_namespace)
        }
    else:
        result_data = {
            "namespace": namespace,
            "suspended_cronjobs": suspended_cronjobs,
            "suspended_count": len(suspended_cronjobs)
        }
    
    return {"status": "success", "data": result_data}


list_suspended_cronjobs_tool = StructuredTool.from_function(
    func=list_suspended_cronjobs,
    name="list_suspended_cronjobs",
    description="Lists all suspended Kubernetes CronJobs with suspension reasons and last execution information.",
    args_schema=NamespaceOptionalInputSchema
)


# ===============================================================================
#                            JOB ANALYSIS TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def analyze_job_performance() -> Dict[str, Any]:
    """Analyzes job performance across the cluster."""
    batch_v1 = get_batch_v1_api()
    
    # Get all jobs
    job_list = batch_v1.list_job_for_all_namespaces(timeout_seconds=10)
    
    analysis = {
        "total_jobs": len(job_list.items),
        "status_breakdown": {
            "running": 0,
            "completed": 0,
            "failed": 0,
            "pending": 0
        },
        "completion_times": [],
        "failure_analysis": {
            "total_failures": 0,
            "common_failure_reasons": {},
            "backoff_limit_exceeded": 0
        },
        "resource_usage": {
            "high_parallelism_jobs": [],
            "long_running_jobs": [],
            "resource_intensive_jobs": []
        },
        "namespace_breakdown": {}
    }
    
    now = datetime.utcnow()
    
    for job in job_list.items:
        namespace = job.metadata.namespace
        job_status = _parse_job_status(job.status)
        
        # Update namespace breakdown
        if namespace not in analysis["namespace_breakdown"]:
            analysis["namespace_breakdown"][namespace] = {
                "total": 0,
                "running": 0,
                "completed": 0,
                "failed": 0,
                "pending": 0
            }
        
        analysis["namespace_breakdown"][namespace]["total"] += 1
        
        # Status breakdown
        status = job_status["overall_status"].lower()
        if status == "running":
            analysis["status_breakdown"]["running"] += 1
            analysis["namespace_breakdown"][namespace]["running"] += 1
        elif status == "completed":
            analysis["status_breakdown"]["completed"] += 1
            analysis["namespace_breakdown"][namespace]["completed"] += 1
        elif status == "failed":
            analysis["status_breakdown"]["failed"] += 1
            analysis["namespace_breakdown"][namespace]["failed"] += 1
            
            # Failure analysis
            analysis["failure_analysis"]["total_failures"] += 1
            
            for condition in job_status["conditions"]:
                if condition["type"] == "Failed" and condition["reason"]:
                    reason = condition["reason"]
                    analysis["failure_analysis"]["common_failure_reasons"][reason] = \
                        analysis["failure_analysis"]["common_failure_reasons"].get(reason, 0) + 1
            
            # Check if backoff limit exceeded
            if job_status["failed"] >= (job.spec.backoff_limit or 6):
                analysis["failure_analysis"]["backoff_limit_exceeded"] += 1
        else:
            analysis["status_breakdown"]["pending"] += 1
            analysis["namespace_breakdown"][namespace]["pending"] += 1
        
        # Completion time analysis
        if job_status["start_time"] and job_status["completion_time"]:
            start_time = datetime.fromisoformat(job_status["start_time"].replace('Z', '+00:00'))
            completion_time = datetime.fromisoformat(job_status["completion_time"].replace('Z', '+00:00'))
            duration = (completion_time - start_time).total_seconds()
            analysis["completion_times"].append(duration)
        
        # Resource usage analysis
        if job.spec.parallelism and job.spec.parallelism > 10:
            analysis["resource_usage"]["high_parallelism_jobs"].append({
                "name": job.metadata.name,
                "namespace": job.metadata.namespace,
                "parallelism": job.spec.parallelism
            })
        
        # Long running jobs (active for more than 24 hours)
        if job_status["overall_status"] == "Running" and job_status["start_time"]:
            start_time = datetime.fromisoformat(job_status["start_time"].replace('Z', '+00:00'))
            start_time = start_time.replace(tzinfo=None)
            duration = now - start_time
            
            if duration.total_seconds() > 86400:  # 24 hours
                analysis["resource_usage"]["long_running_jobs"].append({
                    "name": job.metadata.name,
                    "namespace": job.metadata.namespace,
                    "duration_hours": duration.total_seconds() / 3600
                })
    
    # Calculate completion time statistics
    if analysis["completion_times"]:
        times = analysis["completion_times"]
        analysis["completion_time_stats"] = {
            "average_seconds": sum(times) / len(times),
            "min_seconds": min(times),
            "max_seconds": max(times),
            "median_seconds": sorted(times)[len(times) // 2]
        }
    
    # Sort common failure reasons
    analysis["failure_analysis"]["common_failure_reasons"] = dict(
        sorted(analysis["failure_analysis"]["common_failure_reasons"].items(),
               key=lambda x: x[1], reverse=True)
    )
    
    return {"status": "success", "data": analysis}


analyze_job_performance_tool = StructuredTool.from_function(
    func=analyze_job_performance,
    name="analyze_job_performance",
    description="Provides comprehensive analysis of job performance across the cluster including status breakdown, completion times, failure analysis, and resource usage patterns.",
    args_schema=NoArgumentsInputSchema
)


# ===============================================================================
#                           CREATE TOOLS
# ===============================================================================

class CreateCronJobInputSchema(BaseModel):
    namespace: str = Field(description="The Kubernetes namespace where the CronJob will be created.")
    name: str = Field(description="The name of the CronJob.")
    schedule: str = Field(description="Cron schedule expression, e.g. '*/5 * * * *' for every 5 minutes.")
    image: str = Field(description="Container image to use for the CronJob, e.g. 'busybox:latest'.")
    command: list = Field(description="Command to run in the container as a list, e.g. ['sh', '-c', 'echo hello'].")
    restart_policy: str = Field(default="OnFailure", description="Restart policy for the Job pods. Options: OnFailure, Never.")


@_handle_k8s_exceptions
def create_cronjob(namespace: str, name: str, schedule: str, image: str, command: list, restart_policy: str = "OnFailure") -> Dict[str, Any]:
    """Creates a CronJob in the specified namespace with the given schedule and command."""
    from kubernetes import client as k8s_client
    batch_v1 = get_batch_v1_api()
    safe_name = name.replace("_", "-").lower()
    container = k8s_client.V1Container(
        name=safe_name,
        image=image,
        command=command,
    )
    job_template = k8s_client.V1JobTemplateSpec(
        spec=k8s_client.V1JobSpec(
            template=k8s_client.V1PodTemplateSpec(
                spec=k8s_client.V1PodSpec(
                    containers=[container],
                    restart_policy=restart_policy,
                )
            )
        )
    )
    cronjob = k8s_client.V1CronJob(
        api_version="batch/v1",
        kind="CronJob",
        metadata=k8s_client.V1ObjectMeta(name=safe_name, namespace=namespace),
        spec=k8s_client.V1CronJobSpec(
            schedule=schedule,
            job_template=job_template,
        ),
    )
    resp = batch_v1.create_namespaced_cron_job(namespace=namespace, body=cronjob)
    return {
        "status": "success",
        "message": f"CronJob '{safe_name}' created in namespace '{namespace}' with schedule '{schedule}'.",
        "data": {"name": resp.metadata.name, "namespace": resp.metadata.namespace, "schedule": schedule},
    }


create_cronjob_tool = StructuredTool.from_function(
    func=create_cronjob,
    name="create_cronjob",
    description="Creates a Kubernetes CronJob in the specified namespace with a cron schedule and a container command.",
    args_schema=CreateCronJobInputSchema,
)


class DeleteCronJobInputSchema(BaseModel):
    namespace: str = Field(..., description="Kubernetes namespace containing the CronJob.")
    name: str = Field(..., description="Name of the CronJob to delete.")


@_handle_k8s_exceptions
def delete_cronjob(namespace: str, name: str) -> Dict[str, Any]:
    batch_v1 = get_batch_v1_api()
    batch_v1.delete_namespaced_cron_job(name=name, namespace=namespace)
    return {"status": "success", "message": f"CronJob '{name}' deleted from namespace '{namespace}'."}


delete_cronjob_tool = StructuredTool.from_function(
    func=delete_cronjob,
    name="delete_cronjob",
    description="Deletes a Kubernetes CronJob from the specified namespace.",
    args_schema=DeleteCronJobInputSchema,
)


class DeleteJobInputSchema(BaseModel):
    namespace: str = Field(..., description="Kubernetes namespace containing the Job.")
    name: str = Field(..., description="Name of the Job to delete.")


@_handle_k8s_exceptions
def delete_job(namespace: str, name: str) -> Dict[str, Any]:
    batch_v1 = get_batch_v1_api()
    batch_v1.delete_namespaced_job(name=name, namespace=namespace)
    return {"status": "success", "message": f"Job '{name}' deleted from namespace '{namespace}'."}


delete_job_tool = StructuredTool.from_function(
    func=delete_job,
    name="delete_job",
    description="Deletes a Kubernetes Job from the specified namespace.",
    args_schema=DeleteJobInputSchema,
)


# ===============================================================================
#                           TOOL COLLECTION
# ===============================================================================

# Export all job tools for easy import
jobs_tools = [
    connectivity_check_tool,
    list_jobs_tool,
    describe_job_tool,
    list_failed_jobs_tool,
    list_jobs_by_status_tool,
    list_cronjobs_tool,
    describe_cronjob_tool,
    list_suspended_cronjobs_tool,
    analyze_job_performance_tool,
    create_cronjob_tool,
    delete_cronjob_tool,
    delete_job_tool,
]

