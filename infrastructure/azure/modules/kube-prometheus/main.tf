terraform {
  required_providers {
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.17"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.38"
    }
  }
}

resource "helm_release" "kube_prometheus" {
  name       = "kube-prom-stack"
  namespace  = "monitoring"
  chart      = "kube-prometheus-stack"
  repository = "https://prometheus-community.github.io/helm-charts"
  create_namespace = true
  version    = "79.4.0"

  values = [
    yamlencode({
      grafana = {
        ingress = {
          enabled     = true
          ingressClassName = "nginx"
          annotations = {
            "kubernetes.io/ingress.class" = "nginx"
            "nginx.ingress.kubernetes.io/rewrite-target" = "/"
          }
          hosts = [var.grafana_hostname]
        }
      }
      prometheus = {
        ingress = {
          enabled     = true
          ingressClassName = "nginx"
          annotations = {
            "nginx.ingress.kubernetes.io/rewrite-target" = "/"
          }
          hosts = [var.prometheus_hostname]
        }
      }
    })
  ]
}
