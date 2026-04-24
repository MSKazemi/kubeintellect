#!/usr/bin/env bash
# Setup nginx as reverse proxy for KubeIntellect on Azure VM.
# Run once after Kind cluster is up.
# Usage: bash scripts/vm/setup-nginx.sh
set -euo pipefail

sudo tee /etc/nginx/sites-available/kubeintellect << 'EOF'
server {
    listen 80;
    server_name api.kubeintellect.com;

    # CRITICAL: SSE streaming requires buffering disabled.
    proxy_buffering          off;
    proxy_cache              off;
    proxy_read_timeout       600s;
    keepalive_timeout        620s;

    location / {
        proxy_pass           http://127.0.0.1:18080;
        proxy_set_header     Host api.kubeintellect.local;
        proxy_set_header     X-Real-IP $remote_addr;
        proxy_set_header     X-Forwarded-Proto $scheme;
        proxy_http_version   1.1;
        proxy_set_header     Connection "";
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/kubeintellect /etc/nginx/sites-enabled/kubeintellect
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl start nginx

echo "✅ nginx configured — test with: curl http://api.kubeintellect.com/healthz"
