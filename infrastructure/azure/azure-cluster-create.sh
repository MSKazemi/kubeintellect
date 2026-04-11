#!/bin/bash

# 🚀 KubeIntellect Automated Deployment Script
# This script provides fully automated deployment with interactive prompts
# and intelligent error handling

set -euo pipefail

# # Always run relative to this script's directory
# SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# cd "$SCRIPT_DIR"


# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Global variables
RESOURCE_GROUP=""
CLUSTER_NAME=""
LOCATION=""
SUBSCRIPTION_ID=""
TENANT_ID=""
NODE_COUNT=1
NODE_VM_SIZE="Standard_DS3_v2"
DNS_PREFIX=""
ENVIRONMENT="production"

# Logging functions
log_info() {
    echo -e "${BLUE}ℹ️  $1${NC}"
}

log_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

log_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

log_error() {
    echo -e "${RED}❌ $1${NC}"
}

log_step() {
    echo -e "${PURPLE}🔄 $1${NC}"
}

# Function to prompt for user input with validation
prompt_with_default() {
    local prompt="$1"
    local default="$2"
    local var_name="$3"
    local validation_func="${4:-}"
    
    while true; do
        echo -ne "${CYAN}$prompt${NC}"
        if [[ -n "$default" ]]; then
            echo -ne " (default: $default): "
        else
            echo -ne ": "
        fi
        
        read -r input
        if [[ -z "$input" && -n "$default" ]]; then
            input="$default"
        fi
        
        if [[ -n "$validation_func" ]]; then
            if ! $validation_func "$input"; then
                log_error "Invalid input. Please try again."
                continue
            fi
        fi
        
        eval "$var_name='$input'"
        break
    done
}

# Validation functions
validate_subscription_id() {
    [[ "$1" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]
}

validate_tenant_id() {
    [[ "$1" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]
}

validate_node_count() {
    [[ "$1" =~ ^[1-9][0-9]*$ ]] && [[ "$1" -le 100 ]]
}

validate_not_empty() {
    [[ -n "$1" ]]
}

# Function to check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Function to check prerequisites
check_prerequisites() {
    log_step "Checking prerequisites..."
    
    local missing_tools=()
    
    if ! command_exists az; then
        missing_tools+=("azure-cli")
    fi
    
    if ! command_exists terraform; then
        missing_tools+=("terraform")
    fi
    
    if ! command_exists kubectl; then
        missing_tools+=("kubectl")
    fi
    
    if [[ ${#missing_tools[@]} -gt 0 ]]; then
        log_error "Missing required tools: ${missing_tools[*]}"
        log_info "Please install missing tools and run the script again."
        log_info "You can use setup_aks_env.sh to install missing dependencies."
        exit 1
    fi
    
    log_success "All prerequisites are installed"
}

# Function to check Azure login status
check_azure_login() {
    log_step "Checking Azure login status..."
    
    if ! az account show >/dev/null 2>&1; then
        log_warning "Not logged into Azure. Please login..."
        
        echo -e "${CYAN}Choose login method:${NC}"
        echo "1) Interactive login (default)"
        echo "2) Device code login"
        echo -ne "${CYAN}Enter choice (1-2): ${NC}"
        read -r login_choice
        
        case "${login_choice:-1}" in
            1)
                if [[ -n "$TENANT_ID" ]]; then
                    az login --tenant "$TENANT_ID"
                else
                    az login
                fi
                ;;
            2)
                if [[ -n "$TENANT_ID" ]]; then
                    az login --use-device-code --tenant "$TENANT_ID"
                else
                    az login --use-device-code
                fi
                ;;
            *)
                log_error "Invalid choice"
                exit 1
                ;;
        esac
    fi
    
    # Set subscription if provided
    if [[ -n "$SUBSCRIPTION_ID" ]]; then
        log_step "Setting subscription to $SUBSCRIPTION_ID..."
        az account set --subscription "$SUBSCRIPTION_ID"
    fi
    
    log_success "Azure authentication verified"
}

# Function to collect configuration interactively
collect_configuration() {
    log_step "Collecting deployment configuration..."
    
    echo -e "${CYAN}=== Azure Configuration ===${NC}"
    
    # Get current subscription if logged in
    if az account show >/dev/null 2>&1; then
        current_sub=$(az account show --query id -o tsv 2>/dev/null || echo "")
        current_tenant=$(az account show --query tenantId -o tsv 2>/dev/null || echo "")
    else
        current_sub=""
        current_tenant=""
    fi
    
    prompt_with_default "Azure Subscription ID" "$current_sub" "SUBSCRIPTION_ID" validate_subscription_id
    prompt_with_default "Azure Tenant ID" "$current_tenant" "TENANT_ID" validate_tenant_id
    
    echo -e "${CYAN}=== Deployment Configuration ===${NC}"
    prompt_with_default "Resource Group Name" "rg-kubeintellect" "RESOURCE_GROUP" validate_not_empty
    prompt_with_default "AKS Cluster Name" "aks-kubeintellect" "CLUSTER_NAME" validate_not_empty
    prompt_with_default "DNS Prefix" "kubeintellect" "DNS_PREFIX" validate_not_empty
    prompt_with_default "Azure Location" "westeurope" "LOCATION" validate_not_empty
    prompt_with_default "Number of Nodes" "1" "NODE_COUNT" validate_node_count
    prompt_with_default "VM Size for Nodes" "Standard_DS3_v2" "NODE_VM_SIZE" validate_not_empty
    prompt_with_default "Environment Tag" "production" "ENVIRONMENT" validate_not_empty
    
    log_success "Configuration collected successfully"
}

# Function to update terraform.tfvars with collected values
update_terraform_vars() {
    log_step "Updating terraform.tfvars with configuration..."
    
    cat > terraform.tfvars <<EOF
# Auto-generated configuration
resource_group_name = "$RESOURCE_GROUP"
cluster_name        = "$CLUSTER_NAME"
dns_prefix          = "$DNS_PREFIX"
location            = "$LOCATION"
node_count          = $NODE_COUNT
node_vm_size        = "$NODE_VM_SIZE"
environment         = "$ENVIRONMENT"
subscription_id     = "$SUBSCRIPTION_ID"
tenant_id           = "$TENANT_ID"
EOF
    
    log_success "terraform.tfvars updated"
}

# Function to check if resource exists in Azure
resource_exists() {
    local resource_type="$1"
    local resource_name="$2"
    local resource_group="$3"
    
    case "$resource_type" in
        "group")
            az group show --name "$resource_name" >/dev/null 2>&1
            ;;
        "aks")
            az aks show --name "$resource_name" --resource-group "$resource_group" >/dev/null 2>&1
            ;;
        *)
            false
            ;;
    esac
}

# Function to import existing resources into Terraform state
import_existing_resources() {
    log_step "Checking for existing resources..."
    
    # Check if resource group exists
    if resource_exists "group" "$RESOURCE_GROUP" ""; then
        log_warning "Resource group '$RESOURCE_GROUP' already exists"
        
        # Check if it's in Terraform state
        if ! terraform state show azurerm_resource_group.kubeintellect >/dev/null 2>&1; then
            log_step "Importing existing resource group into Terraform state..."
            terraform import azurerm_resource_group.kubeintellect "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP"
            log_success "Resource group imported successfully"
        else
            log_info "Resource group already in Terraform state"
        fi
    fi
    
    # Check if AKS cluster exists
    if resource_exists "aks" "$CLUSTER_NAME" "$RESOURCE_GROUP"; then
        log_warning "AKS cluster '$CLUSTER_NAME' already exists"
        
        # Check if it's in Terraform state
        if ! terraform state show azurerm_kubernetes_cluster.aks >/dev/null 2>&1; then
            log_step "Importing existing AKS cluster into Terraform state..."
            terraform import azurerm_kubernetes_cluster.aks "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.ContainerService/managedClusters/$CLUSTER_NAME"
            log_success "AKS cluster imported successfully"
        else
            log_info "AKS cluster already in Terraform state"
        fi
    fi
}

# Function to wait for AKS cluster readiness
wait_for_cluster_ready() {
    log_step "Waiting for AKS cluster to be ready..."
    
    local max_attempts=30
    local attempt=1
    
    while [[ $attempt -le $max_attempts ]]; do
        # Check if we can get nodes - this is a more reliable indicator of cluster readiness
        # than kubectl version which can fail even when cluster is working
        if kubectl get nodes --no-headers 2>/dev/null | grep -q "Ready"; then
            log_success "AKS cluster is ready and responding"
            return 0
        fi
        
        log_info "Waiting for cluster... (attempt $attempt/$max_attempts)"
        sleep 10
        ((attempt++))
    done
    
    # Final check - if we can get nodes at all, consider it ready
    if kubectl get nodes >/dev/null 2>&1; then
        log_success "AKS cluster is ready and responding"
        return 0
    fi
    
    log_error "Cluster failed to become ready after $max_attempts attempts"
    return 1
}

# Function to configure kubectl
configure_kubectl() {
    log_step "Configuring kubectl for AKS cluster..."
    
    az aks get-credentials \
        --resource-group "$RESOURCE_GROUP" \
        --name "$CLUSTER_NAME" \
        --overwrite-existing
    
    # Set the current context to the AKS cluster
    local context_name
    context_name=$(kubectl config get-contexts -o name | grep -i "$CLUSTER_NAME" | head -n 1)
    if [[ -n "$context_name" ]]; then
        kubectl config use-context "$context_name"
        log_info "Set kubectl context to: $context_name"
    fi
    
    # Test kubectl connectivity - wait a bit for cluster to be ready
    sleep 5
    if kubectl get nodes >/dev/null 2>&1; then
        log_success "kubectl configured successfully"
    else
        log_warning "kubectl configuration completed, but cluster not immediately accessible"
        # Don't fail here - wait_for_cluster_ready will be called later
    fi
}

# Function to deploy infrastructure
deploy_infrastructure() {
    log_step "Starting infrastructure deployment..."
    
    # Clean previous state if requested
    echo -e "${CYAN}Do you want to clean previous Terraform state? (y/N): ${NC}"
    read -r clean_state
    if [[ "${clean_state,,}" == "y" ]]; then
        log_warning "Cleaning previous Terraform state..."
        rm -rf .terraform terraform.tfstate* .terraform.lock.hcl
    fi
    
    # Initialize Terraform
    log_step "Initializing Terraform..."
    terraform init
    
    # Import existing resources
    import_existing_resources
    
    # Configure kubectl if cluster already exists (needed for terraform plan/apply)
    if resource_exists "aks" "$CLUSTER_NAME" "$RESOURCE_GROUP"; then
        log_step "Configuring kubectl for existing cluster (needed for Terraform)..."
        configure_kubectl
    fi
    
    # Deploy all resources
    log_step "Deploying all infrastructure..."
    
    if ! terraform apply -var-file="terraform.tfvars" -auto-approve; then
        log_error "Deployment failed"
        return 1
    fi
    
    # Ensure kubectl is configured after apply (in case cluster was just created)
    if ! kubectl get nodes >/dev/null 2>&1; then
        log_step "Configuring kubectl after cluster creation..."
        configure_kubectl
    fi
    
    # Wait for cluster to be fully ready
    wait_for_cluster_ready
    
    log_success "Infrastructure deployment completed successfully"
}

# Function to display deployment summary
display_summary() {
    log_success "🎉 KubeIntellect Infrastructure Deployment Complete!"
    
    echo -e "${CYAN}=== Deployment Summary ===${NC}"
    echo "Resource Group: $RESOURCE_GROUP"
    echo "AKS Cluster: $CLUSTER_NAME"
    echo "Location: $LOCATION"
    echo "Node Count: $NODE_COUNT"
    echo "VM Size: $NODE_VM_SIZE"
    
    # Get ingress IP
    log_step "Retrieving ingress controller IP..."
    local ingress_ip
    ingress_ip=$(kubectl get svc -n ingress-nginx ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || echo "pending")
    
    echo -e "${CYAN}=== Access Information ===${NC}"
    echo "Ingress IP: $ingress_ip"
    
    if [[ "$ingress_ip" != "pending" && "$ingress_ip" != "" ]]; then
        echo ""
        echo -e "${GREEN}To access your services, add these entries to /etc/hosts:${NC}"
        echo "$ingress_ip grafana.kubeintellect.local"
        echo "$ingress_ip prometheus.kubeintellect.local"
        echo ""
        echo -e "${GREEN}Access URLs:${NC}"
        echo "Grafana: http://grafana.kubeintellect.local"
        echo "Prometheus: http://prometheus.kubeintellect.local"
        echo ""
        echo -e "${GREEN}Grafana Credentials:${NC}"
        echo "Username: admin"
        echo -n "Password: "
        kubectl get secret -n monitoring kube-prom-stack-grafana -o jsonpath="{.data.admin-password}" 2>/dev/null | base64 -d 2>/dev/null && echo
    else
        log_warning "Ingress IP not yet assigned. Please check later with:"
        echo "kubectl get svc -n ingress-nginx"
    fi
    
    echo ""
    echo -e "${GREEN}Useful Commands:${NC}"
    echo "kubectl get nodes                    # View cluster nodes"
    echo "kubectl get pods -A                  # View all pods"
    echo "kubectl get ingress -A               # View ingress resources"
    echo "kubectl get svc -A                   # View all services"
}

# Function to handle cleanup on script exit
cleanup() {
    local exit_code=$?
    if [[ $exit_code -ne 0 ]]; then
        log_error "Deployment failed with exit code $exit_code"
        echo ""
        echo -e "${CYAN}Troubleshooting Tips:${NC}"
        echo "1. Check Azure permissions and quota limits"
        echo "2. Verify network connectivity"
        echo "3. Review Terraform logs for detailed errors"
        echo "4. Run 'terraform plan' to validate configuration"
        echo "5. Check 'kubectl get events -A' for cluster events"
    fi
}

# Main function
main() {
    echo -e "${PURPLE}"
    echo "🚀 KubeIntellect Automated Deployment Script"
    echo "============================================="
    echo -e "${NC}"
    
    # Set up cleanup handler
    trap cleanup EXIT
    
    # Check prerequisites
    check_prerequisites
    
    # Collect configuration
    collect_configuration
    
    # Update terraform variables
    update_terraform_vars
    
    # Check Azure login
    check_azure_login
    
    # Deploy infrastructure
    deploy_infrastructure
    
    # Display summary
    display_summary
    
    log_success "Deployment completed successfully! 🎉"
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --help|-h)
            echo "KubeIntellect Automated Deployment Script"
            echo ""
            echo "Usage: $0 [options]"
            echo ""
            echo "Options:"
            echo "  -h, --help              Show this help message"
            echo "  --subscription-id ID    Azure subscription ID"
            echo "  --tenant-id ID          Azure tenant ID"
            echo "  --resource-group NAME   Resource group name"
            echo "  --cluster-name NAME     AKS cluster name"
            echo "  --location LOCATION     Azure location"
            echo "  --node-count COUNT      Number of nodes"
            echo "  --vm-size SIZE          VM size for nodes"
            echo ""
            echo "If options are not provided, the script will prompt interactively."
            exit 0
            ;;
        --subscription-id)
            SUBSCRIPTION_ID="$2"
            shift 2
            ;;
        --tenant-id)
            TENANT_ID="$2"
            shift 2
            ;;
        --resource-group)
            RESOURCE_GROUP="$2"
            shift 2
            ;;
        --cluster-name)
            CLUSTER_NAME="$2"
            shift 2
            ;;
        --location)
            LOCATION="$2"
            shift 2
            ;;
        --node-count)
            NODE_COUNT="$2"
            shift 2
            ;;
        --vm-size)
            NODE_VM_SIZE="$2"
            shift 2
            ;;
        *)
            log_error "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Run main function
main "$@" 



# helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
# helm repo update

# helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
#   -n ingress-nginx --create-namespace \
#   --set controller.service.type=LoadBalancer \
#   --set controller.service.annotations."service\.beta\.kubernetes\.io/azure-load-balancer-health-probe-request-path"=/healthz
