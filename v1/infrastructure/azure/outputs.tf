# Cluster Information
output "cluster_name" {
  description = "Name of the AKS cluster"
  value       = azurerm_kubernetes_cluster.aks.name
}

output "cluster_fqdn" {
  description = "FQDN of the AKS cluster"
  value       = azurerm_kubernetes_cluster.aks.fqdn
}

output "cluster_location" {
  description = "Location of the AKS cluster"
  value       = azurerm_kubernetes_cluster.aks.location
}

output "resource_group_name" {
  description = "Name of the resource group"
  value       = azurerm_resource_group.kubeintellect.name
}

# Kubernetes Configuration
output "kube_config" {
  description = "Kubernetes configuration file content"
  value       = azurerm_kubernetes_cluster.aks.kube_config_raw
  sensitive   = true
}

output "cluster_identity" {
  description = "The managed identity used by the AKS cluster"
  value       = azurerm_kubernetes_cluster.aks.identity[0].principal_id
}

# Network Information
output "cluster_private_fqdn" {
  description = "Private FQDN of the AKS cluster (if private cluster)"
  value       = azurerm_kubernetes_cluster.aks.private_fqdn
}

# Access Commands
output "kubectl_config_command" {
  description = "Command to configure kubectl for this cluster"
  value       = "az aks get-credentials --resource-group ${azurerm_resource_group.kubeintellect.name} --name ${azurerm_kubernetes_cluster.aks.name}"
}

# Service URLs (will be populated after ingress controller gets external IP)
output "grafana_url" {
  description = "URL to access Grafana (after adding to /etc/hosts)"
  value       = "http://${var.grafana_hostname}"
}

output "prometheus_url" {
  description = "URL to access Prometheus (after adding to /etc/hosts)"
  value       = "http://${var.prometheus_hostname}"
}

# Monitoring
output "log_analytics_workspace_id" {
  description = "ID of the Log Analytics workspace (if monitoring is enabled)"
  value       = var.enable_monitoring ? azurerm_log_analytics_workspace.main[0].id : null
}

# Useful Commands
output "get_ingress_ip_command" {
  description = "Command to get the ingress controller IP address"
  value       = "kubectl get svc -n ingress-nginx ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].ip}'"
}

output "get_grafana_password_command" {
  description = "Command to retrieve Grafana admin password"
  value       = "kubectl get secret -n monitoring kube-prom-stack-grafana -o jsonpath='{.data.admin-password}' | base64 -d"
}

output "hosts_file_entries" {
  description = "Entries to add to /etc/hosts file (replace <INGRESS_IP> with actual IP)"
  value       = "<INGRESS_IP> ${var.grafana_hostname}\n<INGRESS_IP> ${var.prometheus_hostname}"
}
