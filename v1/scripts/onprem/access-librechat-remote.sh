#!/bin/bash
# Script to help access LibreChat from a remote laptop
# The Kubernetes cluster is running on a remote computer

set -e

NAMESPACE="kubeintellect"
INGRESS_NS="ingress-nginx"

echo "=========================================="
echo "LibreChat Remote Access Helper"
echo "=========================================="
echo ""

# Function to show port forwarding option
show_port_forward() {
    echo "📡 OPTION 1: Port Forwarding (Easiest)"
    echo "----------------------------------------"
    echo ""
    echo "On your laptop, run one of these commands:"
    echo ""
    echo "A) Forward LibreChat service directly:"
    echo "   kubectl -n $NAMESPACE port-forward svc/librechat 3080:3080"
    echo "   Then open: http://localhost:3080"
    echo ""
    echo "B) Forward Ingress controller:"
    echo "   kubectl -n $INGRESS_NS port-forward svc/ingress-nginx-controller 8080:80"
    echo "   Then open: http://kubeintellect.chat.local:8080"
    echo "   (Make sure kubeintellect.chat.local points to 127.0.0.1 in /etc/hosts)"
    echo ""
    echo "C) Forward KubeIntellect API:"
    echo "   kubectl -n $NAMESPACE port-forward svc/kubeintellect-core-service 8000:80"
    echo "   Then open: http://localhost:8000"
    echo ""
}

# Function to show ingress option
show_ingress() {
    echo "🌐 OPTION 2: Ingress with Host/IP (Permanent)"
    echo "----------------------------------------"
    echo ""
    
    # Try to get external IP
    EXTERNAL_IP=$(kubectl get svc -n $INGRESS_NS ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || echo "")
    
    if [[ -n "$EXTERNAL_IP" ]]; then
        echo "✅ Found External IP: $EXTERNAL_IP"
        echo ""
        echo "On your laptop, add to /etc/hosts:"
        echo "  $EXTERNAL_IP kubeintellect.chat.local"
        echo "  $EXTERNAL_IP kubeintellect.api.local"
        echo ""
        echo "Then access: http://kubeintellect.chat.local"
    else
        # Try NodePort
        NODEPORT=$(kubectl get svc -n $INGRESS_NS ingress-nginx-controller -o jsonpath='{.spec.ports[?(@.name=="http")].nodePort}' 2>/dev/null || echo "")
        
        if [[ -n "$NODEPORT" ]]; then
            echo "⚠️  No External IP found. Using NodePort: $NODEPORT"
            echo ""
            echo "Get the remote node IP first:"
            echo "  kubectl get nodes -o wide"
            echo ""
            echo "Then on your laptop, add to /etc/hosts:"
            echo "  <REMOTE_NODE_IP> kubeintellect.chat.local"
            echo ""
            echo "Access at: http://kubeintellect.chat.local:$NODEPORT"
        else
            echo "⚠️  No External IP or NodePort found."
            echo ""
            echo "Check ingress controller service:"
            echo "  kubectl get svc -n $INGRESS_NS ingress-nginx-controller"
            echo ""
            echo "If it's ClusterIP only, you need to:"
            echo "  1. Change service type to NodePort or LoadBalancer, OR"
            echo "  2. Use port forwarding (Option 1)"
        fi
    fi
    echo ""
}

# Function to check current setup
check_setup() {
    echo "🔍 Checking current setup..."
    echo ""
    
    # Check if ingress exists
    if kubectl get ingress -n $NAMESPACE librechat &>/dev/null; then
        echo "✅ LibreChat ingress found"
        INGRESS_HOST=$(kubectl get ingress -n $NAMESPACE librechat -o jsonpath='{.spec.rules[0].host}' 2>/dev/null || echo "not found")
        echo "   Host: $INGRESS_HOST"
    else
        echo "❌ LibreChat ingress not found"
    fi
    
    # Check ingress controller
    if kubectl get svc -n $INGRESS_NS ingress-nginx-controller &>/dev/null; then
        echo "✅ Ingress controller service found"
        SERVICE_TYPE=$(kubectl get svc -n $INGRESS_NS ingress-nginx-controller -o jsonpath='{.spec.type}' 2>/dev/null || echo "unknown")
        echo "   Type: $SERVICE_TYPE"
    else
        echo "❌ Ingress controller not found"
    fi
    
    echo ""
}

# Main menu
echo "Choose an option:"
echo "1) Show port forwarding commands"
echo "2) Show ingress configuration"
echo "3) Check current setup"
echo "4) Show all options"
echo ""
read -p "Enter choice [1-4]: " choice

case $choice in
    1)
        show_port_forward
        ;;
    2)
        show_ingress
        ;;
    3)
        check_setup
        ;;
    4)
        check_setup
        show_port_forward
        echo ""
        show_ingress
        ;;
    *)
        echo "Invalid choice. Showing all options..."
        check_setup
        show_port_forward
        echo ""
        show_ingress
        ;;
esac

echo ""
echo "💡 Tip: Port forwarding is easiest for development."
echo "   Ingress is better for permanent access."




