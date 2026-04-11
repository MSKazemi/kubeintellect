#!/bin/bash

# 🗑️ KubeIntellect Infrastructure Destruction Script
# This script safely destroys all KubeIntellect infrastructure

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Global variables
FORCE_DESTROY=false
PRESERVE_STATE=false
RESOURCE_GROUP=""
CLUSTER_NAME=""
DESTROY_START_TIME=""

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

# Function to prompt for confirmation
confirm_action() {
    local prompt="$1"
    local default="${2:-n}"
    
    if [[ "$FORCE_DESTROY" == true ]]; then
        log_warning "Force mode enabled - skipping confirmation"
        return 0
    fi
    
    echo -ne "${YELLOW}$prompt${NC}"
    if [[ "$default" == "y" ]]; then
        echo -ne " (Y/n): "
    else
        echo -ne " (y/N): "
    fi
    
    read -r response
    case "${response:-$default}" in
        [yY]|[yY][eE][sS])
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

# Function to check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Function to read terraform variables
read_terraform_vars() {
    if [[ -f "terraform.tfvars" ]]; then
        RESOURCE_GROUP=$(grep "resource_group_name" terraform.tfvars | cut -d'"' -f2 2>/dev/null || echo "")
        CLUSTER_NAME=$(grep "cluster_name" terraform.tfvars | cut -d'"' -f2 2>/dev/null || echo "")
    fi
    
    # Fallback to defaults if not found
    RESOURCE_GROUP=${RESOURCE_GROUP:-"rg-kubeintellect"}
    CLUSTER_NAME=${CLUSTER_NAME:-"aks-kubeintellect"}
}

# Function to backup current state
backup_state() {
    if [[ "$PRESERVE_STATE" == true ]]; then
        log_step "Creating state backup..."
        
        local backup_dir="state-backup-$(date +%Y%m%d-%H%M%S)"
        mkdir -p "$backup_dir"
        
        if [[ -f "terraform.tfstate" ]]; then
            cp terraform.tfstate "$backup_dir/"
            log_success "Terraform state backed up to $backup_dir/"
        fi
        
        if [[ -f "terraform.tfstate.backup" ]]; then
            cp terraform.tfstate.backup "$backup_dir/"
        fi
        
        if [[ -f "terraform.tfvars" ]]; then
            cp terraform.tfvars "$backup_dir/"
            log_success "Configuration backed up to $backup_dir/"
        fi
        
        log_info "Backup created in: $backup_dir"
    fi
}

# Function to get cluster information
get_cluster_info() {
    log_step "Gathering cluster information..."
    
    if command_exists kubectl && kubectl cluster-info >/dev/null 2>&1; then
        log_info "Current kubectl context: $(kubectl config current-context 2>/dev/null || echo 'none')"
        
        local nodes=$(kubectl get nodes --no-headers 2>/dev/null | wc -l || echo "0")
        log_info "Cluster has $nodes node(s)"
        
        local namespaces=$(kubectl get namespaces --no-headers 2>/dev/null | wc -l || echo "0")
        log_info "Cluster has $namespaces namespace(s)"
    else
        log_warning "kubectl not configured or cluster not accessible"
    fi
}

# Function to show resources to be destroyed
show_resources() {
    log_step "Resources that will be destroyed:"
    
    if command_exists terraform && [[ -f "terraform.tfstate" ]]; then
        echo ""
        terraform state list 2>/dev/null | while read -r resource; do
            echo -e "${RED}  - $resource${NC}"
        done
        echo ""
    else
        log_warning "Cannot list Terraform resources (no state file or terraform not available)"
        echo ""
        echo -e "${RED}Expected resources:${NC}"
        echo -e "${RED}  - Resource Group: $RESOURCE_GROUP${NC}"
        echo -e "${RED}  - AKS Cluster: $CLUSTER_NAME${NC}"
        echo -e "${RED}  - All associated networking and storage resources${NC}"
        echo ""
    fi
}

# Function to perform pre-destruction checks
pre_destruction_checks() {
    log_step "Performing pre-destruction checks..."
    
    # Check if resource group exists
    if command_exists az && az account show >/dev/null 2>&1; then
        if az group show --name "$RESOURCE_GROUP" >/dev/null 2>&1; then
            log_warning "Resource group '$RESOURCE_GROUP' exists in Azure"
        else
            log_info "Resource group '$RESOURCE_GROUP' not found in Azure"
        fi
        
        if az aks show --name "$CLUSTER_NAME" --resource-group "$RESOURCE_GROUP" >/dev/null 2>&1; then
            log_warning "AKS cluster '$CLUSTER_NAME' exists in Azure"
        else
            log_info "AKS cluster '$CLUSTER_NAME' not found in Azure"
        fi
    else
        log_warning "Cannot check Azure resources (not logged in or az CLI not available)"
    fi
    
    # Check for running workloads
    if command_exists kubectl && kubectl cluster-info >/dev/null 2>&1; then
        log_step "Checking for running workloads..."
        
        local running_pods=$(kubectl get pods --all-namespaces --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l || echo "0")
        if [[ $running_pods -gt 0 ]]; then
            log_warning "$running_pods running pods will be terminated"
        fi
        
        local persistent_volumes=$(kubectl get pv --no-headers 2>/dev/null | wc -l || echo "0")
        if [[ $persistent_volumes -gt 0 ]]; then
            log_warning "$persistent_volumes persistent volumes will be deleted"
        fi
        
        local load_balancers=$(kubectl get svc --all-namespaces --field-selector=spec.type=LoadBalancer --no-headers 2>/dev/null | wc -l || echo "0")
        if [[ $load_balancers -gt 0 ]]; then
            log_warning "$load_balancers load balancer services will be deleted"
        fi
    fi
}

# Function to pre-delete resources created outside Terraform (ESO setup script)
# that would cause `terraform destroy` to fail with the RG-contains-resources error.
# Covers: Key Vault (kubeintellect-kv), ESO Managed Identity (kubeintellect-eso-mi),
# and the ContainerInsights solution auto-created by AKS monitoring.
delete_unmanaged_resources() {
    if ! command_exists az || ! az account show >/dev/null 2>&1; then
        log_warning "Azure CLI not available — skipping unmanaged resource pre-cleanup"
        return 0
    fi

    if ! az group show --name "$RESOURCE_GROUP" >/dev/null 2>&1; then
        log_info "Resource group '$RESOURCE_GROUP' not found — nothing to pre-clean"
        return 0
    fi

    log_step "Pre-deleting unmanaged resources in '$RESOURCE_GROUP'..."

    # Key Vault — purge on delete so the name is immediately reusable
    local kv_name="${CLUSTER_NAME/aks-/kubeintellect}-kv"
    kv_name="${kv_name:-kubeintellect-kv}"
    if az keyvault show --name "$kv_name" --resource-group "$RESOURCE_GROUP" >/dev/null 2>&1; then
        log_step "Deleting Key Vault '$kv_name' (with purge)..."
        az keyvault delete --name "$kv_name" --resource-group "$RESOURCE_GROUP" || true
        az keyvault purge --name "$kv_name" --location "$(az group show --name "$RESOURCE_GROUP" --query location -o tsv 2>/dev/null || echo westeurope)" --no-wait || true
        log_success "Key Vault '$kv_name' deleted and purge initiated"
    fi

    # ESO Managed Identity
    local mi_name="kubeintellect-eso-mi"
    if az identity show --name "$mi_name" --resource-group "$RESOURCE_GROUP" >/dev/null 2>&1; then
        log_step "Deleting Managed Identity '$mi_name'..."
        az identity delete --name "$mi_name" --resource-group "$RESOURCE_GROUP" || true
        log_success "Managed Identity '$mi_name' deleted"
    fi

    # ContainerInsights solution (auto-created by OMS agent — not in Terraform state)
    local log_ws="${CLUSTER_NAME}-logs"
    local ci_name="ContainerInsights($log_ws)"
    if az monitor log-analytics solution show --name "$ci_name" --resource-group "$RESOURCE_GROUP" >/dev/null 2>&1; then
        log_step "Deleting ContainerInsights solution..."
        az monitor log-analytics solution delete --name "$ci_name" --resource-group "$RESOURCE_GROUP" --yes || true
        log_success "ContainerInsights solution deleted"
    fi
}

# Function to verify destruction completed
verify_destruction() {
    log_step "Verifying destruction..."
    if command_exists az && az account show >/dev/null 2>&1; then
        if az group show --name "$RESOURCE_GROUP" >/dev/null 2>&1; then
            log_warning "Resource group '$RESOURCE_GROUP' still exists (deletion may be in progress asynchronously)"
        else
            log_success "Resource group '$RESOURCE_GROUP' confirmed deleted"
        fi
    fi
}

# Function to destroy infrastructure using Terraform
destroy_with_terraform() {
    log_step "Destroying infrastructure with Terraform..."
    
    if ! command_exists terraform; then
        log_error "Terraform not available"
        return 1
    fi
    
    if [[ ! -f "main.tf" ]]; then
        log_error "Terraform configuration not found"
        return 1
    fi
    
    # Initialize if needed
    if [[ ! -d ".terraform" ]]; then
        log_step "Initializing Terraform..."
        terraform init
    fi
    
    # Show destruction plan
    if ! confirm_action "Show destruction plan before proceeding?"; then
        log_step "Skipping plan display"
    else
        log_step "Generating destruction plan..."
        if terraform plan -destroy -var-file="terraform.tfvars" 2>/dev/null; then
            echo ""
            if ! confirm_action "Proceed with destruction based on the plan above?"; then
                log_info "Destruction cancelled by user"
                exit 0
            fi
        else
            log_warning "Could not generate destruction plan, proceeding anyway..."
        fi
    fi
    
    # Perform destruction
    log_step "Executing terraform destroy..."
    
    if terraform destroy -var-file="terraform.tfvars" -auto-approve; then
        log_success "Terraform destruction completed successfully"
        return 0
    else
        log_error "Terraform destruction failed"
        return 1
    fi
}

# Function to force cleanup using Azure CLI
force_cleanup_azure() {
    log_step "Performing force cleanup using Azure CLI..."
    
    if ! command_exists az; then
        log_error "Azure CLI not available for force cleanup"
        return 1
    fi
    
    if ! az account show >/dev/null 2>&1; then
        log_error "Not logged into Azure"
        return 1
    fi
    
    # Delete AKS cluster first
    if az aks show --name "$CLUSTER_NAME" --resource-group "$RESOURCE_GROUP" >/dev/null 2>&1; then
        log_step "Deleting AKS cluster '$CLUSTER_NAME'..."
        if az aks delete --name "$CLUSTER_NAME" --resource-group "$RESOURCE_GROUP" --yes --no-wait; then
            log_success "AKS cluster deletion initiated"
        else
            log_error "Failed to delete AKS cluster"
        fi
    else
        log_info "AKS cluster '$CLUSTER_NAME' not found"
    fi
    
    # Delete resource group
    if az group show --name "$RESOURCE_GROUP" >/dev/null 2>&1; then
        log_step "Deleting resource group '$RESOURCE_GROUP'..."
        if confirm_action "This will delete ALL resources in the resource group. Continue?" "n"; then
            if az group delete --name "$RESOURCE_GROUP" --yes --no-wait; then
                log_success "Resource group deletion initiated"
            else
                log_error "Failed to delete resource group"
            fi
        else
            log_info "Resource group deletion cancelled"
        fi
    else
        log_info "Resource group '$RESOURCE_GROUP' not found"
    fi
}

# Function to cleanup local files
cleanup_local_files() {
    log_step "Cleaning up local files..."
    
    local files_to_clean=()
    
    if [[ -f ".terraform.lock.hcl" ]]; then
        files_to_clean+=(".terraform.lock.hcl")
    fi
    
    if [[ -d ".terraform" ]]; then
        files_to_clean+=(".terraform/")
    fi
    
    if [[ "$PRESERVE_STATE" != true ]]; then
        if [[ -f "terraform.tfstate" ]]; then
            files_to_clean+=("terraform.tfstate")
        fi
        
        if [[ -f "terraform.tfstate.backup" ]]; then
            files_to_clean+=("terraform.tfstate.backup")
        fi
    fi
    
    if [[ ${#files_to_clean[@]} -gt 0 ]]; then
        echo "Files to be cleaned:"
        printf '  - %s\n' "${files_to_clean[@]}"
        echo ""
        
        if confirm_action "Clean up these local files?"; then
            for file in "${files_to_clean[@]}"; do
                if rm -rf "$file" 2>/dev/null; then
                    log_success "Removed $file"
                else
                    log_warning "Could not remove $file"
                fi
            done
        else
            log_info "Local cleanup skipped"
        fi
    else
        log_info "No local files to clean up"
    fi
}

# Function to cleanup kubectl context
cleanup_kubectl_context() {
    if command_exists kubectl; then
        local current_context=$(kubectl config current-context 2>/dev/null || echo "")
        
        if [[ -n "$current_context" && "$current_context" == *"$CLUSTER_NAME"* ]]; then
            if confirm_action "Remove kubectl context '$current_context'?"; then
                if kubectl config delete-context "$current_context" 2>/dev/null; then
                    log_success "Kubectl context removed"
                else
                    log_warning "Could not remove kubectl context"
                fi
            fi
        fi
    fi
}

# Display help
show_help() {
    echo "KubeIntellect Infrastructure Destruction Script"
    echo ""
    echo "Usage: $0 [options]"
    echo ""
    echo "Options:"
    echo "  -h, --help          Show this help message"
    echo "  -f, --force         Skip all confirmation prompts"
    echo "  -p, --preserve      Preserve Terraform state files"
    echo "  --terraform-only    Only use Terraform for destruction"
    echo "  --azure-only        Only use Azure CLI for destruction"
    echo ""
    echo "Examples:"
    echo "  $0                  Interactive destruction"
    echo "  $0 --force          Destroy everything without prompts"
    echo "  $0 --preserve       Destroy but keep state files"
}

# Main destruction function
main() {
    echo -e "${PURPLE}"
    echo "🗑️ KubeIntellect Infrastructure Destruction"
    echo "==========================================="
    echo -e "${NC}"
    
    read_terraform_vars
    
    log_warning "This will PERMANENTLY DESTROY all KubeIntellect infrastructure!"
    log_info "Target Resource Group: $RESOURCE_GROUP"
    log_info "Target AKS Cluster: $CLUSTER_NAME"
    echo ""
    
    if [[ "$FORCE_DESTROY" != true ]]; then
        if ! confirm_action "Are you absolutely sure you want to proceed?" "n"; then
            log_info "Destruction cancelled by user"
            exit 0
        fi
    fi
    
    # Create backup if requested
    backup_state
    
    # Get cluster information
    get_cluster_info
    
    # Show resources to be destroyed
    show_resources
    
    # Perform pre-destruction checks
    pre_destruction_checks
    
    # Final confirmation
    if [[ "$FORCE_DESTROY" != true ]]; then
        if ! confirm_action "Last chance! Proceed with destruction?" "n"; then
            log_info "Destruction cancelled by user"
            exit 0
        fi
    fi
    
    # Record start time for elapsed reporting
    DESTROY_START_TIME=$(date +%s)

    # Pre-delete resources not tracked by Terraform to prevent RG-contains-resources errors
    delete_unmanaged_resources

    # Attempt destruction with Terraform first
    log_step "Starting infrastructure destruction..."

    if destroy_with_terraform; then
        log_success "Infrastructure destroyed successfully with Terraform"
    else
        log_warning "Terraform destruction failed or incomplete"

        if confirm_action "Attempt force cleanup using Azure CLI?"; then
            force_cleanup_azure
        fi
    fi

    # Verify the resource group is gone
    verify_destruction

    # Cleanup local files
    cleanup_local_files

    # Cleanup kubectl context
    cleanup_kubectl_context

    local elapsed=$(( $(date +%s) - DESTROY_START_TIME ))
    local elapsed_min=$(( elapsed / 60 ))
    local elapsed_sec=$(( elapsed % 60 ))

    echo ""
    log_success "🎉 Destruction process completed in ${elapsed_min}m ${elapsed_sec}s!"
    echo ""
    log_info "What was destroyed:"
    echo "  • AKS cluster and all nodes"
    echo "  • All Kubernetes workloads and data"
    echo "  • Load balancers and networking resources"
    echo "  • Monitoring and logging resources"
    if [[ "$PRESERVE_STATE" != true ]]; then
        echo "  • Local Terraform state files"
    fi
    echo ""
    log_warning "This action cannot be undone!"
}

# Parse command line arguments
TERRAFORM_ONLY=false
AZURE_ONLY=false

while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            show_help
            exit 0
            ;;
        -f|--force)
            FORCE_DESTROY=true
            shift
            ;;
        -p|--preserve)
            PRESERVE_STATE=true
            shift
            ;;
        --terraform-only)
            TERRAFORM_ONLY=true
            shift
            ;;
        --azure-only)
            AZURE_ONLY=true
            shift
            ;;
        *)
            log_error "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Validate conflicting options
if [[ "$TERRAFORM_ONLY" == true && "$AZURE_ONLY" == true ]]; then
    log_error "Cannot specify both --terraform-only and --azure-only"
    exit 1
fi

# Run main function
main "$@" 