variable "location" {
  description = "Azure region"
  default     = "westeurope"
}

variable "tenant_id" {
  description = "Azure Tenant ID"
  type        = string
  sensitive   = true
}

variable "subscription_id" {
  description = "Azure Subscription ID"
  type        = string
  sensitive   = true
}

variable "resource_group_name" {
  description = "Name of the resource group"
  default     = "rg-kubeintellect"
}

variable "cluster_name" {
  description = "Name of the AKS cluster"
  default     = "aks-kubeintellect"
}

variable "dns_prefix" {
  description = "DNS prefix for AKS"
  default     = "kubeintellect"
}

variable "node_count" {
  description = "Number of nodes in the default pool"
  default     = 1
  type        = number
}

variable "node_vm_size" {
  description = "VM size for agent nodes"
  default     = "Standard_DS3_v2"
}

variable "environment" {
  description = "Environment tag"
  default     = "production"
}

# New variables for enhanced configuration
variable "kubernetes_version" {
  description = "Kubernetes version for the AKS cluster"
  type        = string
  default     = null
}

variable "enable_auto_scaling" {
  description = "Enable auto-scaling for the default node pool"
  type        = bool
  default     = false
}

variable "min_node_count" {
  description = "Minimum number of nodes when auto-scaling is enabled"
  type        = number
  default     = 1
}

variable "max_node_count" {
  description = "Maximum number of nodes when auto-scaling is enabled"
  type        = number
  default     = 5
}

variable "os_disk_size_gb" {
  description = "OS disk size in GB for agent nodes"
  type        = number
  default     = 128
}

variable "enable_monitoring" {
  description = "Enable Azure Monitor for containers"
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "Log Analytics workspace retention in days"
  type        = number
  default     = 30
}

variable "enable_azure_policy" {
  description = "Enable Azure Policy for Kubernetes"
  type        = bool
  default     = false
}

variable "automatic_channel_upgrade" {
  description = "Automatic channel upgrade for AKS cluster"
  type        = string
  default     = "patch"
  validation {
    condition     = contains(["patch", "rapid", "node-image", "stable", "none"], var.automatic_channel_upgrade)
    error_message = "Automatic channel upgrade must be one of: patch, rapid, node-image, stable, none."
  }
}

variable "grafana_hostname" {
  description = "Hostname for Grafana ingress"
  type        = string
  default     = "grafana.kubeintellect.local"
}

variable "prometheus_hostname" {
  description = "Hostname for Prometheus ingress"
  type        = string
  default     = "prometheus.kubeintellect.local"
}
