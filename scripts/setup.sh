#!/usr/bin/env bash
# KubeIntellect — laptop quickstart
#
# This script sets up KubeIntellect on your local machine.
# Your machine needs access to a Kubernetes cluster via ~/.kube/config.
# KubeIntellect runs locally; it does NOT get deployed into your cluster.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/MSKazemi/kubeintellect/main/scripts/setup.sh | bash
#   # or locally:
#   bash scripts/setup.sh

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
RESET='\033[0m'

info()  { echo -e "${GREEN}✓${RESET} $*"; }
warn()  { echo -e "${YELLOW}!${RESET} $*"; }
error() { echo -e "${RED}✗${RESET} $*" >&2; }
bold()  { echo -e "${BOLD}$*${RESET}"; }

echo ""
bold "KubeIntellect — laptop quickstart"
echo "────────────────────────────────────────────────────────────────"
echo ""

# ── prerequisite checks ───────────────────────────────────────────────────────

check_cmd() {
    if ! command -v "$1" &>/dev/null; then
        error "$1 not found — please install it first"
        return 1
    fi
    info "$1 found"
}

bold "Checking prerequisites..."
HAVE_DOCKER=true
HAVE_PYTHON=true

check_cmd kubectl || { error "kubectl is required"; exit 1; }

if ! check_cmd docker 2>/dev/null; then
    HAVE_DOCKER=false
    warn "Docker not found — will use pip install approach"
fi

if ! check_cmd python3 2>/dev/null; then
    HAVE_PYTHON=false
fi

# Check kubeconfig
if [ -z "${KUBECONFIG:-}" ] && [ ! -f "$HOME/.kube/config" ]; then
    warn "No kubeconfig found at ~/.kube/config and KUBECONFIG is not set"
    warn "Make sure your cluster credentials are configured before running kubeintellect"
else
    info "kubeconfig found"
fi

echo ""

# ── choose installation method ────────────────────────────────────────────────

if [ "$HAVE_DOCKER" = true ]; then
    bold "Installation method:"
    echo "  1  Docker Compose  (recommended — includes postgres automatically)"
    echo "  2  pip install     (requires a separate PostgreSQL instance)"
    read -r -p "Choose [1/2] (default: 1): " METHOD
    METHOD="${METHOD:-1}"
else
    METHOD="2"
fi

echo ""

# ── collect config ────────────────────────────────────────────────────────────

bold "LLM provider:"
echo "  1  OpenAI"
echo "  2  Azure OpenAI"
read -r -p "Choose [1/2] (default: 1): " LLM_CHOICE
LLM_CHOICE="${LLM_CHOICE:-1}"

if [ "$LLM_CHOICE" = "2" ]; then
    LLM_PROVIDER="azure"
    read -r -p "AZURE_OPENAI_API_KEY: " AZURE_OPENAI_API_KEY
    read -r -p "AZURE_OPENAI_ENDPOINT (https://...): " AZURE_OPENAI_ENDPOINT
    LLM_LINES="LLM_PROVIDER=azure
AZURE_OPENAI_API_KEY=${AZURE_OPENAI_API_KEY}
AZURE_OPENAI_ENDPOINT=${AZURE_OPENAI_ENDPOINT}
AZURE_COORDINATOR_DEPLOYMENT=gpt-4o
AZURE_SUBAGENT_DEPLOYMENT=gpt-4o-mini"
else
    LLM_PROVIDER="openai"
    read -r -p "OPENAI_API_KEY: " OPENAI_API_KEY
    LLM_LINES="LLM_PROVIDER=openai
OPENAI_API_KEY=${OPENAI_API_KEY}
OPENAI_COORDINATOR_MODEL=gpt-4o
OPENAI_SUBAGENT_MODEL=gpt-4o-mini"
fi

# Generate secrets
PG_PASS="$(python3 -c 'import secrets; print(secrets.token_hex(16))' 2>/dev/null || openssl rand -hex 16)"
ADMIN_KEY="ki-admin-$(python3 -c 'import secrets; print(secrets.token_hex(10))' 2>/dev/null || openssl rand -hex 10)"

echo ""
info "Generated PostgreSQL password: ${PG_PASS}"
info "Generated admin API key:       ${ADMIN_KEY}"
echo ""

# ── method 1: docker compose ──────────────────────────────────────────────────

if [ "$METHOD" = "1" ]; then
    ENV_FILE=".env"

    # Write or update .env
    cat > "$ENV_FILE" <<EOF
${LLM_LINES}

POSTGRES_PASSWORD=${PG_PASS}
POSTGRES_HOST=postgres
POSTGRES_DB=kubeintellectdb
POSTGRES_USER=kubeuser

KUBEINTELLECT_ADMIN_KEYS=${ADMIN_KEY}
LOG_LEVEL=INFO
EOF

    info ".env written to ${ENV_FILE}"

    bold "Starting KubeIntellect..."
    docker compose pull --quiet
    docker compose up -d

    echo ""
    bold "Waiting for KubeIntellect to be healthy..."
    for i in $(seq 1 30); do
        if curl -sf http://localhost:8000/healthz &>/dev/null; then
            info "KubeIntellect is up!"
            break
        fi
        if [ "$i" -eq 30 ]; then
            warn "Server not responding after 30s — check: docker compose logs kubeintellect"
        fi
        sleep 2
    done

# ── method 2: pip install ─────────────────────────────────────────────────────

else
    mkdir -p "$HOME/.kubeintellect"
    ENV_FILE="$HOME/.kubeintellect/.env"

    cat > "$ENV_FILE" <<EOF
${LLM_LINES}

POSTGRES_PASSWORD=${PG_PASS}
POSTGRES_HOST=localhost
POSTGRES_DB=kubeintellectdb
POSTGRES_USER=kubeuser

KUBEINTELLECT_ADMIN_KEYS=${ADMIN_KEY}
LOG_LEVEL=INFO
EOF

    info "Config written to ${ENV_FILE}"

    if [ "$HAVE_PYTHON" = true ]; then
        bold "Installing kubeintellect..."
        pip install kubeintellect --quiet
        info "kubeintellect installed"
    else
        warn "Python not found — install manually: pip install kubeintellect"
    fi

    warn "You need a running PostgreSQL instance."
    warn "Quick option: docker run -d -p 5432:5432 \\"
    warn "  -e POSTGRES_DB=kubeintellectdb -e POSTGRES_USER=kubeuser \\"
    warn "  -e POSTGRES_PASSWORD=${PG_PASS} postgres:15-alpine"
fi

# ── install kube-q ────────────────────────────────────────────────────────────

echo ""
bold "Installing kube-q CLI..."
if command -v pip3 &>/dev/null; then
    pip3 install kube-q --quiet && info "kube-q installed"
elif command -v pip &>/dev/null; then
    pip install kube-q --quiet && info "kube-q installed"
else
    warn "pip not found — install kube-q manually: pip install kube-q"
fi

# ── summary ───────────────────────────────────────────────────────────────────

echo ""
echo "────────────────────────────────────────────────────────────────"
bold "Setup complete!"
echo ""
if [ "$METHOD" = "1" ]; then
    echo "  Backend: http://localhost:8000  (docker compose)"
else
    echo "  Backend: run 'kubeintellect serve' to start"
fi
echo ""
echo "  Connect with kube-q:"
echo "    KUBE_Q_API_KEY=${ADMIN_KEY} kq"
echo ""
echo "  Or set it permanently:"
echo "    mkdir -p ~/.kube-q"
echo "    echo 'KUBE_Q_URL=http://localhost:8000' >> ~/.kube-q/.env"
echo "    echo 'KUBE_Q_API_KEY=${ADMIN_KEY}' >> ~/.kube-q/.env"
echo "    kq"
echo "────────────────────────────────────────────────────────────────"
