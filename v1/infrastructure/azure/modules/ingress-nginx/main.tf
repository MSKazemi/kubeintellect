# modules/ingress-nginx/main.tf

# modules/ingress-nginx/main.tf

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

resource "helm_release" "ingress_nginx" {
  name             = "ingress-nginx"
  namespace        = "ingress-nginx"
  create_namespace = true
  chart            = "ingress-nginx"
  repository       = "https://kubernetes.github.io/ingress-nginx"
  # version          = "4.10.0" # optional, pin a version if needed

  values = [
    yamlencode({
      controller = {
        publishService = {
          enabled = true
        }
        service = {
          externalTrafficPolicy = "Local"
        }
      }
    })
  ]
}
