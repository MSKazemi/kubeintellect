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

