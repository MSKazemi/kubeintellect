#!/bin/bash

# 🔍 KubeIntellect Environment Validation Script
# This script validates that all prerequisites are met before deployment

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Validation results
VALIDATION_PASSED=true

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
    VALIDATION_PASSED=false
}

log_step() {
    echo -e "${PURPLE}🔄 $1${NC}"
}

# Function to check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Function to get version of a command
get_version() {
    local cmd="$1"
    local version_flag="${2:---version}"
    
    if command_exists "$cmd"; then
        # Read the first line of output to avoid SIGPIPE
        read -r first_line < <($cmd $version_flag 2>/dev/null)
        echo "$first_line" | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -n1
    else
        echo "not installed"
    fi
}

# Function to compare version strings
version_compare() {
    local version1="$1"
    local operator="$2"
    local version2="$3"
    
    if [[ "$version1" == "not installed" ]]; then
        return 1
    fi
    
    local result=$(echo -e "$version1\n$version2" | sort -V | head -n1)
    
    case "$operator" in
        ">=")
            [[ "$result" == "$version2" ]]
            ;;
        ">")
            [[ "$result" == "$version2" && "$version1" != "$version2" ]]
            ;;
        "=")
            [[ "$version1" == "$version2" ]]
            ;;
        *)
            false
            ;;
    esac
}

echo -e "${PURPLE}"
echo "🔍 KubeIntellect Environment Validation"
echo "======================================"
echo -e "${NC}"

# Check required tools
log_step "Checking required tools..."

# Check Terraform
terraform_version=$(get_version terraform)
if command_exists terraform; then
    if version_compare "$terraform_version" ">=" "1.3.0"; then
        log_success "Terraform $terraform_version is installed and meets requirements (>= 1.3.0)"
    else
        log_error "Terraform $terraform_version is installed but does not meet minimum version requirement (>= 1.3.0)"
    fi
else
    log_error "Terraform is not installed"
fi

# Check Azure CLI
az_version=$(get_version az)
if command_exists az; then
    if version_compare "$az_version" ">=" "2.40.0"; then
        log_success "Azure CLI $az_version is installed and meets requirements (>= 2.40.0)"
    else
        log_warning "Azure CLI $az_version is installed but may not meet optimal requirements (>= 2.40.0)"
    fi
else
    log_error "Azure CLI is not installed"
fi

# Check kubectl
if command_exists kubectl; then
    kubectl_version=$(kubectl version --client --short 2>/dev/null | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -n1 || echo "unknown")
    log_success "kubectl $kubectl_version is installed"
else
    log_warning "kubectl is not installed (will be installed during deployment if needed)"
fi

# Check Git
git_version=$(get_version git)
if command_exists git; then
    log_success "Git $git_version is installed"
else
    log_warning "Git is not installed (recommended for version control)"
fi

# Check curl
if command_exists curl; then
    log_success "curl is installed"
else
    log_error "curl is not installed (required for various operations)"
fi

# Check Azure authentication
log_step "Checking Azure authentication..."

if command_exists az; then
    if az account show >/dev/null 2>&1; then
        current_subscription=$(az account show --query id -o tsv 2>/dev/null)
        current_tenant=$(az account show --query tenantId -o tsv 2>/dev/null)
        current_user=$(az account show --query user.name -o tsv 2>/dev/null)
        
        log_success "Logged into Azure as: $current_user"
        log_info "Current Subscription: $current_subscription"
        log_info "Current Tenant: $current_tenant"
        
        # Check Azure permissions
        log_step "Checking Azure permissions..."
        
        if az provider show --namespace Microsoft.ContainerService >/dev/null 2>&1; then
            log_success "Microsoft.ContainerService provider is accessible"
        else
            log_error "Cannot access Microsoft.ContainerService provider (required for AKS)"
        fi
        
        if az provider show --namespace Microsoft.Network >/dev/null 2>&1; then
            log_success "Microsoft.Network provider is accessible"
        else
            log_error "Cannot access Microsoft.Network provider (required for networking)"
        fi
        
    else
        log_warning "Not logged into Azure (will prompt during deployment)"
    fi
else
    log_error "Azure CLI not available for authentication check"
fi

# Check system resources
log_step "Checking system resources..."

# Check disk space
available_space=$(df . | tail -1 | awk '{print $4}')
if [[ $available_space -gt 1048576 ]]; then # 1GB in KB
    log_success "Sufficient disk space available"
else
    log_warning "Low disk space detected (< 1GB available)"
fi

# Check network connectivity
log_step "Checking network connectivity..."

if curl -s --connect-timeout 5 https://management.azure.com >/dev/null; then
    log_success "Azure API endpoints are reachable"
else
    log_error "Cannot reach Azure API endpoints"
fi

if curl -s --connect-timeout 5 https://registry-1.docker.io >/dev/null; then
    log_success "Docker Hub is reachable"
else
    log_warning "Cannot reach Docker Hub (may affect container image pulls)"
fi

# Check Terraform configuration
log_step "Checking Terraform configuration..."

if [[ -f "main.tf" ]]; then
    log_success "main.tf found"

    log_step "Initializing Terraform..."
    if terraform init >/dev/null 2>&1; then
        log_success "Terraform initialized successfully"
    else
        log_error "Terraform initialization failed"
    fi
    
    if terraform validate; then
        log_success "Terraform configuration is valid"
    else
        log_error "Terraform configuration validation failed"
        log_info "Run 'terraform validate' for detailed error information"
    fi
else
    log_error "main.tf not found in current directory"
fi

if [[ -f "variables.tf" ]]; then
    log_success "variables.tf found"
else
    log_error "variables.tf not found"
fi

if [[ -f "terraform.tfvars.example" ]]; then
    log_success "terraform.tfvars.example found"
    
    if [[ ! -f "terraform.tfvars" ]]; then
        log_warning "terraform.tfvars not found (will be created during deployment)"
    else
        log_success "terraform.tfvars found"
    fi
else
    log_warning "terraform.tfvars.example not found"
fi

# Check modules
log_step "Checking Terraform modules..."

if [[ -d "modules/ingress-nginx" ]]; then
    log_success "ingress-nginx module found"
else
    log_error "ingress-nginx module not found"
fi

if [[ -d "modules/kube-prometheus" ]]; then
    log_success "kube-prometheus module found"
else
    log_error "kube-prometheus module not found"
fi

if [[ -d "manifests" ]]; then
    log_success "manifests directory found"
    
    if [[ -f "manifests/grafana-ingress.yaml" ]]; then
        log_success "grafana-ingress.yaml found"
    else
        log_error "grafana-ingress.yaml not found"
    fi
    
    if [[ -f "manifests/prometheus-ingress.yaml" ]]; then
        log_success "prometheus-ingress.yaml found"
    else
        log_error "prometheus-ingress.yaml not found"
    fi
else
    log_error "manifests directory not found"
fi

# Final validation result
echo ""
echo -e "${CYAN}=== Validation Summary ===${NC}"

if [[ "$VALIDATION_PASSED" == true ]]; then
    log_success "🎉 Environment validation passed! You can proceed with deployment."
    echo ""
    echo -e "${GREEN}Next steps:${NC}"
    # echo "1. Run: chmod +x deploy-automated.sh"
    # echo "2. Run: ./deploy-automated.sh"
    echo "make azure-cluster-create"
    echo "make azure-kubeintellect-deploy"
    exit 0
else
    log_error "❌ Environment validation failed. Please address the issues above before proceeding."
    echo ""
    echo -e "${YELLOW}Common solutions:${NC}"
    echo "• Install missing tools using setup_aks_env.sh"
    echo "• Login to Azure using: az login"
    echo "• Check network connectivity and firewall settings"
    echo "• Verify Azure subscription permissions"
    echo ""
    exit 1
fi 