#!/usr/bin/env bash
# Add TLS to nginx using Let's Encrypt (certbot).
# Run after DNS has propagated: dig api.kubeintellect.com should return 20.119.62.10
# Usage: bash scripts/vm/setup-tls.sh
set -euo pipefail

DOMAIN="api.kubeintellect.com"

# Install certbot if not present
sudo apt-get install -y certbot python3-certbot-nginx

# Get certificate and auto-configure nginx
sudo certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m admin@kubeintellect.com

# Add HTTPS→HTTP upgrade for SSE in nginx config
sudo tee /etc/nginx/sites-available/kubeintellect << 'EOF'
server {
    listen 80;
    server_name api.kubeintellect.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name api.kubeintellect.com;

    ssl_certificate     /etc/letsencrypt/live/api.kubeintellect.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.kubeintellect.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;

    # CRITICAL: SSE streaming requires buffering disabled.
    proxy_buffering          off;
    proxy_cache              off;
    proxy_read_timeout       600s;
    keepalive_timeout        620s;

    location / {
        proxy_pass           http://127.0.0.1:18080;
        proxy_set_header     Host api.kubeintellect.local;
        proxy_set_header     X-Real-IP $remote_addr;
        proxy_set_header     X-Forwarded-Proto https;
        proxy_http_version   1.1;
        proxy_set_header     Connection "";
    }
}
EOF

sudo nginx -t && sudo systemctl reload nginx

echo "✅ TLS configured — test with: curl https://api.kubeintellect.com/healthz"
