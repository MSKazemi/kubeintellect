.PHONY: install run run-bg stop logs dev db-init lint test cli \
        local-run local-stop local-logs quickstart \
        kind-cluster-create kind-cluster-cleanup kind-cluster-create-vm \
        kind-build-kubeintellect kind-deploy-kubeintellect kind-redeploy-kubeintellect \
        monitoring-install monitoring-uninstall \
        langfuse-install langfuse-clean \
        vm-deploy-kubeintellect aks-deploy-kubeintellect \
        hosts-entry \
        docs-serve docs-build \
        helm-package check-dist \
        scenarios help

IMAGE_NAME        ?= ghcr.io/mskazemi/kubeintellect-v2
TAG               ?= dev-latest
KIND_CLUSTER_NAME ?= testbed-v2
NAMESPACE         ?= kubeintellect
MONITORING_NS     ?= monitoring

# ═══════════════════════════════════════════════════════════════════════════════
##@ Help
# ═══════════════════════════════════════════════════════════════════════════════

help: ## Show all available targets
	@printf "\n\033[1mKubeIntellect V2 — Makefile Reference\033[0m\n"
	@printf "Defaults: KIND_CLUSTER_NAME=\033[33m$(KIND_CLUSTER_NAME)\033[0m  IMAGE=\033[33m$(IMAGE_NAME):$(TAG)\033[0m  NAMESPACE=\033[33m$(NAMESPACE)\033[0m\n"
	@printf "Override any default inline:  \033[36mmake <target> KIND_CLUSTER_NAME=mycluster\033[0m\n"
	@printf "\n\033[1mLaptop (no K8s — docker compose)\033[0m\n"
	@printf "  \033[36mquickstart\033[0m               Interactive wizard — downloads + configures everything\n"
	@printf "                             \033[2mexample: make quickstart\033[0m\n"
	@printf "  \033[36mlocal-run\033[0m                Start app + Postgres via docker compose\n"
	@printf "                             \033[2mprereq: .env file exists (copy .env.example)\033[0m\n"
	@printf "                             \033[2mexample: make local-run\033[0m\n"
	@printf "  \033[36mlocal-stop\033[0m               Stop docker compose stack\n"
	@printf "  \033[36mlocal-logs\033[0m               Tail app logs from docker compose\n"
	@printf "\n\033[1mLocal K8s cluster (Kind)\033[0m\n"
	@printf "  \033[36mkind-cluster-create\033[0m      Create 2-node Kind cluster with hot-reload mounts (run once)\n"
	@printf "                             \033[2mexample: make kind-cluster-create\033[0m\n"
	@printf "                             \033[2mexample: make kind-cluster-create KIND_CLUSTER_NAME=mydev\033[0m\n"
	@printf "  \033[36mkind-cluster-create-vm\033[0m   Create Kind cluster on Azure VM (no host mounts, run once on VM)\n"
	@printf "  \033[36mkind-cluster-stop\033[0m        Pause cluster containers — state is preserved\n"
	@printf "                             \033[2mexample: make kind-cluster-stop\033[0m\n"
	@printf "  \033[36mkind-cluster-start\033[0m       Resume a stopped cluster\n"
	@printf "                             \033[2mexample: make kind-cluster-start\033[0m\n"
	@printf "  \033[36mkind-cluster-cleanup\033[0m     Delete the cluster entirely (irreversible)\n"
	@printf "                             \033[2mexample: make kind-cluster-cleanup\033[0m\n"
	@printf "  \033[36mkind-build-kubeintellect\033[0m Build Docker image and load it into Kind (run after code changes)\n"
	@printf "                             \033[2mexample: make kind-build-kubeintellect\033[0m\n"
	@printf "                             \033[2mexample: make kind-build-kubeintellect TAG=my-branch\033[0m\n"
	@printf "\n\033[1mObservability (Prometheus · Grafana · Loki · Langfuse)\033[0m\n"
	@printf "  \033[36mmonitoring-install\033[0m       Install Prometheus, Grafana, Loki into 'monitoring' namespace\n"
	@printf "                             \033[2mprereq: Kind or cloud cluster is running\033[0m\n"
	@printf "                             \033[2mexample: make monitoring-install\033[0m\n"
	@printf "  \033[36mmonitoring-uninstall\033[0m     Remove Prometheus, Grafana, Loki\n"
	@printf "  \033[36mlangfuse-install\033[0m         Install Langfuse LLM tracing (optional)\n"
	@printf "                             \033[2mexample: make langfuse-install\033[0m\n"
	@printf "  \033[36mlangfuse-clean\033[0m           Uninstall Langfuse and wipe all PVCs (irreversible)\n"
	@printf "\n\033[1mDeploy KubeIntellect\033[0m\n"
	@printf "  \033[36mkind-deploy-kubeintellect\033[0m     Deploy/upgrade on local Kind via Helm\n"
	@printf "                                   \033[2mprereq: .env with API keys, Kind cluster running\033[0m\n"
	@printf "                                   \033[2mexample: make kind-deploy-kubeintellect\033[0m\n"
	@printf "  \033[36mkind-redeploy-kubeintellect\033[0m   Uninstall + redeploy app only (Langfuse stays)\n"
	@printf "                                   \033[2mexample: make kind-redeploy-kubeintellect\033[0m\n"
	@printf "  \033[36mvm-deploy-kubeintellect\033[0m       Deploy to Kind-on-VM\n"
	@printf "                                   \033[2mprereq: .env + values-production.yaml\033[0m\n"
	@printf "  \033[36maks-deploy-kubeintellect\033[0m      Deploy to AKS / any cloud K8s\n"
	@printf "                                   \033[2mprereq: .env + values-cloud.yaml\033[0m\n"
	@printf "  \033[36mhosts-entry\033[0m                   Add api.kubeintellect.local + langfuse.local to /etc/hosts\n"
	@printf "                                   \033[2mexample: make hosts-entry  (uses sudo)\033[0m\n"
	@printf "\n\033[1mPython development (no cluster needed)\033[0m\n"
	@printf "  \033[36minstall\033[0m      Install Python deps via uv\n"
	@printf "                   \033[2mexample: make install\033[0m\n"
	@printf "  \033[36mrun\033[0m          Start API server on port 8000 (foreground)\n"
	@printf "                   \033[2mprereq: Postgres running\033[0m\n"
	@printf "  \033[36mrun-bg\033[0m       Start API server in background (logs → .server.log)\n"
	@printf "  \033[36mstop\033[0m         Stop background server\n"
	@printf "  \033[36mlogs\033[0m         Tail background server logs\n"
	@printf "  \033[36mdev\033[0m          Start with hot-reload\n"
	@printf "  \033[36mdb-init\033[0m      Apply schema.sql to Postgres\n"
	@printf "  \033[36mlint\033[0m         Run ruff linter + format check\n"
	@printf "  \033[36mtest\033[0m         Run pytest suite\n"
	@printf "  \033[36mcli\033[0m          Open REPL against local Kind cluster\n"
	@printf "                   \033[2mexample: make cli\033[0m\n"
	@printf "\n\033[1mBuild & Docs\033[0m\n"
	@printf "  \033[36mhelm-package\033[0m   Package Helm charts to *.tgz\n"
	@printf "  \033[36mdocs-serve\033[0m     Serve docs at http://127.0.0.1:8001 with live-reload\n"
	@printf "  \033[36mdocs-build\033[0m     Build static docs site → site/\n"
	@printf "\nRun \033[36mmake scenarios\033[0m for step-by-step install guides.\n\n"

scenarios: ## Print step-by-step guides for common install scenarios
	@printf "\n\033[1mInstall Scenarios\033[0m\n"
	@printf "\n\033[36m[A] Fresh laptop — no K8s yet (local dev with Kind)\033[0m\n"
	@printf "    make kind-cluster-create              # create Kind cluster\n"
	@printf "    make kind-build-kubeintellect         # build + load Docker image into Kind\n"
	@printf "    make monitoring-install               # Prometheus + Grafana + Loki\n"
	@printf "    make langfuse-install                 # LLM tracing (optional)\n"
	@printf "    make hosts-entry                      # add hostnames to /etc/hosts\n"
	@printf "    make kind-deploy-kubeintellect        # deploy KubeIntellect via Helm\n"
	@printf "\n\033[36m[B] Already have a K8s cluster with Prometheus + Loki\033[0m\n"
	@printf "    # Point config.prometheusUrl / config.lokiUrl at your existing services\n"
	@printf "    # in deploy/helm/kubeintellect/values-production.yaml, then:\n"
	@printf "    make langfuse-install                 # LLM tracing (optional)\n"
	@printf "    make vm-deploy-kubeintellect          # or: make aks-deploy-kubeintellect\n"
	@printf "\n\033[36m[C] Already have a K8s cluster — no Prometheus/Loki yet\033[0m\n"
	@printf "    make monitoring-install               # install into 'monitoring' namespace\n"
	@printf "    make langfuse-install                 # LLM tracing (optional)\n"
	@printf "    make vm-deploy-kubeintellect          # or: make aks-deploy-kubeintellect\n"
	@printf "\n\033[36m[D] Already have K8s + monitoring — just install KubeIntellect\033[0m\n"
	@printf "    make vm-deploy-kubeintellect          # or: make aks-deploy-kubeintellect\n"
	@printf "\n\033[36m[E] Just install Langfuse (standalone, any cluster)\033[0m\n"
	@printf "    make langfuse-install\n"
	@printf "\n\033[36m[F] Update KubeIntellect after a code change\033[0m\n"
	@printf "    make kind-build-kubeintellect         # rebuild + reload image into Kind\n"
	@printf "    make kind-deploy-kubeintellect        # Helm upgrade in-place (idempotent)\n"
	@printf "\n"

# ═══════════════════════════════════════════════════════════════════════════════
##@ Laptop deployment (no K8s cluster required for the app)
# Run KubeIntellect on your local machine using docker compose.
# The app connects to your cluster via ~/.kube/config.
# ═══════════════════════════════════════════════════════════════════════════════

quickstart: ## Interactive setup wizard (downloads + configures everything)
	bash scripts/setup.sh

local-run: ## Start KubeIntellect + postgres via docker compose (uses .env)
	docker compose up -d
	@echo ""
	@echo "KubeIntellect running at http://localhost:8000"
	@echo "Connect: KUBE_Q_API_KEY=<key> kq"

local-stop: ## Stop the docker compose stack
	docker compose down

local-logs: ## Tail KubeIntellect logs from docker compose
	docker compose logs -f kubeintellect

# ═══════════════════════════════════════════════════════════════════════════════
##@ Local K8s cluster (Kind)
# Use these to create and manage a local Kind cluster for development.
# Skip this section if you already have a K8s cluster (scenario B/C/D above).
# ═══════════════════════════════════════════════════════════════════════════════

kind-cluster-create: ## Create local dev Kind cluster (2-node, hot-reload mounts) — run once
	KIND_CLUSTER_NAME=$(KIND_CLUSTER_NAME) bash scripts/kind/create-kind-cluster.sh

kind-cluster-create-vm: ## Create Kind cluster on an Azure VM (no host mounts) — run once on the VM
	kind create cluster --name $(KIND_CLUSTER_NAME) \
	  --config deploy/kind/kind-config-vm.yaml
	kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.10.1/deploy/static/provider/kind/deploy.yaml
	@echo "Waiting for ingress-nginx to be ready..."
	kubectl wait --namespace ingress-nginx \
	  --for=condition=ready pod \
	  --selector=app.kubernetes.io/component=controller \
	  --timeout=90s

kind-cluster-stop: ## Stop Kind cluster containers (preserves state — resume with kind-cluster-start)
	@CONTAINERS=$$(docker ps -q --filter label=io.x-k8s.kind.cluster=$(KIND_CLUSTER_NAME)); \
	  if [ -z "$$CONTAINERS" ]; then \
	    echo "No running containers found for cluster '$(KIND_CLUSTER_NAME)' — already stopped?"; \
	  else \
	    docker stop $$CONTAINERS && echo "Cluster '$(KIND_CLUSTER_NAME)' stopped."; \
	  fi

kind-cluster-start: ## Start previously stopped Kind cluster containers
	docker start $(shell docker ps -aq --filter label=io.x-k8s.kind.cluster=$(KIND_CLUSTER_NAME))

kind-cluster-cleanup: ## Delete the local Kind cluster entirely
	kind delete cluster --name $(KIND_CLUSTER_NAME)

kind-build-kubeintellect: ## Build the KubeIntellect Docker image and load it into Kind — run after code changes
	uv lock
	docker build -t $(IMAGE_NAME):$(TAG) .
	kind load docker-image $(IMAGE_NAME):$(TAG) --name $(KIND_CLUSTER_NAME)

# ═══════════════════════════════════════════════════════════════════════════════
##@ Observability stack (Prometheus · Grafana · Loki · Langfuse)
# Independent of KubeIntellect — install into the 'monitoring' namespace.
# Skip if your cluster already has these services; just point values.yaml at them.
# ═══════════════════════════════════════════════════════════════════════════════

monitoring-install: ## Install Prometheus, Grafana, Loki + Promtail — skip if cluster already has them
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
	helm repo add grafana https://grafana.github.io/helm-charts 2>/dev/null || true
	helm repo update prometheus-community grafana
	helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
	  -n $(MONITORING_NS) --create-namespace \
	  -f deploy/kind/monitoring-values.yaml \
	  --timeout 10m
	helm upgrade --install loki grafana/loki-stack \
	  -n $(MONITORING_NS) \
	  -f deploy/kind/loki-values.yaml

monitoring-uninstall: ## Remove Prometheus, Grafana, and Loki from the cluster
	helm uninstall kube-prometheus-stack -n $(MONITORING_NS) || true
	helm uninstall loki -n $(MONITORING_NS) || true

langfuse-install: ## Install Langfuse LLM tracing (optional — skip if you don't need trace UI)
	@kubectl create namespace $(MONITORING_NS) --dry-run=client -o yaml | kubectl apply -f -
	helm upgrade --install langfuse deploy/helm/langfuse \
	  -f deploy/helm/langfuse/values.yaml \
	  -f deploy/helm/langfuse/values-kind.yaml \
	  --namespace $(MONITORING_NS) --create-namespace

langfuse-clean: ## Uninstall Langfuse and wipe all trace data (PVCs) — irreversible
	helm uninstall langfuse -n $(MONITORING_NS) || true
	@kubectl delete pvc \
	  langfuse-clickhouse-pvc langfuse-minio-pvc langfuse-postgres-pvc langfuse-redis-pvc \
	  -n $(MONITORING_NS) --ignore-not-found
	@echo "Langfuse PVCs cleared."

# ═══════════════════════════════════════════════════════════════════════════════
##@ Deploy KubeIntellect
# ═══════════════════════════════════════════════════════════════════════════════

kind-deploy-kubeintellect: ## Deploy (or upgrade) KubeIntellect on local Kind via Helm — sources .env for secrets
	@kubectl create namespace $(MONITORING_NS) --dry-run=client -o yaml | kubectl apply -f -
	helm upgrade --install langfuse deploy/helm/langfuse \
	  -f deploy/helm/langfuse/values.yaml \
	  -f deploy/helm/langfuse/values-kind.yaml \
	  --namespace $(MONITORING_NS) --create-namespace
	@bash -c '\
	  set -euo pipefail; \
	  test -f .env || { echo "ERROR: .env not found — copy .env.example and fill in secrets"; exit 1; }; \
	  set -a && source .env && set +a; \
	  LLM_PROVIDER=$${LLM_PROVIDER:-azure}; \
	  if [ "$$LLM_PROVIDER" = "azure" ]; then \
	    : "$${AZURE_OPENAI_API_KEY:?ERROR: AZURE_OPENAI_API_KEY is not set in .env}"; \
	    : "$${AZURE_OPENAI_ENDPOINT:?ERROR: AZURE_OPENAI_ENDPOINT is not set in .env}"; \
	  elif [ "$$LLM_PROVIDER" = "openai" ]; then \
	    : "$${OPENAI_API_KEY:?ERROR: OPENAI_API_KEY is not set in .env}"; \
	  fi; \
	  helm upgrade --install kubeintellect deploy/helm/kubeintellect \
	    -f deploy/helm/kubeintellect/values.yaml \
	    -f deploy/helm/kubeintellect/values-local.yaml \
	    --namespace $(NAMESPACE) --create-namespace \
	    --set postgres.password="$${POSTGRES_PASSWORD:-changeme}" \
	    --set-string config.llmProvider="$${LLM_PROVIDER}" \
	    --set-string secrets.openaiApiKey="$${OPENAI_API_KEY:-}" \
	    --set-string secrets.azureOpenaiApiKey="$${AZURE_OPENAI_API_KEY:-}" \
	    --set-string secrets.azureOpenaiEndpoint="$${AZURE_OPENAI_ENDPOINT:-}" \
	    --set-string secrets.azureCoordinatorDeployment="$${AZURE_COORDINATOR_DEPLOYMENT:-gpt-4o}" \
	    --set-string secrets.azureSubagentDeployment="$${AZURE_SUBAGENT_DEPLOYMENT:-gpt-4o-mini}" \
	    --set-string secrets.adminApiKeys="$${KUBEINTELLECT_ADMIN_KEYS:-}" \
	    --set-string secrets.operatorApiKeys="$${KUBEINTELLECT_OPERATOR_KEYS:-}" \
	    --set-string secrets.readonlyApiKeys="$${KUBEINTELLECT_READONLY_KEYS:-}"; \
	'

kind-redeploy-kubeintellect: ## Uninstall and redeploy KubeIntellect only (Langfuse stays running)
	helm uninstall kubeintellect -n $(NAMESPACE) || true
	@echo "Waiting for KubeIntellect pods to terminate..."
	@kubectl wait --for=delete pod -l app.kubernetes.io/instance=kubeintellect \
	  -n $(NAMESPACE) --timeout=60s 2>/dev/null || true
	$(MAKE) kind-deploy-kubeintellect

vm-deploy-kubeintellect: ## Deploy KubeIntellect to Kind-on-VM — needs .env + values-production.yaml
	@kubectl create namespace $(MONITORING_NS) --dry-run=client -o yaml | kubectl apply -f -
	@bash -c '\
	  set -euo pipefail; \
	  test -f .env || { echo "ERROR: .env not found"; exit 1; }; \
	  test -f deploy/helm/kubeintellect/values-production.yaml || \
	    { echo "ERROR: copy deploy/helm/kubeintellect/values-production.yaml.example → deploy/helm/kubeintellect/values-production.yaml and fill in secrets"; exit 1; }; \
	  set -a && source .env && set +a; \
	  LLM_PROVIDER=$${LLM_PROVIDER:-azure}; \
	  if [ "$$LLM_PROVIDER" = "azure" ]; then \
	    : "$${AZURE_OPENAI_API_KEY:?ERROR: AZURE_OPENAI_API_KEY is not set in .env}"; \
	    : "$${AZURE_OPENAI_ENDPOINT:?ERROR: AZURE_OPENAI_ENDPOINT is not set in .env}"; \
	  elif [ "$$LLM_PROVIDER" = "openai" ]; then \
	    : "$${OPENAI_API_KEY:?ERROR: OPENAI_API_KEY is not set in .env}"; \
	  fi; \
	  helm upgrade --install kubeintellect deploy/helm/kubeintellect \
	    -f deploy/helm/kubeintellect/values.yaml \
	    -f deploy/helm/kubeintellect/values-production.yaml \
	    --namespace $(NAMESPACE) --create-namespace \
	    --set postgres.password="$${POSTGRES_PASSWORD}" \
	    --set-string config.llmProvider="$${LLM_PROVIDER}" \
	    --set-string secrets.openaiApiKey="$${OPENAI_API_KEY:-}" \
	    --set-string secrets.azureOpenaiApiKey="$${AZURE_OPENAI_API_KEY:-}" \
	    --set-string secrets.azureOpenaiEndpoint="$${AZURE_OPENAI_ENDPOINT:-}" \
	    --set-string secrets.azureCoordinatorDeployment="$${AZURE_COORDINATOR_DEPLOYMENT:-gpt-4o}" \
	    --set-string secrets.azureSubagentDeployment="$${AZURE_SUBAGENT_DEPLOYMENT:-gpt-4o-mini}" \
	    --set-string secrets.adminApiKeys="$${KUBEINTELLECT_ADMIN_KEYS:-}" \
	    --set-string secrets.operatorApiKeys="$${KUBEINTELLECT_OPERATOR_KEYS:-}" \
	    --set-string secrets.readonlyApiKeys="$${KUBEINTELLECT_READONLY_KEYS:-}"; \
	'

aks-deploy-kubeintellect: ## Deploy KubeIntellect to AKS / any cloud K8s — needs .env + values-cloud.yaml
	@bash -c '\
	  set -euo pipefail; \
	  test -f .env || { echo "ERROR: .env not found"; exit 1; }; \
	  test -f deploy/helm/kubeintellect/values-cloud.yaml || \
	    { echo "ERROR: copy deploy/helm/kubeintellect/values-cloud.yaml.example → deploy/helm/kubeintellect/values-cloud.yaml and fill in secrets"; exit 1; }; \
	  set -a && source .env && set +a; \
	  LLM_PROVIDER=$${LLM_PROVIDER:-azure}; \
	  if [ "$$LLM_PROVIDER" = "azure" ]; then \
	    : "$${AZURE_OPENAI_API_KEY:?ERROR: AZURE_OPENAI_API_KEY is not set in .env}"; \
	    : "$${AZURE_OPENAI_ENDPOINT:?ERROR: AZURE_OPENAI_ENDPOINT is not set in .env}"; \
	  elif [ "$$LLM_PROVIDER" = "openai" ]; then \
	    : "$${OPENAI_API_KEY:?ERROR: OPENAI_API_KEY is not set in .env}"; \
	  fi; \
	  helm upgrade --install kubeintellect deploy/helm/kubeintellect \
	    -f deploy/helm/kubeintellect/values.yaml \
	    -f deploy/helm/kubeintellect/values-cloud.yaml \
	    --namespace $(NAMESPACE) --create-namespace \
	    --set postgres.password="$${POSTGRES_PASSWORD}" \
	    --set-string config.llmProvider="$${LLM_PROVIDER}" \
	    --set-string secrets.openaiApiKey="$${OPENAI_API_KEY:-}" \
	    --set-string secrets.azureOpenaiApiKey="$${AZURE_OPENAI_API_KEY:-}" \
	    --set-string secrets.azureOpenaiEndpoint="$${AZURE_OPENAI_ENDPOINT:-}" \
	    --set-string secrets.azureCoordinatorDeployment="$${AZURE_COORDINATOR_DEPLOYMENT:-gpt-4o}" \
	    --set-string secrets.azureSubagentDeployment="$${AZURE_SUBAGENT_DEPLOYMENT:-gpt-4o-mini}" \
	    --set-string secrets.adminApiKeys="$${KUBEINTELLECT_ADMIN_KEYS:-}" \
	    --set-string secrets.operatorApiKeys="$${KUBEINTELLECT_OPERATOR_KEYS:-}" \
	    --set-string secrets.readonlyApiKeys="$${KUBEINTELLECT_READONLY_KEYS:-}"; \
	'

hosts-entry: ## Add api.kubeintellect.local + langfuse.local to /etc/hosts — run once for local dev
	@grep -q "api.kubeintellect.local" /etc/hosts || \
	  echo "127.0.0.1 api.kubeintellect.local" | sudo tee -a /etc/hosts
	@grep -q "langfuse.local" /etc/hosts || \
	  echo "127.0.0.1 langfuse.local" | sudo tee -a /etc/hosts

# ═══════════════════════════════════════════════════════════════════════════════
##@ Python development (local, no cluster needed)
# ═══════════════════════════════════════════════════════════════════════════════

install: ## Install Python dependencies
	uv sync

run: ## Start API server locally (port 8000) — requires a running Postgres
	uv run uvicorn app.main:app --host 0.0.0.0 --port 8000

run-bg: ## Start API server in background (logs → .server.log, PID → .server.pid)
	@uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 \
	  >> .server.log 2>&1 & echo $$! > .server.pid
	@echo "KubeIntellect started (PID $$(cat .server.pid)) — http://localhost:8000"
	@echo "Logs: tail -f .server.log   Stop: make stop"

stop: ## Stop the background API server started by run-bg
	@if [ -f .server.pid ]; then \
	  PID=$$(cat .server.pid); \
	  kill "$$PID" 2>/dev/null && echo "Stopped PID $$PID" || echo "Process $$PID not running"; \
	  rm -f .server.pid; \
	else \
	  echo "No .server.pid found — nothing to stop"; \
	fi

logs: ## Tail logs from the background API server
	tail -f .server.log

dev: ## Start API server with hot-reload — requires a running Postgres
	uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

db-init: ## Apply schema.sql to the configured Postgres database
	psql $$(uv run python -c "from app.core.config import settings; print(settings.POSTGRES_DSN)") -f app/db/schema.sql

lint: ## Run ruff linter and format check
	uv run ruff check app/
	uv run ruff format --check app/

test: ## Run the pytest test suite
	uv run pytest tests/ -v

cli: ## Open the interactive REPL against the local Kind deployment
	KUBE_Q_URL=http://api.kubeintellect.local uv run kq

# ═══════════════════════════════════════════════════════════════════════════════
##@ Build & publish
# ═══════════════════════════════════════════════════════════════════════════════

helm-package: ## Package both Helm charts (output: *.tgz)
	helm package deploy/helm/kubeintellect/
	helm package deploy/helm/langfuse/

check-dist: ## Build wheel and verify it contains only app/ code — no docs, Helm, secrets
	@rm -rf dist/
	@uv build --wheel -q
	@echo "── Wheel contents ──────────────────────────────────────────────"
	@python3 -m zipfile -l dist/*.whl
	@echo ""
	@echo "── Checking for unexpected paths ───────────────────────────────"
	@python3 -c "\
import zipfile, sys, glob; \
whl = glob.glob('dist/*.whl')[0]; \
bad = [n for n in zipfile.ZipFile(whl).namelist() \
       if not n.startswith('app/') and '.dist-info/' not in n]; \
(print('FAIL — unexpected files:\\n  ' + '\\n  '.join(bad)) or sys.exit(1)) if bad \
else print('OK — only app/ and dist-info')"
	@echo ""
	@echo "── Scanning for hardcoded secrets ──────────────────────────────"
	@! grep -rn --include="*.py" \
	  -E '(sk-proj-[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|ghp_[A-Za-z0-9]{36}|sk-lf-[a-f0-9-]{30,})' \
	  app/ && echo "OK — no secrets found"
	@echo ""
	@echo "── All checks passed ───────────────────────────────────────────"

# ═══════════════════════════════════════════════════════════════════════════════
##@ Documentation
# ═══════════════════════════════════════════════════════════════════════════════

docs-serve: ## Serve docs locally with live-reload (http://127.0.0.1:8001)
	uv run mkdocs serve --dev-addr 127.0.0.1:8001

docs-build: ## Build static docs site (output: site/)
	uv run mkdocs build
