#!/bin/bash
# Script to configure ingress-nginx for remote access
# For bare metal/VM Kubernetes clusters

set -e

INGRESS_NS="ingress-nginx"
SERVICE_NAME="ingress-nginx-controller"
PUBLIC_IP="${1:-137.204.56.169}"

echo "=========================================="
echo "Ingress-Nginx Remote Access Configuration"
echo "=========================================="
echo ""

# Check current service configuration
echo "🔍 Current service configuration:"
kubectl get svc -n $INGRESS_NS $SERVICE_NAME -o jsonpath='{.spec.type}' | xargs -I {} echo "   Type: {}"
EXTERNAL_IP=$(kubectl get svc -n $INGRESS_NS $SERVICE_NAME -o jsonpath='{.spec.externalIPs[0]}' 2>/dev/null || echo "")
NODEPORT_HTTP=$(kubectl get svc -n $INGRESS_NS $SERVICE_NAME -o jsonpath='{.spec.ports[?(@.name=="http")].nodePort}' 2>/dev/null || echo "")
NODEPORT_HTTPS=$(kubectl get svc -n $INGRESS_NS $SERVICE_NAME -o jsonpath='{.spec.ports[?(@.name=="https")].nodePort}' 2>/dev/null || echo "")

if [[ -n "$EXTERNAL_IP" ]]; then
    echo "   Current ExternalIP: $EXTERNAL_IP"
else
    echo "   ExternalIP: Not set"
fi

if [[ -n "$NODEPORT_HTTP" ]]; then
    echo "   NodePort HTTP: $NODEPORT_HTTP"
fi
if [[ -n "$NODEPORT_HTTPS" ]]; then
    echo "   NodePort HTTPS: $NODEPORT_HTTPS"
fi
echo ""

# Get node IP
NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || echo "")
echo "🖥️  Node Internal IP: ${NODE_IP:-Not found}"
echo "🌐 Public IP to use: $PUBLIC_IP"
echo ""

# Options
echo "Choose configuration option:"
echo "1) Use NodePort with public IP (recommended for bare metal)"
echo "2) Set ExternalIP to match public IP (direct access on port 80/443)"
echo "3) Show current access methods"
echo "4) Exit"
echo ""
read -p "Enter choice [1-4]: " choice

case $choice in
    1)
        echo ""
        echo "✅ Using NodePort method"
        echo ""
        echo "On your laptop, update /etc/hosts:"
        echo "  $PUBLIC_IP kubeintellect.chat.local"
        echo ""
        echo "Then access:"
        echo "  HTTP:  http://kubeintellect.chat.local:$NODEPORT_HTTP"
        echo "  HTTPS: https://kubeintellect.chat.local:$NODEPORT_HTTPS"
        echo ""
        echo "Or if your ingress is configured for port 80, you can use:"
        echo "  http://kubeintellect.chat.local:$NODEPORT_HTTP"
        ;;
    2)
        echo ""
        echo "🔧 Setting ExternalIP to $PUBLIC_IP..."
        echo ""
        
        # Patch the service to set externalIP
        kubectl patch svc -n $INGRESS_NS $SERVICE_NAME -p "{\"spec\":{\"externalIPs\":[\"$PUBLIC_IP\"]}}"
        
        echo "✅ ExternalIP updated!"
        echo ""
        echo "On your laptop, update /etc/hosts:"
        echo "  $PUBLIC_IP kubeintellect.chat.local"
        echo ""
        echo "Then access:"
        echo "  HTTP:  http://kubeintellect.chat.local"
        echo "  HTTPS: https://kubeintellect.chat.local"
        echo ""
        echo "⚠️  Note: Make sure ports 80 and 443 are open on your remote machine firewall"
        ;;
    3)
        echo ""
        echo "📋 Current Access Methods:"
        echo ""
        
        if [[ -n "$EXTERNAL_IP" ]]; then
            echo "Option A: ExternalIP (if firewall allows):"
            echo "  Add to /etc/hosts: $EXTERNAL_IP kubeintellect.chat.local"
            echo "  Access: http://kubeintellect.chat.local"
            echo ""
        fi
        
        if [[ -n "$NODEPORT_HTTP" ]]; then
            echo "Option B: NodePort:"
            echo "  Add to /etc/hosts: $PUBLIC_IP kubeintellect.chat.local"
            echo "  Access: http://kubeintellect.chat.local:$NODEPORT_HTTP"
            echo ""
        fi
        
        echo "Option C: Port Forwarding:"
        echo "  kubectl -n $INGRESS_NS port-forward svc/$SERVICE_NAME 8080:80"
        echo "  Access: http://kubeintellect.chat.local:8080"
        echo ""
        ;;
    4)
        echo "Exiting..."
        exit 0
        ;;
    *)
        echo "Invalid choice"
        exit 1
        ;;
esac

echo ""
echo "💡 Tip: Check firewall rules if access doesn't work:"
echo "   sudo ufw status"
echo "   sudo ufw allow 80/tcp"
echo "   sudo ufw allow 443/tcp"
if [[ -n "$NODEPORT_HTTP" ]]; then
    echo "   sudo ufw allow $NODEPORT_HTTP/tcp"
fi
echo ""

