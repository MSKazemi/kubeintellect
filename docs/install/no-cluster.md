---
description: >-
  No Kubernetes cluster, no Docker? Three paths: browser demo (zero install), kube-q CLI against our hosted server, or install Docker and create a local Kind cluster in minutes.
---

# Install: No Cluster, No Docker

Three ways to try KubeIntellect without a cluster or Docker:

|  | Browser demo | Option A — kube-q CLI | Option B — Local cluster |
|--|:------------:|:---------------------:|:------------------------:|
| **Setup** | None | ~2 min | ~5 min |
| **Install** | Nothing | `kube-q` only | Docker + `kubeintellect` |
| **Speed** | Slower† | Fast | Fast |
| **Access** | Read-only | Read-only | Full (HITL-gated) |
| **RCA scenarios** | Yes | Yes | Yes (if selected) |

† The browser terminal shares a single backend instance — responses may be slower under load.

---

## Try it in your browser (zero install)

No install, no terminal. Open **[kubeintellect.com/demo](https://kubeintellect.com/demo)** and start querying immediately.

!!! warning "Slower and limited"
    The demo terminal shares a single hosted instance. Responses are slower under concurrent load,
    and access is read-only — destructive operations (delete, restart, scale) are disabled.

---

## Option A — kube-q CLI

Install only the thin CLI client and connect it to our hosted KubeIntellect instance.
`kq` already defaults to `https://api.kubeintellect.com`, so all you need is a personal API key.

**Requirements:** Python 3.12+

### 1. Get your personal API key

Go to **[kubeintellect.com/demo](https://kubeintellect.com/demo)**, enter your email, and your key
appears instantly on the page and is emailed to you. It looks like:

```
ki-ro-dXNlckBleGFtcGxlLmNvbQ.a1b2c3d4e5f6g7h8i9j0k1l2
```

Keys expire after 30 days — request a new one at any time from the same page.

### 2. Install kube-q

```bash
pip install kube-q
```

> `kq: command not found`? Add `~/.local/bin` to your PATH:
> ```bash
> echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
> ```

### 3. Connect

```bash
kq --api-key ki-ro-dev
```

Or save it permanently so you never type it again:

```bash
mkdir -p ~/.kube-q
echo "KUBE_Q_API_KEY=ki-ro-dev" >> ~/.kube-q/.env
kq
```

!!! note "Read-only access"
    The demo cluster is shared. Destructive operations (delete, restart, scale) are disabled.
    For full access use [Option B](#option-b--local-cluster) or
    [connect to your own cluster](existing-cluster.md).

---

## Option B — Local Cluster

Install Docker, then let `kubeintellect init` handle everything else: Kind cluster creation, sample
workloads, optional observability stack, and RCA practice scenarios.

**Requirements:** Python 3.12+, an LLM API key (OpenAI or Azure OpenAI)

### 1. Install Docker

=== "Linux"

    ```bash
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker $USER
    newgrp docker          # apply group change without logging out
    docker run hello-world # verify
    ```

=== "macOS"

    Install **[Docker Desktop](https://www.docker.com/products/docker-desktop/)**, launch it, and wait for the whale icon in the menu bar before continuing.

=== "Windows (WSL 2)"

    Install **[Docker Desktop](https://www.docker.com/products/docker-desktop/)** with WSL 2 integration enabled, then run the remaining steps inside your WSL terminal.

### 2. Install KubeIntellect

```bash
pip install kubeintellect
```

??? tip "Ubuntu 22.04 — Python 3.10 ships by default, you need 3.12+"
    ```bash
    sudo add-apt-repository ppa:deadsnakes/ppa -y
    sudo apt-get install -y python3.12 python3.12-distutils
    python3.12 -m pip install kubeintellect
    ```

> `kubeintellect: command not found`? Fix PATH:
> ```bash
> echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
> ```

### 3. Configure and start

!!! tip "Prefer editing a file directly?"
    Create `~/.kubeintellect/.env` from the
    [pip install template](../configuration.md#pip-install-template) — fill in your LLM key,
    save, then run `kubeintellect serve`. Skip the rest of this step.

Otherwise, run the interactive wizard:

```bash
kubeintellect init
```

Because no `~/.kube/config` exists yet, the wizard offers to create a cluster automatically.
Recommended answers:

| Prompt | Answer |
|--------|--------|
| LLM provider | `1` OpenAI or `2` Azure OpenAI |
| API key (and endpoint for Azure) | Your key |
| Create a local Kind cluster with sample workloads? | **Y** |
| *(kind, kubectl, helm installed automatically if missing)* | — |
| Install observability stack (Prometheus, Grafana, Loki)? | **Y** |
| Create RCA demo scenarios? | **Y** — 5 broken pods to practice root-cause analysis |
| Install as background service? | **Y** — server starts automatically on every login |

When `init` finishes it:

- Creates a 1-node Kind cluster named `kubeintellect`
- Deploys sample workloads in the `demo` namespace (nginx ×2, httpbin ×1)
- Installs Prometheus + Grafana (NodePort 30090 / 30080) and Loki (NodePort 30100) — if selected
- Deploys 5 RCA practice scenarios in `demo-rca` namespace — if selected
- Configures cluster DNS so `svc.cluster.local` resolves from your host
- Writes `~/.kubeintellect/.env` with all URLs set automatically
- Configures `kube-q` (`~/.kube-q/.env`) with your API key
- Installs a systemd service so the server starts on every login

### 4. Open a new terminal and start querying

```bash
kq
```

No API key to copy — `init` configured everything automatically.

### 5. Verify

```bash
kubeintellect status
```

Expected output (with Kind cluster and observability):

```
  Config:    ✓  ~/.kubeintellect/.env
  LLM:       ✓  openai / gpt-4o
  DB:        ✓  sqlite  ~/.kubeintellect/kubeintellect.db
  kubectl:   ✓  found
  Kube:      ✓  ~/.kube/config  context: kind-kubeintellect
  Auth:      ✓  enabled
    admin     ki-admin-xxxxxxxxxxxxxxxxxxxx
  Prometheus:✓  http://172.18.0.2:30090  reachable
  Loki:      ✓  http://172.18.0.2:30100  reachable
  Grafana:   ✓  http://172.18.0.2:30080  reachable
  kube-q:    ✓  found
```

### Try the RCA scenarios

```bash
kq
```

Ask questions like:

- *"what pods are broken in the demo-rca namespace?"*
- *"why is crash-loop crashing and how do I fix it?"*
- *"why is resource-hog pending?"*
- *"why does the api-server service have no endpoints?"*

The 5 scenarios cover: `CrashLoopBackOff`, `OOMKilled`, `ImagePullBackOff`, `Pending` (resource exhaustion), and a service with no endpoints.

---

## Managing the service (Option B)

```bash
kubeintellect service status     # check if server is running
kubeintellect service logs       # tail live logs
kubeintellect service stop       # stop the server
kubeintellect service start      # start it again
kubeintellect service uninstall  # remove the service entirely
```

---

## Next steps

- Already have a cluster? → [Connect to an existing cluster](existing-cluster.md)
- Want full monitoring + Langfuse for local dev? → [Kind dev environment](../deploy/kind.md)
- Deploy to production (AKS, EKS, GKE)? → [Helm / cloud](../deploy/cloud.md)
