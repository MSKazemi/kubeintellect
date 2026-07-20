#!/usr/bin/env python3
"""
Tool Organization Script

This script organizes Kubernetes tools based on agent assignments.
It moves tools from the centralized ai_generated_tools.py to agent-specific files.
"""

# Tool assignments based on the provided table
tool_assignments = {
    "AdvancedOps": [
        "apply_test_namespace_with_quota_tool",
        "check_deprecated_api_pods_tool", 
        "check_deprecated_api_versions_tool",
        "check_loadbalancer_external_ips_tool",
        "check_pods_using_deprecated_apis_tool",
        "connectivity_check_tool",
        "list_ingress_in_namespace_tool",
        "list_kubernetes_services_tool",
        "list_persistent_volume_claims_tool",
        "list_persistent_volumes_tool"
    ],
    "Configs": [
        "check_deployments_missing_resource_limits_tool",
        "check_deployments_without_affinity_tool",
        "create_namespace_with_resource_quota_tool",
        "describe_kubernetes_deployment_tool",
        "describe_kubernetes_pod_tool",
        "find_deployments_with_multiple_replicas_no_affinity_tool",
        "get_recent_deployment_changelog_tool",
        "list_all_pods_tool",
        "list_configmaps_in_namespace_tool",
        "list_cronjobs_with_next_run_tool",
        "list_cronjobs_with_next_schedule_tool",
        "list_daemonsets_in_namespace_tool",
        "list_deployments_in_namespace_tool",
        "list_jobs_in_namespace_tool",
        "list_kubernetes_deployments_tool",
        "list_kubernetes_jobs_tool",
        "list_pods_with_two_containers_tool",
        "list_replicasets_in_namespace_tool",
        "list_resources_with_label_tool",
        "list_statefulsets_tool"
    ],
    "Execution": [
        "execute_command_in_pod_tool"
    ],
    "Metrics": [
        "get_kubelet_not_ready_nodes_tool",
        "get_kubernetes_nodes_info_tool",
        "get_persistent_volumes_usage_tool",
        "get_pod_resource_usage_trends_tool",
        "list_kubernetes_nodes_tool",
        "measure_network_bandwidth_usage_tool"
    ],
    "Logs": [
        "list_error_pods_tool",
        "list_failed_jobs_last_24_hours_tool",
        "list_namespace_events_tool"
    ]
}

def get_agent_for_tool(tool_name: str) -> str:
    """Get the agent assignment for a given tool name."""
    for agent, tools in tool_assignments.items():
        if tool_name in tools:
            return agent
    return "Unknown"

def print_tool_distribution():
    """Print the distribution of tools by agent."""
    print("Tool Distribution by Agent:")
    print("=" * 40)
    
    for agent, tools in tool_assignments.items():
        print(f"\n{agent}: ({len(tools)} tools)")
        for tool in tools:
            print(f"  - {tool}")
    
    # Calculate totals
    total_tools = sum(len(tools) for tools in tool_assignments.values())
    print(f"\nTotal tools to organize: {total_tools}")

if __name__ == "__main__":
    print_tool_distribution() 