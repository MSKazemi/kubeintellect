#!/bin/bash

set -e

echo "📦 Installing dependencies..."

# Install Terraform if not installed
if ! command -v terraform &> /dev/null; then
  echo "🔧 Installing Terraform..."
  sudo apt-get update && sudo apt-get install -y gnupg software-properties-common curl
  curl -fsSL https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
  echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/hashicorp.list
  sudo apt-get update && sudo apt-get install -y terraform
else
  echo "✅ Terraform already installed."
fi

# Install Azure CLI if not installed
if ! command -v az &> /dev/null; then
  echo "🔧 Installing Azure CLI..."
  curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
else
  echo "✅ Azure CLI already installed."
fi

# Install kubectl if not installed
if ! command -v kubectl &> /dev/null; then
  echo "🔧 Installing kubectl..."
  az aks install-cli
else
  echo "✅ kubectl already installed."
fi

echo "🚀 Logging in to Azure..."
az login

# OPTIONAL: set your subscription (customize if needed)
# az account set --subscription "YOUR_SUBSCRIPTION_ID"

echo "🛠️ Initializing Terraform..."
terraform init

echo "✅ Environment setup complete."

echo ""
echo "📌 To create your AKS cluster, now run:"
echo "terraform apply -auto-approve"
