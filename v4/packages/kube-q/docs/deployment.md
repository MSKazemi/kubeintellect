# Deployment & Hosting

The kube-q web terminal is a Docker container. You need somewhere to run it so users can reach it from a browser.

---

## Hosting options

| Platform | Free tier | WebSockets | Always-on | Docker | Verdict |
|---|---|---|---|---|---|
| **Oracle Cloud** | 4 CPU / 24 GB RAM — forever | ✓ | ✓ | ✓ | Most generous, manual setup |
| **Fly.io** | 3 shared VMs | ✓ | ✓ | ✓ | Best managed option |
| **Railway** | $5 credit/mo | ✓ | ✓ | ✓ | Easiest setup |
| **Render** | Yes | ✓ | ✗ (sleeps after 15 min) | ✓ | Bad for a terminal |
| **Koyeb** | Yes | ✓ | ✓ | ✓ | Good alternative |

---

## Oracle Cloud (recommended for free hosting)

Oracle Cloud has the most generous free tier of any cloud provider — it never expires and never charges you. The only trade-off is manual setup.

### What you get — free forever

- **4 ARM CPU cores + 24 GB RAM** (Ampere A1) — use all 4 on one VM
- **2 AMD VMs** (1 CPU, 1 GB RAM each)
- **200 GB storage**
- No time limit, no credit card expiry

A single ARM VM with 2 cores and 12 GB RAM is more than enough for the kube-q web terminal.

### Setup

**1. Create a free account**

Go to `cloud.oracle.com` → sign up → select your home region (pick one close to you — you cannot change it later).

**2. Create a free VM**

- Compute → Instances → Create Instance
- Shape: **Ampere A1** (ARM) → 2 OCPUs, 12 GB RAM
- Image: **Ubuntu 22.04**
- Add your SSH public key

**3. Install Docker**

```bash
ssh ubuntu@<your-vm-ip>

sudo apt update && sudo apt install -y docker.io
sudo usermod -aG docker ubuntu
# log out and back in for the group to take effect
```

**4. Open port 3000**

In Oracle Console → Networking → VCN → Security List → add an ingress rule for TCP port 3000.

Also allow it in the VM's local firewall:

```bash
sudo iptables -I INPUT -p tcp --dport 3000 -j ACCEPT
```

**5. Run the container**

```bash
docker run -d \
  --name kube-q \
  --restart always \
  -p 3000:3000 \
  -e KUBE_Q_URL=https://your-backend.com \
  -e KUBE_Q_API_KEY=your-key \
  ghcr.io/mskazemi/kube_q:latest
```

Open `http://<your-vm-ip>:3000` — done.

### Add HTTPS (optional but recommended)

Install nginx and Certbot for a proper domain + TLS:

```bash
sudo apt install -y nginx certbot python3-certbot-nginx

# Add your domain's A record pointing to the VM IP first, then:
sudo certbot --nginx -d kube-q.example.com
```

Nginx config (`/etc/nginx/sites-available/kube-q`):

```nginx
server {
    listen 443 ssl;
    server_name kube-q.example.com;

    location / {
        proxy_pass http://localhost:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
    }
}
```

!!! note
    The `Upgrade` and `Connection` headers are required for WebSocket (PTY) connections to work through nginx.

---

## Fly.io (easiest managed option)

Fly.io deploys directly from your Dockerfile with one command and provides automatic HTTPS.

```bash
# Install flyctl
curl -L https://fly.io/install.sh | sh

# From the kube_q project root
fly launch        # detects Dockerfile automatically
fly secrets set KUBE_Q_URL=https://your-backend.com
fly secrets set KUBE_Q_API_KEY=your-key
fly deploy
```

Your app will be live at `https://kube-q.fly.dev` (or a name you choose). Free tier includes 3 shared VMs with automatic HTTPS and built-in WebSocket support.

---

## Railway

Connect your GitHub repo and Railway auto-deploys on every push to `main`.

1. Go to `railway.app` → New Project → Deploy from GitHub repo
2. Select `MSKazemi/kube_q`
3. Add environment variables: `KUBE_Q_URL`, `KUBE_Q_API_KEY`
4. Railway detects the `Dockerfile` and builds automatically

Free tier includes $5 credit/month — enough for light usage.

---

## Oracle vs Fly.io comparison

| | Oracle Cloud | Fly.io |
|---|---|---|
| Setup effort | ~30 min manual | 5 min, one command |
| Resources | 4 CPU / 24 GB RAM | Shared, limited |
| Cost | Free forever | Free tier, then pay-as-you-go |
| HTTPS | Manual (nginx + Certbot) | Automatic |
| Auto-deploy on push | Manual (you set up CI) | Built-in |

**Oracle** is better long-term value (much more RAM/CPU, truly free forever).  
**Fly.io** is faster to get running with less ops work.
