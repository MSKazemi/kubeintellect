#!/bin/bash

NAMESPACE="ingress-nginx"
SERVICE="ingress-nginx-controller"
HOSTNAME="kubeintellect.chat.local"

echo "ℹ️ Attempting to get external IP for service $SERVICE in namespace $NAMESPACE ..."

# Step 1: Try to get LoadBalancer IP
EXTERNAL_IP=$(kubectl get svc $SERVICE -n $NAMESPACE -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null)

if [[ -z "$EXTERNAL_IP" ]]; then
    echo "⚠️  No EXTERNAL-IP found — assuming Kind setup. Falling back to 127.0.0.1"
    EXTERNAL_IP="127.0.0.1"
else
    echo "✅ External IP found: $EXTERNAL_IP"
fi

# Step 2: Check if the hostname already exists in /etc/hosts
if grep -q "[[:space:]]$HOSTNAME" /etc/hosts; then
    CURRENT_IP=$(grep "[[:space:]]$HOSTNAME" /etc/hosts | awk '{print $1}')
    echo "ℹ️ Entry for $HOSTNAME already exists with IP $CURRENT_IP."
    read -rp "Do you want to overwrite it with $EXTERNAL_IP? (yes/no): " CONFIRM
    if [[ "$CONFIRM" != "yes" ]]; then
        echo "❌ Aborted by user. No changes made."
        exit 0
    fi
    # Remove old entry (make a backup first)
    sudo sed -i.bak "/[[:space:]]$HOSTNAME/d" /etc/hosts
else
    # Make a backup before editing
    sudo cp /etc/hosts /etc/hosts.bak
fi

# Step 3: Add the new entry
echo "$EXTERNAL_IP $HOSTNAME" | sudo tee -a /etc/hosts > /dev/null

echo "✅ Successfully mapped $HOSTNAME to $EXTERNAL_IP in /etc/hosts."
