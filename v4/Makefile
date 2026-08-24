.PHONY: install run run-bg stop logs dev dev-postgres dev-postgres-stop db-init db-purge lint typecheck test test-memory cli \
        local-run local-stop local-logs quickstart \
        kind-build-kubeintellect kind-deploy-kubeintellect kind-redeploy-kubeintellect \
        vm-deploy-kubeintellect aks-deploy-kubeintellect alibaba-deploy-kubeintellect \
        aws-deploy-kubeintellect gcp-deploy-kubeintellect _cloud-deploy \
        docs-serve docs-build docs-check \
        helm-package check-dist \
        scenarios help

IMAGE_NAME        ?= ghcr.io/mskazemi/kubeintellect
TAG               ?= dev-latest
KIND_CLUSTER_NAME ?= testbed-v2
NAMESPACE         ?= kubeintellect

# ═══════════════════════════════════════════════════════════════════════════════
##@ Help
# ═══════════════════════════════════════════════════════════════════════════════

help: ## Show all available targets
	@printf "\n\033[1mKubeIntellect V2 — Makefile Reference\033[0m\n"
	@printf "Defaults: KIND_CLUSTER_NAME=\033[33m$(KIND_CLUSTER_NAME)\033[0m  IMAGE=\033[33m$(IMAGE_NAME):$(TAG)\033[0m  NAMESPACE=\033[33m$(NAMESPACE)\033[0m\n"
	@printf "Override any default inline:  \033[36mmake <target> KIND_CLUSTER_NAME=mycluster\033[0m\n"
	@printf "\n\033[2mShared infra (Kind cluster, monitoring, Langfuse) is run from the repo root:\033[0m \033[36mcd .. && make help\033[0m\n"
	@printf "\n\033[1mLaptop (no K8s — docker compose)\033[0m\n"
	@printf "  \033[36mquickstart\033[0m               Interactive wizard — downloads + configures everything\n"
	@printf "                             \033[2mexample: make quickstart\033[0m\n"
	@printf "  \033[36mlocal-run\033[0m                Start app + Postgres via docker compose\n"
	@printf "                             \033[2mprereq: .env file exists (copy .env.example)\033[0m\n"
	@printf "                             \033[2mexample: make local-run\033[0m\n"
	@printf "  \033[36mlocal-stop\033[0m               Stop docker compose stack\n"
	@printf "  \033[36mlocal-logs\033[0m               Tail app logs from docker compose\n"
	@printf "\n\033[1mLocal K8s cluster (Kind)\033[0m\n"
	@printf "  \033[36mkind-build-kubeintellect\033[0m Build Docker image and load it into Kind (run after code changes)\n"
	@printf "                             \033[2mexample: make kind-build-kubeintellect\033[0m\n"
	@printf "                             \033[2mexample: make kind-build-kubeintellect TAG=my-branch\033[0m\n"
	@printf "\n\033[1mDeploy KubeIntellect\033[0m\n"
	@printf "  \033[36mkind-deploy-kubeintellect\033[0m     Deploy/upgrade on local Kind via Helm\n"
	@printf "                                   \033[2mprereq: .env with API keys, Kind cluster running\033[0m\n"
	@printf "                                   \033[2mexample: make kind-deploy-kubeintellect\033[0m\n"
	@printf "  \033[36mkind-redeploy-kubeintellect\033[0m   Uninstall + redeploy app only (Langfuse stays)\n"
	@printf "                                   \033[2mexample: make kind-redeploy-kubeintellect\033[0m\n"
	@printf "  \033[36mvm-deploy-kubeintellect\033[0m       Deploy to Kind-on-VM\n"
	@printf "                                   \033[2mprereq: .env + values-production.yaml\033[0m\n"
	@printf "  \033[36maks-deploy-kubeintellect\033[0m      Deploy to AKS / any cloud K8s\n"
	@printf "  \033[36malibaba-deploy-kubeintellect\033[0m  Deploy to Alibaba Cloud ACK (Qwen LLM)\n"
	@printf "  \033[36maws-deploy-kubeintellect\033[0m      Deploy to AWS EKS (any LLM provider)\n"
	@printf "                                   \033[2mprereq: .env + values-aws.yaml\033[0m\n"
	@printf "  \033[36mgcp-deploy-kubeintellect\033[0m      Deploy to Google Cloud GKE (any LLM provider)\n"
	@printf "                                   \033[2mprereq: .env + values-gcp.yaml\033[0m\n"
	@printf "                                   \033[2mprereq: .env + values-cloud.yaml\033[0m\n"
	@printf "\n\033[1mPython development (no cluster needed)\033[0m\n"
	@printf "  \033[36minstall\033[0m      Install Python deps via uv\n"
	@printf "                   \033[2mexample: make install\033[0m\n"
	@printf "  \033[36mrun\033[0m          Start API server on port 8000 (foreground)\n"
	@printf "                   \033[2mprereq: Postgres running\033[0m\n"
	@printf "  \033[36mrun-bg\033[0m       Start API server in background (logs → .server.log)\n"
	@printf "  \033[36mstop\033[0m         Stop background server\n"
	@printf "  \033[36mlogs\033[0m         Tail background server logs\n"
	@printf "  \033[36mdev\033[0m               Start with hot-reload\n"
	@printf "  \033[36mdev-postgres\033[0m      Start Postgres on localhost:5432 via docker compose\n"
	@printf "  \033[36mdev-postgres-stop\033[0m Stop that Postgres\n"
	@printf "  \033[36mdb-init\033[0m           Apply schema.sql to Postgres\n"
	@printf "  \033[36mdb-purge\033[0m          Reflexion retention purge (90d outcomes / 30d unverified patterns)\n"
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
	@printf "\n\033[2mShared infra (Kind cluster, monitoring, Langfuse, /etc/hosts) is provisioned from the repo root:\033[0m \033[36mcd .. && make help\033[0m\n"
	@printf "\n\033[36m[A] Fresh laptop — no K8s yet (local dev with Kind)\033[0m\n"
	@printf "    cd .. && make <cluster + monitoring + langfuse + hosts>   # see repo-root: make help\n"
	@printf "    make kind-build-kubeintellect         # build + load Docker image into Kind\n"
	@printf "    make kind-deploy-kubeintellect        # deploy KubeIntellect via Helm\n"
	@printf "\n\033[36m[B] Already have a K8s cluster with Prometheus + Loki\033[0m\n"
	@printf "    # Point config.prometheusUrl / config.lokiUrl at your existing services\n"
	@printf "    # in deploy/helm/kubeintellect/values-production.yaml, then:\n"
	@printf "    make vm-deploy-kubeintellect          # or: make aks-deploy-kubeintellect\n"
	@printf "\n\033[36m[C] Already have a K8s cluster — no Prometheus/Loki yet\033[0m\n"
	@printf "    cd .. && make <monitoring + langfuse>  # see repo-root: make help\n"
	@printf "    make vm-deploy-kubeintellect          # or: make aks-deploy-kubeintellect\n"
	@printf "\n\033[36m[D] Already have K8s + monitoring — just install KubeIntellect\033[0m\n"
	@printf "    make vm-deploy-kubeintellect          # or: make aks-deploy-kubeintellect\n"
	@printf "\n\033[36m[E] Update KubeIntellect after a code change\033[0m\n"
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

kind-build-kubeintellect: ## Build the KubeIntellect Docker image and load it into Kind — run after code changes
	uv lock
	docker build -t $(IMAGE_NAME):$(TAG) .
	kind load docker-image $(IMAGE_NAME):$(TAG) --name $(KIND_CLUSTER_NAME)

# ═══════════════════════════════════════════════════════════════════════════════
##@ Deploy KubeIntellect
# ═══════════════════════════════════════════════════════════════════════════════

kind-deploy-kubeintellect: ## Deploy (or upgrade) KubeIntellect on local Kind via Helm — sources .env for secrets
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
	    --set-string secrets.readonlyApiKeys="$${KUBEINTELLECT_READONLY_KEYS:-}" \
	    --set config.langfuseEnabled="$${LANGFUSE_ENABLED:-false}" \
	    --set-string secrets.langfusePublicKey="$${LANGFUSE_PUBLIC_KEY:-}" \
	    --set-string secrets.langfuseSecretKey="$${LANGFUSE_SECRET_KEY:-}"; \
	'

kind-redeploy-kubeintellect: ## Uninstall and redeploy KubeIntellect only (Langfuse stays running)
	helm uninstall kubeintellect -n $(NAMESPACE) || true
	@echo "Waiting for KubeIntellect pods to terminate..."
	@kubectl wait --for=delete pod -l app.kubernetes.io/instance=kubeintellect \
	  -n $(NAMESPACE) --timeout=60s 2>/dev/null || true
	$(MAKE) kind-deploy-kubeintellect

vm-deploy-kubeintellect: ## Deploy KubeIntellect to Kind-on-VM — needs .env + values-production.yaml
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
	    --set-string secrets.readonlyApiKeys="$${KUBEINTELLECT_READONLY_KEYS:-}" \
	    --set config.langfuseEnabled="$${LANGFUSE_ENABLED:-false}" \
	    --set-string secrets.langfusePublicKey="$${LANGFUSE_PUBLIC_KEY:-}" \
	    --set-string secrets.langfuseSecretKey="$${LANGFUSE_SECRET_KEY:-}"; \
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
	    --set-string secrets.openaiBaseUrl="$${OPENAI_BASE_URL:-}" \
	    --set-string secrets.openaiCoordinatorModel="$${OPENAI_COORDINATOR_MODEL:-gpt-4o}" \
	    --set-string secrets.openaiSubagentModel="$${OPENAI_SUBAGENT_MODEL:-gpt-4o-mini}" \
	    --set-string secrets.azureOpenaiApiKey="$${AZURE_OPENAI_API_KEY:-}" \
	    --set-string secrets.azureOpenaiEndpoint="$${AZURE_OPENAI_ENDPOINT:-}" \
	    --set-string secrets.azureCoordinatorDeployment="$${AZURE_COORDINATOR_DEPLOYMENT:-gpt-4o}" \
	    --set-string secrets.azureSubagentDeployment="$${AZURE_SUBAGENT_DEPLOYMENT:-gpt-4o-mini}" \
	    --set-string secrets.adminApiKeys="$${KUBEINTELLECT_ADMIN_KEYS:-}" \
	    --set-string secrets.operatorApiKeys="$${KUBEINTELLECT_OPERATOR_KEYS:-}" \
	    --set-string secrets.readonlyApiKeys="$${KUBEINTELLECT_READONLY_KEYS:-}" \
	    --set config.langfuseEnabled="$${LANGFUSE_ENABLED:-false}" \
	    --set-string secrets.langfusePublicKey="$${LANGFUSE_PUBLIC_KEY:-}" \
	    --set-string secrets.langfuseSecretKey="$${LANGFUSE_SECRET_KEY:-}"; \
	'

alibaba-deploy-kubeintellect: ## Deploy KubeIntellect to Alibaba Cloud ACK (Qwen LLM) — needs .env + values-alibaba.yaml
	@bash -c '\
	  set -euo pipefail; \
	  test -f .env || { echo "ERROR: .env not found"; exit 1; }; \
	  test -f deploy/helm/kubeintellect/values-alibaba.yaml || \
	    { echo "ERROR: copy deploy/helm/kubeintellect/values-alibaba.yaml.example → deploy/helm/kubeintellect/values-alibaba.yaml and fill in ACR image + RDS host"; exit 1; }; \
	  set -a && source .env && set +a; \
	  LLM_PROVIDER=$${LLM_PROVIDER:-qwen}; \
	  if [ "$$LLM_PROVIDER" != "qwen" ] && [ "$$LLM_PROVIDER" != "openai" ]; then \
	    echo "ERROR: Alibaba/Qwen deploy expects LLM_PROVIDER=qwen (or openai); got $$LLM_PROVIDER"; exit 1; fi; \
	  : "$${OPENAI_API_KEY:?ERROR: OPENAI_API_KEY (DashScope key) is not set in .env}"; \
	  OPENAI_BASE_URL=$${OPENAI_BASE_URL:-https://dashscope-intl.aliyuncs.com/compatible-mode/v1}; \
	  helm upgrade --install kubeintellect deploy/helm/kubeintellect \
	    -f deploy/helm/kubeintellect/values.yaml \
	    -f deploy/helm/kubeintellect/values-alibaba.yaml \
	    --namespace $(NAMESPACE) --create-namespace \
	    --set postgres.password="$${POSTGRES_PASSWORD:-}" \
	    --set-string config.llmProvider="$$LLM_PROVIDER" \
	    --set-string secrets.openaiApiKey="$${OPENAI_API_KEY}" \
	    --set-string secrets.openaiBaseUrl="$${OPENAI_BASE_URL}" \
	    --set-string secrets.openaiCoordinatorModel="$${OPENAI_COORDINATOR_MODEL:-qwen-max}" \
	    --set-string secrets.openaiSubagentModel="$${OPENAI_SUBAGENT_MODEL:-qwen-plus}" \
	    --set-string secrets.adminApiKeys="$${KUBEINTELLECT_ADMIN_KEYS:-}" \
	    --set-string secrets.operatorApiKeys="$${KUBEINTELLECT_OPERATOR_KEYS:-}" \
	    --set-string secrets.readonlyApiKeys="$${KUBEINTELLECT_READONLY_KEYS:-}" \
	    --set config.langfuseEnabled="$${LANGFUSE_ENABLED:-false}" \
	    --set-string secrets.langfusePublicKey="$${LANGFUSE_PUBLIC_KEY:-}" \
	    --set-string secrets.langfuseSecretKey="$${LANGFUSE_SECRET_KEY:-}"; \
	  echo "✓ deployed. Check: kubectl -n $(NAMESPACE) get pods,ingress"; \
	'

aws-deploy-kubeintellect: ## Deploy KubeIntellect to AWS EKS (any LLM provider) — needs .env + values-aws.yaml
	@$(MAKE) --no-print-directory _cloud-deploy CLOUD=aws

gcp-deploy-kubeintellect: ## Deploy KubeIntellect to Google Cloud GKE (any LLM provider) — needs .env + values-gcp.yaml
	@$(MAKE) --no-print-directory _cloud-deploy CLOUD=gcp

# Shared multi-cloud deploy recipe. CLOUD=aws|gcp selects the values-<CLOUD>.yaml
# overlay. LLM provider is decoupled from the cloud: azure | openai | qwen | anthropic.
_cloud-deploy:
	@bash -c '\
	  set -euo pipefail; \
	  test -f .env || { echo "ERROR: .env not found"; exit 1; }; \
	  test -f deploy/helm/kubeintellect/values-$(CLOUD).yaml || \
	    { echo "ERROR: copy deploy/helm/kubeintellect/values-$(CLOUD).yaml.example → deploy/helm/kubeintellect/values-$(CLOUD).yaml and fill in image + host + (optional) managed-DB url"; exit 1; }; \
	  set -a && source .env && set +a; \
	  LLM_PROVIDER=$${LLM_PROVIDER:-openai}; \
	  case "$$LLM_PROVIDER" in \
	    azure) : "$${AZURE_OPENAI_API_KEY:?ERROR: AZURE_OPENAI_API_KEY not set in .env}"; \
	           : "$${AZURE_OPENAI_ENDPOINT:?ERROR: AZURE_OPENAI_ENDPOINT not set in .env}";; \
	    anthropic) : "$${ANTHROPIC_API_KEY:?ERROR: ANTHROPIC_API_KEY not set in .env}";; \
	    qwen) : "$${OPENAI_API_KEY:?ERROR: OPENAI_API_KEY (DashScope key) not set in .env}"; \
	          OPENAI_BASE_URL=$${OPENAI_BASE_URL:-https://dashscope-intl.aliyuncs.com/compatible-mode/v1};; \
	    openai) : "$${OPENAI_API_KEY:?ERROR: OPENAI_API_KEY not set in .env}";; \
	    *) echo "ERROR: unknown LLM_PROVIDER=$$LLM_PROVIDER (want azure|openai|qwen|anthropic)"; exit 1;; \
	  esac; \
	  helm upgrade --install kubeintellect deploy/helm/kubeintellect \
	    -f deploy/helm/kubeintellect/values.yaml \
	    -f deploy/helm/kubeintellect/values-$(CLOUD).yaml \
	    --namespace $(NAMESPACE) --create-namespace \
	    --set postgres.password="$${POSTGRES_PASSWORD:-}" \
	    --set-string config.llmProvider="$$LLM_PROVIDER" \
	    --set-string secrets.openaiApiKey="$${OPENAI_API_KEY:-}" \
	    --set-string secrets.openaiBaseUrl="$${OPENAI_BASE_URL:-}" \
	    --set-string secrets.openaiCoordinatorModel="$${OPENAI_COORDINATOR_MODEL:-gpt-4o}" \
	    --set-string secrets.openaiSubagentModel="$${OPENAI_SUBAGENT_MODEL:-gpt-4o-mini}" \
	    --set-string secrets.azureOpenaiApiKey="$${AZURE_OPENAI_API_KEY:-}" \
	    --set-string secrets.azureOpenaiEndpoint="$${AZURE_OPENAI_ENDPOINT:-}" \
	    --set-string secrets.azureCoordinatorDeployment="$${AZURE_COORDINATOR_DEPLOYMENT:-gpt-4o}" \
	    --set-string secrets.azureSubagentDeployment="$${AZURE_SUBAGENT_DEPLOYMENT:-gpt-4o-mini}" \
	    --set-string secrets.anthropicApiKey="$${ANTHROPIC_API_KEY:-}" \
	    --set-string secrets.adminApiKeys="$${KUBEINTELLECT_ADMIN_KEYS:-}" \
	    --set-string secrets.operatorApiKeys="$${KUBEINTELLECT_OPERATOR_KEYS:-}" \
	    --set-string secrets.readonlyApiKeys="$${KUBEINTELLECT_READONLY_KEYS:-}" \
	    --set config.langfuseEnabled="$${LANGFUSE_ENABLED:-false}" \
	    --set-string secrets.langfusePublicKey="$${LANGFUSE_PUBLIC_KEY:-}" \
	    --set-string secrets.langfuseSecretKey="$${LANGFUSE_SECRET_KEY:-}"; \
	  echo "✓ deployed to $(CLOUD). Check: kubectl -n $(NAMESPACE) get pods,ingress"; \
	'

# ═══════════════════════════════════════════════════════════════════════════════
##@ Python development (local, no cluster needed)
# ═══════════════════════════════════════════════════════════════════════════════

install: ## Install Python dependencies
	uv sync

run: ## Start API server locally (port 8000) — requires a running Postgres
	uv run python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

run-bg: ## Start API server in background (logs → .server.log, PID → .server.pid)
	@uv run python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 \
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
	uv run python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

dev-postgres: ## Start only Postgres via docker compose (for use with make dev)
	docker compose up -d postgres
	@echo "Postgres ready on localhost:5432 — run 'make db-init' on first use, then 'make dev'"

dev-postgres-stop: ## Stop the docker compose Postgres
	docker compose stop postgres

db-init: ## Apply schema.sql to the configured Postgres database (uses docker compose postgres)
	@# ON_ERROR_STOP=1 + --single-transaction: without them psql exits 0 after a failed
	@# statement, so `make db-init` reported success on a schema that never landed.
	docker compose exec -T postgres psql \
	  -v ON_ERROR_STOP=1 --single-transaction \
	  -U $${POSTGRES_USER:-kubeintellect} \
	  -d $${POSTGRES_DB:-kubeintellect} \
	  -f /dev/stdin < packages/kubeintellect-server/app/db/schema.sql

db-purge: ## Run reflexion retention (delete old rca_outcomes / unverified patterns)
	docker compose exec -T postgres psql \
	  -U $${POSTGRES_USER:-kubeintellect} \
	  -d $${POSTGRES_DB:-kubeintellect} \
	  -c "SELECT * FROM reflexion_purge($${RETAIN_OUTCOMES_DAYS:-90}, $${RETAIN_PATTERNS_DAYS:-30});"

lint: ## Run ruff linter (every tracked Python root, as CI does) and the format check
	uv run ruff check packages/kubeintellect-server/app/ packages/ki-protocol/ packages/kube-q/ tests/ scripts/
	uv run ruff format --check packages/kubeintellect-server/app/ packages/ki-protocol/

typecheck: ## Run mypy over the whole workspace (blocking CI gate — must stay at zero errors)
	uv run mypy packages/kubeintellect-server/app packages/ki-protocol packages/kube-q/kube_q

test: ## Run the pytest test suite (server + kube-q workspace member)
	uv run python -m pytest tests/ -v
	cd packages/kube-q && uv run python -m pytest tests/ -v

test-memory: ## Run the memory-subsystem tests (episodes, KG, bi-temporal, promotion, importance, prospective, security, summaries)
	uv run python -m pytest tests/test_memory_hierarchy.py tests/test_kg.py tests/test_kg_bitemporal.py \
		tests/test_promotion.py tests/test_memory_importance.py tests/test_prospective.py \
		tests/test_memory_security.py tests/test_summaries.py -v

cli: ## Open the interactive REPL against the local Kind deployment
	KUBE_Q_URL=http://api.kubeintellect.local uv run kq

# ═══════════════════════════════════════════════════════════════════════════════
##@ Build & publish
# ═══════════════════════════════════════════════════════════════════════════════

helm-package: ## Package the KubeIntellect Helm chart (output: *.tgz)
	helm package deploy/helm/kubeintellect/

check-dist: ## Build wheel and verify it contains only app/ code — no docs, Helm, secrets
	@rm -rf dist/
	@uv build --wheel -q --package kubeintellect
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
	  packages/ && echo "OK — no secrets found"
	@echo ""
	@echo "── All checks passed ───────────────────────────────────────────"

# ═══════════════════════════════════════════════════════════════════════════════
##@ Documentation
# ═══════════════════════════════════════════════════════════════════════════════

docs-serve: ## Serve docs locally with live-reload (http://127.0.0.1:8001)
	uv run mkdocs serve --dev-addr 127.0.0.1:8001

docs-build: ## Build static docs site (output: site/)
	uv run mkdocs build

docs-check: ## Verify numbered doc claims (playbooks, detectors, providers, flags) match code
	uv run python scripts/check_doc_claims.py

docs-fix: ## Rewrite drifted numbered doc claims to match the code (adding a playbook moves 8 numbers)
	uv run python scripts/check_doc_claims.py --fix
