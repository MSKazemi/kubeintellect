# app/agents/tools/execution_tools.py
"""
Execution Tools for Kubernetes Command Execution

This module contains tools for executing commands within Kubernetes pods
and performing runtime operations.
"""
import json
from datetime import datetime
from typing import List, Dict, Any, Optional

from kubernetes.client.exceptions import ApiException
from kubernetes.stream import stream
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.agents.tools.tools_lib._base import (
    get_core_v1_api,
    _handle_k8s_exceptions,
    calculate_age as _calculate_age,
)
from app.services import kubernetes_service
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")


# ===============================================================================
#                             EXCEPTION DEFINITIONS
# ===============================================================================

class ExecutionError(Exception):
    """Raised when command execution fails."""
    pass


# ===============================================================================
#                              HELPER FUNCTIONS
# ===============================================================================

def _parse_command_output(output: str) -> Dict[str, Any]:
    """Parse command output and extract useful information."""
    lines = output.split('\n')
    result = {
        "raw_output": output,
        "line_count": len(lines),
        "non_empty_lines": len([line for line in lines if line.strip()]),
        "output_size_bytes": len(output.encode('utf-8'))
    }
    
    # Try to detect if output is JSON
    if output.strip().startswith(('{', '[')):
        try:
            parsed_json = json.loads(output.strip())
            result["json_parsed"] = True
            result["parsed_data"] = parsed_json
        except json.JSONDecodeError:
            result["json_parsed"] = False
    
    return result


def _get_shell_for_container(pod_name: str, namespace: str, container_name: Optional[str] = None) -> List[str]:
    """Determine the best shell to use for command execution."""
    core_v1 = get_core_v1_api()
    
    # Get pod details to understand the container
    try:
        pod = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)

        # If container_name not specified, use first container
        if not container_name and pod.spec.containers:
            container_name = pod.spec.containers[0].name
        
        # Try different shells in order of preference
        shells_to_try = ["/bin/bash", "/bin/sh", "/bin/ash"]
        
        for shell in shells_to_try:
            try:
                # Test if shell exists
                test_command = [shell, "-c", "echo 'shell_test'"]
                response = stream(
                    core_v1.connect_get_namespaced_pod_exec,
                    name=pod_name,
                    namespace=namespace,
                    container=container_name,
                    command=test_command,
                    stderr=True,
                    stdin=False,
                    stdout=True,
                    tty=False,
                    _request_timeout=5
                )
                
                if "shell_test" in response:
                    return [shell, "-c"]
            except Exception:
                continue

        # Fallback to sh
        return ["/bin/sh", "-c"]

    except Exception:
        # Default fallback
        return ["/bin/sh", "-c"]


# ===============================================================================
#                               INPUT SCHEMAS
# ===============================================================================

class PodInputSchema(BaseModel):
    """Schema for tools that require pod name and namespace."""
    namespace: str = Field(description="The Kubernetes namespace where the pod is located.")
    pod_name: str = Field(description="The name of the Kubernetes pod.")


class ExecuteCommandInputSchema(BaseModel):
    """Schema for command execution in pods."""
    namespace: str = Field(description="The Kubernetes namespace where the pod is located.")
    pod_name: str = Field(description="The name of the Kubernetes pod in which to execute the command.")
    command: str = Field(description="The shell command to execute inside the pod.")
    container_name: Optional[str] = Field(default=None, description="The name of the container within the pod. If not specified, uses the first container.")
    timeout_seconds: Optional[int] = Field(default=30, description="Timeout for command execution in seconds.")


class FileOperationInputSchema(BaseModel):
    """Schema for file operations in pods."""
    namespace: str = Field(description="The Kubernetes namespace where the pod is located.")
    pod_name: str = Field(description="The name of the Kubernetes pod.")
    file_path: str = Field(description="The path to the file in the pod.")
    container_name: Optional[str] = Field(default=None, description="The name of the container within the pod.")


class CopyFileInputSchema(BaseModel):
    """Schema for copying files to/from pods."""
    namespace: str = Field(description="The Kubernetes namespace where the pod is located.")
    pod_name: str = Field(description="The name of the Kubernetes pod.")
    source_path: str = Field(description="The source file path.")
    destination_path: str = Field(description="The destination file path.")
    container_name: Optional[str] = Field(default=None, description="The name of the container within the pod.")
    direction: str = Field(description="Direction of copy: 'to_pod' or 'from_pod'.")


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
#                            COMMAND EXECUTION TOOLS
# ===============================================================================

@_handle_k8s_exceptions
def execute_command_in_pod(namespace: str, pod_name: str, command: str, container_name: Optional[str] = None, timeout_seconds: int = 30) -> Dict[str, Any]:
    """Execute a command in a Kubernetes pod with enhanced capabilities."""
    core_v1 = get_core_v1_api()
    
    # Get pod information first
    try:
        pod = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
    except ApiException as e:
        if e.status == 404:
            return {
                "status": "error",
                "message": f"Pod '{pod_name}' not found in namespace '{namespace}'",
                "error_type": "PodNotFound"
            }
        raise
    
    # Determine container name if not provided
    if not container_name:
        if pod.spec.containers:
            container_name = pod.spec.containers[0].name
        else:
            return {
                "status": "error",
                "message": f"No containers found in pod '{pod_name}'",
                "error_type": "NoContainers"
            }
    
    # Validate container exists
    container_names = [c.name for c in pod.spec.containers]
    if container_name not in container_names:
        return {
            "status": "error",
            "message": f"Container '{container_name}' not found in pod. Available containers: {container_names}",
            "error_type": "ContainerNotFound"
        }
    
    # Check pod status
    if pod.status.phase not in ["Running"]:
        return {
            "status": "error",
            "message": f"Pod is not in Running state. Current phase: {pod.status.phase}",
            "error_type": "PodNotReady"
        }
    
    # Get optimal shell for the container
    exec_command = _get_shell_for_container(pod_name, namespace, container_name)
    exec_command.append(command)
    
    # Record execution start time
    start_time = datetime.utcnow()
    
    try:
        # Execute the command
        logger.info(f"Executing command in pod {pod_name}/{container_name}: {command}")
        
        response = stream(
            core_v1.connect_get_namespaced_pod_exec,
            name=pod_name,
            namespace=namespace,
            container=container_name,
            command=exec_command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _request_timeout=timeout_seconds
        )
        
        end_time = datetime.utcnow()
        execution_duration = (end_time - start_time).total_seconds()
        
        # Parse output for additional insights
        parsed_output = _parse_command_output(response)
        
        result = {
            "status": "success",
            "data": {
                "output": response,
                "parsed_output": parsed_output,
                "execution_info": {
                    "pod_name": pod_name,
                    "namespace": namespace,
                    "container_name": container_name,
                    "command": command,
                    "shell_used": exec_command[0] if exec_command else "unknown",
                    "execution_duration_seconds": execution_duration,
                    "start_time": start_time.isoformat(),
                    "end_time": end_time.isoformat()
                },
                "pod_info": {
                    "phase": pod.status.phase,
                    "node_name": pod.spec.node_name,
                    "pod_ip": pod.status.pod_ip,
                    "creation_timestamp": pod.metadata.creation_timestamp.isoformat() if pod.metadata.creation_timestamp else None,
                    "age": _calculate_age(pod.metadata.creation_timestamp)
                }
            }
        }
        
        return result
        
    except Exception as e:
        end_time = datetime.utcnow()
        execution_duration = (end_time - start_time).total_seconds()
        
        return {
            "status": "error",
            "message": f"Command execution failed: {str(e)}",
            "error_type": "ExecutionFailed",
            "execution_info": {
                "pod_name": pod_name,
                "namespace": namespace,
                "container_name": container_name,
                "command": command,
                "execution_duration_seconds": execution_duration,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat()
            }
        }


execute_command_in_pod_tool = StructuredTool.from_function(
    func=execute_command_in_pod,
    name="execute_command_in_pod",
    description="Executes a shell command inside a Kubernetes pod with comprehensive execution details, automatic shell detection, and enhanced error handling.",
    args_schema=ExecuteCommandInputSchema
)


@_handle_k8s_exceptions
def read_file_from_pod(namespace: str, pod_name: str, file_path: str, container_name: Optional[str] = None) -> Dict[str, Any]:
    """Read a file from a Kubernetes pod."""
    command = f"cat '{file_path}'"
    
    result = execute_command_in_pod(namespace, pod_name, command, container_name)
    
    if result["status"] == "success":
        file_content = result["data"]["output"]
        
        # Add file-specific information
        file_info = {
            "file_path": file_path,
            "content": file_content,
            "content_length": len(file_content),
            "line_count": len(file_content.split('\n')) if file_content else 0,
            "is_binary": '\0' in file_content
        }
        
        # Try to detect file type
        if file_path.endswith(('.json',)):
            try:
                parsed_json = json.loads(file_content)
                file_info["file_type"] = "json"
                file_info["parsed_json"] = parsed_json
            except Exception:
                file_info["file_type"] = "text"
        elif file_path.endswith(('.yaml', '.yml')):
            file_info["file_type"] = "yaml"
        elif file_path.endswith(('.xml',)):
            file_info["file_type"] = "xml"
        else:
            file_info["file_type"] = "text"
        
        result["data"]["file_info"] = file_info
        
    return result


read_file_from_pod_tool = StructuredTool.from_function(
    func=read_file_from_pod,
    name="read_file_from_pod",
    description="Reads a file from a Kubernetes pod with automatic file type detection and content analysis.",
    args_schema=FileOperationInputSchema
)


@_handle_k8s_exceptions
def list_directory_in_pod(namespace: str, pod_name: str, directory_path: str = "/", container_name: Optional[str] = None) -> Dict[str, Any]:
    """List directory contents in a Kubernetes pod."""
    command = f"ls -la '{directory_path}'"
    
    result = execute_command_in_pod(namespace, pod_name, command, container_name)
    
    if result["status"] == "success":
        output_lines = result["data"]["output"].split('\n')
        
        # Parse ls -la output
        files = []
        directories = []
        
        for line in output_lines[1:]:  # Skip first line (total)
            if line.strip():
                parts = line.split()
                if len(parts) >= 9:
                    permissions = parts[0]
                    name = ' '.join(parts[8:])
                    
                    if name not in ['.', '..']:
                        item_info = {
                            "name": name,
                            "permissions": permissions,
                            "size": parts[4] if len(parts) > 4 else "unknown",
                            "modified": ' '.join(parts[5:8]) if len(parts) >= 8 else "unknown",
                            "type": "directory" if permissions.startswith('d') else "file"
                        }
                        
                        if permissions.startswith('d'):
                            directories.append(item_info)
                        else:
                            files.append(item_info)
        
        result["data"]["directory_info"] = {
            "path": directory_path,
            "files": files,
            "directories": directories,
            "file_count": len(files),
            "directory_count": len(directories),
            "total_items": len(files) + len(directories)
        }
    
    return result


list_directory_in_pod_tool = StructuredTool.from_function(
    func=list_directory_in_pod,
    name="list_directory_in_pod", 
    description="Lists directory contents in a Kubernetes pod with detailed file and directory information.",
    args_schema=FileOperationInputSchema
)


@_handle_k8s_exceptions
def check_pod_processes(namespace: str, pod_name: str, container_name: Optional[str] = None) -> Dict[str, Any]:
    """Check running processes in a Kubernetes pod."""
    command = "ps aux"
    
    result = execute_command_in_pod(namespace, pod_name, command, container_name)
    
    if result["status"] == "success":
        output_lines = result["data"]["output"].split('\n')
        processes = []
        
        # Parse ps aux output
        for line in output_lines[1:]:  # Skip header
            if line.strip():
                parts = line.split(None, 10)  # Split into max 11 parts
                if len(parts) >= 11:
                    process_info = {
                        "user": parts[0],
                        "pid": parts[1],
                        "cpu_percent": parts[2],
                        "memory_percent": parts[3],
                        "vsz": parts[4],
                        "rss": parts[5],
                        "tty": parts[6],
                        "stat": parts[7],
                        "start": parts[8],
                        "time": parts[9],
                        "command": parts[10]
                    }
                    processes.append(process_info)
        
        # Calculate summary statistics
        total_processes = len(processes)
        total_memory_percent = sum(float(p["memory_percent"]) for p in processes if p["memory_percent"] != "0.0")
        total_cpu_percent = sum(float(p["cpu_percent"]) for p in processes if p["cpu_percent"] != "0.0")
        
        result["data"]["process_info"] = {
            "processes": processes,
            "summary": {
                "total_processes": total_processes,
                "total_memory_percent": round(total_memory_percent, 2),
                "total_cpu_percent": round(total_cpu_percent, 2)
            }
        }
    
    return result


check_pod_processes_tool = StructuredTool.from_function(
    func=check_pod_processes,
    name="check_pod_processes",
    description="Checks running processes in a Kubernetes pod with detailed process information and resource usage statistics.",
    args_schema=PodInputSchema
)


@_handle_k8s_exceptions
def check_pod_resource_usage(namespace: str, pod_name: str, container_name: Optional[str] = None) -> Dict[str, Any]:
    """Check resource usage in a Kubernetes pod."""
    # Multiple commands to get comprehensive resource info
    commands = {
        "memory": "free -m",
        "disk": "df -h",
        "cpu_info": "cat /proc/cpuinfo | grep 'processor\\|model name' | head -10",
        "load_avg": "cat /proc/loadavg",
        "uptime": "uptime"
    }
    
    resource_info = {}
    
    for resource_type, command in commands.items():
        try:
            result = execute_command_in_pod(namespace, pod_name, command, container_name, timeout_seconds=10)
            if result["status"] == "success":
                resource_info[resource_type] = result["data"]["output"]
            else:
                resource_info[resource_type] = f"Error: {result.get('message', 'Unknown error')}"
        except Exception:
            resource_info[resource_type] = "Not available"
    
    return {
        "status": "success",
        "data": {
            "pod_name": pod_name,
            "namespace": namespace,
            "container_name": container_name,
            "resource_usage": resource_info,
            "collection_time": datetime.utcnow().isoformat()
        }
    }


check_pod_resource_usage_tool = StructuredTool.from_function(
    func=check_pod_resource_usage,
    name="check_pod_resource_usage",
    description="Checks comprehensive resource usage in a Kubernetes pod including memory, disk, CPU, and system information.",
    args_schema=PodInputSchema
)


# ===============================================================================
#                           TOOL COLLECTION
# ===============================================================================

# Export all execution tools for easy import
execution_tools = [
    connectivity_check_tool,
    execute_command_in_pod_tool,
    read_file_from_pod_tool,
    list_directory_in_pod_tool,
    check_pod_processes_tool,
    check_pod_resource_usage_tool,
] 

