# KubeIntellect Infrastructure Configuration
# Improved version with fixed provider configuration

terraform {
  required_version = ">= 1.3"

  # Remote state backend — REQUIRED before the next deploy.
  # One-time bootstrap (run once per environment, not per apply):
  #   az storage account create --name stkubeintellectstate --resource-group rg-kubeintellect \
  #     --location westeurope --sku Standard_LRS --allow-blob-public-access false
  #   az storage container create --name tfstate --account-name stkubeintellectstate
  #   terraform init -migrate-state
  # Then uncomment the block below:
  #
  # backend "azurerm" {
  #   resource_group_name  = "rg-kubeintellect"
  #   storage_account_name = "stkubeintellectstate"
  #   container_name       = "tfstate"
  #   key                  = "kubeintellect.terraform.tfstate"
  #   use_oidc             = true
  # }

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.117"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.17"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.38"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }
}




provider "azurerm" {
  features {
    resource_group {
      # Allow deleting a resource group even when it still contains resources
      # not tracked by Terraform (e.g. Key Vault, Managed Identity created by
      # the ESO setup script). Without this flag, `terraform destroy` refuses
      # to delete the RG and the Azure CLI fallback is required every time.
      prevent_deletion_if_contains_resources = false
    }
    key_vault {
      # Do not soft-delete Key Vaults on destroy — purge immediately so the
      # same name can be reused on the next deploy without a name-conflict error.
      purge_soft_delete_on_destroy    = true
      recover_soft_deleted_key_vaults = false
    }
  }
  subscription_id = var.subscription_id
  tenant_id       = var.tenant_id
}

# Simplified provider configuration to avoid circular dependencies
# provider "kubernetes" {
#   config_path = "~/.kube/config"
# }

# provider "helm" {
#   kubernetes {
#     config_path = "~/.kube/config"
#   }
# }

# Kubernetes provider configured to use kubeconfig file
# The kubeconfig will be configured by null_resource.configure_kubectl after cluster creation
provider "kubernetes" {
  config_path = "~/.kube/config"
}

provider "helm" {
  kubernetes {
    config_path = "~/.kube/config"
  }
}





# Resource Group
resource "azurerm_resource_group" "kubeintellect" {
  name     = var.resource_group_name
  location = var.location

  tags = {
    environment = var.environment
    project     = "KubeIntellect"
    managed_by  = "terraform"
  }
}

# AKS Cluster with corrected configuration
resource "azurerm_kubernetes_cluster" "aks" {
  name                = var.cluster_name
  location            = azurerm_resource_group.kubeintellect.location
  resource_group_name = azurerm_resource_group.kubeintellect.name
  dns_prefix          = var.dns_prefix
  kubernetes_version  = var.kubernetes_version

  default_node_pool {
    name                = "agentpool"
    node_count          = var.node_count
    vm_size             = var.node_vm_size
    type                = "VirtualMachineScaleSets"
    enable_auto_scaling = var.enable_auto_scaling
    min_count           = var.enable_auto_scaling ? var.min_node_count : null
    max_count           = var.enable_auto_scaling ? var.max_node_count : null
    os_disk_type        = "Managed"
    os_disk_size_gb     = var.os_disk_size_gb

    # Enable better performance and security
    upgrade_settings {
      max_surge = "33%"
    }
  }

  identity {
    type = "SystemAssigned"
  }

  role_based_access_control_enabled = true

  network_profile {
    network_plugin      = "azure"
    load_balancer_sku   = "standard"
    outbound_type       = "loadBalancer"
  }

  # Enhanced monitoring and logging (corrected structure)
  dynamic "oms_agent" {
    for_each = var.enable_monitoring ? [1] : []
    content {
      log_analytics_workspace_id = azurerm_log_analytics_workspace.main[0].id
    }
  }

  # Security enhancements
  azure_policy_enabled = var.enable_azure_policy
  
  # Automatic upgrades
  automatic_channel_upgrade = var.automatic_channel_upgrade

  tags = {
    environment = var.environment
    project     = "KubeIntellect"
    managed_by  = "terraform"
  }

  lifecycle {
    ignore_changes = [
      default_node_pool[0].node_count
    ]
  }
}

# Optional Log Analytics Workspace for monitoring
resource "azurerm_log_analytics_workspace" "main" {
  count               = var.enable_monitoring ? 1 : 0
  name                = "${var.cluster_name}-logs"
  location            = azurerm_resource_group.kubeintellect.location
  resource_group_name = azurerm_resource_group.kubeintellect.name
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days

  tags = {
    environment = var.environment
    project     = "KubeIntellect"
    managed_by  = "terraform"
  }
}

# Configure kubectl after cluster is created
# This ensures kubeconfig is available for Kubernetes/Helm providers
resource "null_resource" "configure_kubectl" {
  depends_on = [azurerm_kubernetes_cluster.aks]

  provisioner "local-exec" {
    command = <<-EOT
      az aks get-credentials \
        --resource-group ${azurerm_resource_group.kubeintellect.name} \
        --name ${azurerm_kubernetes_cluster.aks.name} \
        --overwrite-existing
    EOT
  }

  triggers = {
    cluster_id = azurerm_kubernetes_cluster.aks.id
  }
}

# Ingress NGINX Module

module "ingress_nginx" {
  source = "./modules/ingress-nginx"
  providers = {
    helm       = helm
    kubernetes = kubernetes
  }
  depends_on = [
    azurerm_kubernetes_cluster.aks,
    null_resource.configure_kubectl
  ]
}
# Kube-Prometheus Stack Module

module "kube_prometheus" {
  source = "./modules/kube-prometheus"
  providers = {
    helm       = helm
    kubernetes = kubernetes
  }
  grafana_hostname    = var.grafana_hostname
  prometheus_hostname = var.prometheus_hostname
  depends_on = [
    azurerm_kubernetes_cluster.aks,
    null_resource.configure_kubectl
  ]
}





# Ingress resources are now configured via Helm values in the kube-prometheus module
# This avoids issues with kubernetes_manifest resources trying to connect during plan phase
