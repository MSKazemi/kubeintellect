# 🚀 KubeIntellect Infrastructure - Fully Automated AKS Deployment

This repository provides **fully automated** Azure Kubernetes Service (AKS) infrastructure provisioning using **Terraform** for deploying the **KubeIntellect** LLM-powered Kubernetes management system.

## ✨ **New: Fully Automated Deployment**

The deployment is now **100% automated** with intelligent error handling, interactive prompts, and automatic resource recovery. No manual intervention required!

---

## 🧱 Project Structure

```bash
infra-kubeintellect/
├── 📦 Core Infrastructure
│   ├── main.tf                  # Enhanced Terraform configuration
│   ├── variables.tf             # Comprehensive variable definitions
│   ├── outputs.tf               # Useful deployment outputs
│   └── terraform.tfvars.example # Configuration template
├── 🤖 Automation Scripts
│   ├── deploy-automated.sh      # 🆕 Fully automated deployment
│   ├── validate-environment.sh  # 🆕 Pre-deployment validation
│   ├── destroy.sh               # 🆕 Safe infrastructure destruction
│   ├── deploy.sh                # Simple wrapper → deploy-automated.sh
│   └── setup_aks_env.sh         # Environment setup
├── 📁 Modules
│   ├── modules/ingress-nginx/   # NGINX Ingress Controller
│   └── modules/kube-prometheus/ # Prometheus monitoring stack
├── 📄 Manifests
│   ├── manifests/grafana-ingress.yaml    # Grafana access
│   └── manifests/prometheus-ingress.yaml # Prometheus access
└── 📚 Documentation
    └── README.md                # This file
```

---

## 🔧 Prerequisites

* **Azure CLI** (`az`) installed and configured
* **Terraform** ≥ v1.3.0
* **Bash-compatible shell** (Linux/macOS/WSL)
* **Azure subscription** with AKS permissions
* **curl** (for validation checks)

---

## 🚀 **Quick Start (Recommended)**

### **Option 1: Fully Automated Deployment (New!)**

```bash
# 1. Clone the repository
git clone https://github.com/MSKazemi/KubeIntellect.git
cd infra-kubeintellect

# 2. Validate your environment
chmod +x validate-environment.sh
./validate-environment.sh

# 3. Run automated deployment
chmod +x deploy-automated.sh
./deploy-automated.sh
```

That's it! The script will:
- ✅ Check all prerequisites
- ✅ Prompt for Azure credentials interactively
- ✅ Validate and collect deployment configuration
- ✅ Handle resource conflicts automatically
- ✅ Deploy AKS cluster + monitoring stack
- ✅ Configure kubectl and ingress
- ✅ Provide access URLs and credentials

### **Option 2: Command Line Parameters**

```bash
./deploy-automated.sh \
  --subscription-id "your-subscription-id" \
  --tenant-id "your-tenant-id" \
  --resource-group "my-rg" \
  --cluster-name "my-aks" \
  --location "eastus" \
  --node-count 2
```

---

## 🎯 **Automation Features**

### **🔍 Pre-Deployment Validation**
- Checks all required tools and versions
- Validates Azure authentication and permissions
- Tests network connectivity
- Verifies Terraform configuration
- Provides detailed troubleshooting guidance

### **🤖 Intelligent Deployment**
- **Interactive Configuration**: Prompts for all required parameters with sensible defaults
- **Automatic Resource Import**: Detects and imports existing Azure resources
- **Error Recovery**: Automatically handles partial deployments and DNS timing issues
- **Progress Tracking**: Real-time deployment status with colored output
- **Validation**: Input validation with helpful error messages

### **📊 Post-Deployment Summary**
- Cluster access information
- Service URLs (Grafana, Prometheus)
- Kubectl configuration commands
- Grafana admin credentials
- `/etc/hosts` file entries

### **🗑️ Safe Destruction**
```bash
chmod +x destroy.sh
./destroy.sh
```

Features:
- Interactive confirmation prompts
- Resource inventory before destruction
- State backup options
- Force cleanup with Azure CLI fallback
- kubectl context cleanup

---

## 📚 **Deployment Methods**

### **Method 1: Standard Deployment (Recommended)**
```bash
./deploy.sh
# or
./deploy-automated.sh
```
*Both commands now run the same automated deployment script*

### **Method 2: Manual Terraform**
```bash
# Setup environment
./setup_aks_env.sh

# Configure variables
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your values

# Deploy
terraform init
terraform plan -var-file="terraform.tfvars"
terraform apply -var-file="terraform.tfvars"
```

---

## 🌐 **Accessing Your Services**

After deployment, the script provides:

### **1. Get Ingress IP**
```bash
kubectl get svc -n ingress-nginx ingress-nginx-controller
```

### **2. Update /etc/hosts**
```bash
echo "YOUR_INGRESS_IP grafana.kubeintellect.local" | sudo tee -a /etc/hosts
echo "YOUR_INGRESS_IP prometheus.kubeintellect.local" | sudo tee -a /etc/hosts
```

### **3. Access Services**
- **Grafana**: http://grafana.kubeintellect.local
- **Prometheus**: http://prometheus.kubeintellect.local

### **4. Get Grafana Credentials**
```bash
echo "Username: admin"
echo "Password: $(kubectl get secret -n monitoring kube-prom-stack-grafana -o jsonpath='{.data.admin-password}' | base64 -d)"
```

---

## ⚙️ **Configuration Options**

The automation script supports extensive customization:

| Variable | Description | Default |
|----------|-------------|---------|
| `resource_group_name` | Azure resource group | `rg-kubeintellect` |
| `cluster_name` | AKS cluster name | `aks-kubeintellect` |
| `location` | Azure region | `westeurope` |
| `node_count` | Number of nodes | `1` |
| `node_vm_size` | VM size for nodes | `Standard_DS3_v2` |
| `enable_auto_scaling` | Enable node auto-scaling | `false` |
| `enable_monitoring` | Enable Azure Monitor | `true` |
| `kubernetes_version` | Kubernetes version | `latest` |

---

## 🔧 **Advanced Features**

### **Auto-Scaling Configuration**
```hcl
enable_auto_scaling = true
min_node_count     = 1
max_node_count     = 5
```

### **Monitoring & Logging**
```hcl
enable_monitoring    = true
log_retention_days   = 30
enable_azure_policy  = true
```

### **Custom Hostnames**
```hcl
grafana_hostname    = "grafana.yourcompany.com"
prometheus_hostname = "prometheus.yourcompany.com"
```

---

## 🛠️ **Troubleshooting**

### **Common Issues & Solutions**

1. **DNS Resolution Issues**
   ```bash
   # Wait for DNS propagation
   kubectl get nodes
   az aks get-credentials --resource-group rg-kubeintellect --name aks-kubeintellect --overwrite-existing
   ```

2. **Resource Already Exists**
   ```bash
   # The automation script handles this automatically
   # Or manually import:
   terraform import azurerm_resource_group.kubeintellect /subscriptions/SUB_ID/resourceGroups/rg-kubeintellect
   ```

3. **Permission Issues**
   ```bash
   # Check Azure permissions
   az provider show --namespace Microsoft.ContainerService
   az provider show --namespace Microsoft.Network
   ```

### **Validation & Diagnostics**
```bash
# Run pre-deployment validation
./validate-environment.sh

# Check deployment status
terraform output
kubectl get all -A
kubectl get events -A
```

---

## 🔄 **Maintenance Operations**

### **Update Cluster**
```bash
# Plan updates
terraform plan -var-file="terraform.tfvars"

# Apply updates
terraform apply -var-file="terraform.tfvars"
```

### **Scale Cluster**
```bash
# Manually scale
az aks scale --resource-group rg-kubeintellect --name aks-kubeintellect --node-count 3

# Or enable auto-scaling
az aks update --resource-group rg-kubeintellect --name aks-kubeintellect --enable-cluster-autoscaler --min-count 1 --max-count 5
```

### **Backup & Recovery**
```bash
# Backup state before major changes
./destroy.sh --preserve

# Backup Kubernetes resources
kubectl get all --all-namespaces -o yaml > cluster-backup.yaml
```

---

## 🧹 **Cleanup**

### **Full Destruction**
```bash
# Interactive destruction
./destroy.sh

# Force destruction (no prompts)
./destroy.sh --force

# Preserve state files
./destroy.sh --preserve
```

### **Partial Cleanup**
```bash
# Remove only Helm releases
terraform destroy -target=module.ingress_nginx -target=module.kube_prometheus

# Remove only ingress manifests
terraform destroy -target=kubernetes_manifest.grafana_ingress -target=kubernetes_manifest.prometheus_ingress
```

---

## 🎛️ **Script Options**

### **deploy-automated.sh**
```bash
./deploy-automated.sh [options]

Options:
  -h, --help              Show help
  --subscription-id ID    Azure subscription ID
  --tenant-id ID          Azure tenant ID
  --resource-group NAME   Resource group name
  --cluster-name NAME     AKS cluster name
  --location LOCATION     Azure location
  --node-count COUNT      Number of nodes
  --vm-size SIZE          VM size for nodes
```

### **destroy.sh**
```bash
./destroy.sh [options]

Options:
  -h, --help          Show help
  -f, --force         Skip all confirmations
  -p, --preserve      Preserve state files
  --terraform-only    Use only Terraform
  --azure-only        Use only Azure CLI
```

---

## 📈 **What Gets Deployed**

### **Core Infrastructure**
- ✅ **Azure Resource Group**
- ✅ **AKS Cluster** with managed identity
- ✅ **System-assigned managed identity**
- ✅ **Azure CNI networking**
- ✅ **Standard load balancer**

### **Monitoring Stack**
- ✅ **Prometheus** for metrics collection
- ✅ **Grafana** for visualization and dashboards
- ✅ **AlertManager** for alerting
- ✅ **Node Exporter** for node metrics
- ✅ **Kube State Metrics** for cluster state

### **Ingress & Networking**
- ✅ **NGINX Ingress Controller**
- ✅ **External LoadBalancer** with public IP
- ✅ **Ingress rules** for Grafana and Prometheus
- ✅ **TLS termination ready**

### **Optional Components**
- ✅ **Azure Monitor integration** (configurable)
- ✅ **Log Analytics workspace** (configurable)
- ✅ **Azure Policy** (configurable)
- ✅ **Auto-scaling** (configurable)

---

## 👤 **Author & Support**

**Created by**: [Mohsen Seyedkazemi Ardebili](https://github.com/MSKazemi)  
**Email**: [mohsen.seyedkazemi@unibo.it](mailto:mohsen.seyedkazemi@unibo.it)  
**Institution**: University of Bologna

### **Contributing**
1. Fork the repository
2. Create a feature branch
3. Make your improvements
4. Submit a pull request

### **Issues & Feature Requests**
Please use the [GitHub Issues](https://github.com/MSKazemi/KubeIntellect/issues) for:
- Bug reports
- Feature requests
- Documentation improvements
- Questions and support

---

## 📄 **License**

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## 🎉 **What's New in This Version**

- ✨ **Fully automated deployment** with zero manual intervention
- 🔍 **Pre-deployment validation** with comprehensive checks
- 🤖 **Intelligent error recovery** and resource import
- 📊 **Enhanced monitoring** with optional Azure Monitor integration
- 🎨 **Beautiful CLI interface** with colors and progress indicators
- 🛡️ **Safe destruction** with multiple confirmation layers
- 📱 **Command-line options** for CI/CD integration
- 📚 **Comprehensive documentation** with troubleshooting guides
- 🔧 **Modular architecture** for easy customization
- 🚀 **Production-ready** configuration with best practices

