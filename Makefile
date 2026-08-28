# ═══════════════════════════════════════════════════════════════════════════════
# KubeIntellect — SHARED INFRASTRUCTURE (root)
#
# This Makefile owns the cluster + observability stack that ALL versions share:
#   • Kind cluster (one cluster, default testbed-v2)
#   • Prometheus + Grafana + Loki  (monitoring namespace)
#   • Langfuse LLM tracing          (monitoring namespace)
#
# Per-version app build/deploy + Python dev live in vN/Makefile (cd v4 && make ...).
# All versions share ONE Langfuse project; each tags its traces with version:vN, so
# per-version cost is a tag filter (per-version projects need Langfuse Enterprise).
# ═══════════════════════════════════════════════════════════════════════════════
.PHONY: help setup check-modes fix-modes check-syntax check-encoding check-roster check-required kind-cluster-create kind-cluster-create-vm kind-cluster-stop kind-cluster-start \
        kind-cluster-cleanup monitoring-install monitoring-uninstall \
        langfuse-provision langfuse-install langfuse-clean hosts-entry helm-package \
        opsmembench opsmembench-demo opsmembench-driver-check opsmembench-test

KIND_CLUSTER_NAME ?= testbed-v2
MONITORING_NS     ?= monitoring

help: ## Show shared-infra targets
	@printf "\n\033[1mKubeIntellect — Shared Infrastructure (root)\033[0m\n"
	@printf "Defaults: KIND_CLUSTER_NAME=\033[33m$(KIND_CLUSTER_NAME)\033[0m  MONITORING_NS=\033[33m$(MONITORING_NS)\033[0m\n"
	@printf "Per-version app targets live in vN/Makefile — e.g. \033[36mcd v4 && make kind-deploy-kubeintellect\033[0m\n"
	@printf "\n\033[1mContributors — start here (no cluster needed)\033[0m\n"
	@printf "  \033[36msetup\033[0m                   Install the v4 workspace and run all nine CI gates\n"
	@printf "  \033[36mcheck-modes\033[0m             Check the CI file-mode gate (executable iff shebang)\n"
	@printf "  \033[36mfix-modes\033[0m               Fix any file-mode violations in place\n"
	@printf "  \033[36mcheck-syntax\033[0m            Check the CI syntax gate (no SyntaxWarning on 3.13)\n"
	@printf "  \033[36mcheck-encoding\033[0m           Check the CI encoding gate (every text-mode call names one)\n"
	@printf "  \033[36mcheck-roster\033[0m             Check the CI roster gate (both contributor lists agree)\n"
	@printf "  \033[36mcheck-public-checkout\033[0m    Run the nine gates against an export of HEAD, not your tree\n"
	@printf "\n\033[1mKind cluster (one cluster, shared by all versions)\033[0m\n"
	@printf "  \033[36mkind-cluster-create\033[0m      Create the shared Kind cluster (run once)\n"
	@printf "  \033[36mkind-cluster-create-vm\033[0m   Create Kind cluster on an Azure VM (run once on the VM)\n"
	@printf "  \033[36mkind-cluster-stop\033[0m        Pause cluster containers (state preserved)\n"
	@printf "  \033[36mkind-cluster-start\033[0m       Resume a stopped cluster\n"
	@printf "  \033[36mkind-cluster-cleanup\033[0m     Delete the cluster entirely (irreversible)\n"
	@printf "\n\033[1mObservability (Prometheus · Grafana · Loki · Langfuse)\033[0m\n"
	@printf "  \033[36mmonitoring-install\033[0m       Install Prometheus + Grafana + Loki into '$(MONITORING_NS)'\n"
	@printf "  \033[36mmonitoring-uninstall\033[0m     Remove Prometheus + Grafana + Loki\n"
	@printf "  \033[36mlangfuse-provision\033[0m       Auto-create the shared Langfuse project + token →\n"
	@printf "                             writes root .env AND fans keys into v2/v3/v4 .env (run once)\n"
	@printf "  \033[36mlangfuse-install\033[0m         Install Langfuse (seeds the project from root .env)\n"
	@printf "  \033[36mlangfuse-clean\033[0m           Uninstall Langfuse + wipe all PVCs (irreversible)\n"
	@printf "  \033[36mhosts-entry\033[0m              Add langfuse/loki/prometheus.local to /etc/hosts (sudo)\n"
	@printf "\n\033[1mFresh setup (laptop + Kind)\033[0m\n"
	@printf "    make kind-cluster-create\n"
	@printf "    make monitoring-install\n"
	@printf "    make langfuse-provision        # creates project + token, fans keys to all versions\n"
	@printf "    make langfuse-install\n"
	@printf "    make hosts-entry\n"
	@printf "    cd v4 && make kind-build-kubeintellect && make kind-deploy-kubeintellect\n\n"

# ── Kind cluster ──────────────────────────────────────────────────────────────
setup: ## Contributor setup — install the v4 workspace and run the nine locally-runnable CI gates (no cluster needed)
	@./scripts/dev-setup.sh

check-modes: ## Check the file-mode CI gate — a tracked file is executable iff it has a shebang
	@./scripts/check-file-modes.sh

fix-modes: ## Fix file-mode violations in place (stages the mode changes)
	@./scripts/check-file-modes.sh --fix

check-syntax: ## Check the syntax CI gate — no SyntaxWarning on the newest supported interpreter
	@./scripts/check-syntax-warnings.py

check-encoding: ## Check the encoding CI gate — every text-mode read/write names its encoding
	@./scripts/check-text-encoding.py

check-roster: ## Check the roster CI gate — .all-contributorsrc and the README table name the same people
	@./scripts/check-contributor-roster.py

check-required: ## Compare main's required checks against what ci.yml produces (needs an authed `gh`)
	@./scripts/check-required-checks.py

check-public-checkout: ## Run the nine gates against an export of HEAD — what a public clone carries, not your working tree
	@./scripts/check-public-checkout.sh

kind-cluster-create: ## Create the shared Kind cluster (2-node, hot-reload mounts) — run once
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

kind-cluster-cleanup: ## Delete the shared Kind cluster entirely
	kind delete cluster --name $(KIND_CLUSTER_NAME)

# ── Observability ─────────────────────────────────────────────────────────────
monitoring-install: ## Install Prometheus, Grafana, Loki + Promtail
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

langfuse-provision: ## Auto-create the shared Langfuse project + token → root .env + fan keys to v2/v3/v4 .env (run once)
	bash scripts/langfuse-provision.sh   # preserves existing LANGFUSE_HOST; pass --target compose|helm to set it

langfuse-install: ## Install Langfuse — seeds the shared project/keys from root .env (run langfuse-provision first)
	@kubectl create namespace $(MONITORING_NS) --dry-run=client -o yaml | kubectl apply -f -
	@bash -c '\
	  set -euo pipefail; \
	  test -f .env || { echo "ERROR: root .env not found — run: make langfuse-provision"; exit 1; }; \
	  set -a && source .env && set +a; \
	  if [ -z "$${LANGFUSE_PUBLIC_KEY:-}" ] || [ "$${LANGFUSE_SECRET_KEY:-}" = "sk-lf-change-me" ] || [ -z "$${LANGFUSE_SECRET_KEY:-}" ]; then \
	    echo "ERROR: Langfuse keys missing/placeholder in root .env — run: make langfuse-provision"; exit 1; \
	  fi; \
	  helm upgrade --install langfuse deploy/helm/langfuse \
	    -f deploy/helm/langfuse/values.yaml \
	    -f deploy/helm/langfuse/values-kind.yaml \
	    --namespace $(MONITORING_NS) --create-namespace \
	    --set-string initProject.publicKey="$${LANGFUSE_PUBLIC_KEY}" \
	    --set-string initProject.secretKey="$${LANGFUSE_SECRET_KEY}" \
	    --set-string initProject.id="$${LANGFUSE_INIT_PROJECT_ID:-kubeintellect-project}" \
	    --set-string initOrg.id="$${LANGFUSE_INIT_ORG_ID:-kubeintellect-org}" \
	    --set-string initUser.email="$${LANGFUSE_INIT_USER_EMAIL:-admin@kubeintellect.local}" \
	    --set-string initUser.password="$${LANGFUSE_INIT_USER_PASSWORD:-langfuse-admin}" \
	    --timeout 10m; \
	'

langfuse-clean: ## Uninstall Langfuse and wipe all trace data (PVCs) — irreversible
	helm uninstall langfuse -n $(MONITORING_NS) || true
	@kubectl delete pvc \
	  langfuse-clickhouse-pvc langfuse-minio-pvc langfuse-postgres-pvc langfuse-redis-pvc \
	  -n $(MONITORING_NS) --ignore-not-found
	@echo "Langfuse PVCs cleared."

hosts-entry: ## Add local dev hostnames to /etc/hosts — run once
	@grep -q "api.kubeintellect.local" /etc/hosts || \
	  echo "127.0.0.1 api.kubeintellect.local" | sudo tee -a /etc/hosts
	@grep -q "langfuse.local" /etc/hosts || \
	  echo "127.0.0.1 langfuse.local" | sudo tee -a /etc/hosts
	@grep -q "loki.local" /etc/hosts || \
	  echo "127.0.0.1 loki.local" | sudo tee -a /etc/hosts
	@grep -q "prometheus.local" /etc/hosts || \
	  echo "127.0.0.1 prometheus.local" | sudo tee -a /etc/hosts

helm-package: ## Package the shared Langfuse Helm chart (output: *.tgz)
	helm package deploy/helm/langfuse/

OPSMEMBENCH_TIMELINE ?= evaluation/opsmembench/timelines/oomkill-recurrence.yaml

opsmembench: ## Prove the OpsMemBench harness end-to-end (offline selftest — no cluster needed)
	uv run --project v4 python -m evaluation.opsmembench.runner selftest --timeline $(OPSMEMBENCH_TIMELINE)

OPSMEMBENCH_DEMO_TIMELINE ?= evaluation/opsmembench/timelines/fleet-multi-theme.yaml

opsmembench-demo: ## Print the OpsMemBench ablation table over built-in baselines (No-memory/V4-flat/V5-full)
	uv run --project v4 python -m evaluation.opsmembench.runner demo --timeline $(OPSMEMBENCH_DEMO_TIMELINE)

opsmembench-driver-check: ## Prove the OpsMemBench live driver's offline core (mock SUT → ceiling grade — no cluster)
	uv run --project v4 python -m evaluation.opsmembench.live_driver self-check --timeline $(OPSMEMBENCH_TIMELINE)

opsmembench-test: ## Run the OpsMemBench harness unit tests
	uv run --project v4 python -m pytest evaluation/opsmembench/test_opsmembench.py -q
