# === KubeIntellect Makefile ===
# Run "make help" for a full list of targets.
# Example: make kind-kubeintellect-clean-deploy
MAKEFLAGS += --no-print-directory
.DEFAULT_GOAL := help

# Variables
NAMESPACE ?= kubeintellect
IMAGE_NAME ?= kubeintellect
TAG ?= latest
KIND_CLUSTER_NAME ?= testbed
FORCE ?=

# Read .env for variables used in Kind targets (_print-access-summary, kind-dev-create-user).
# These are read at make invocation time — they do NOT override shell exports.
_ENV_FILE := $(wildcard .env)
DEV_USER_EMAIL    := $(if $(_ENV_FILE),$(shell grep '^DEV_USER_EMAIL=' $(_ENV_FILE) 2>/dev/null | head -1 | cut -d= -f2-),admin@kubeintellect.local)
DEV_USER_PASSWORD := $(if $(_ENV_FILE),$(shell grep '^DEV_USER_PASSWORD=' $(_ENV_FILE) 2>/dev/null | head -1 | cut -d= -f2-),changeme)
DEV_USER_EMAIL    := $(or $(DEV_USER_EMAIL),admin@kubeintellect.local)
DEV_USER_PASSWORD := $(or $(DEV_USER_PASSWORD),changeme)

KIND_SECRETS_FILE := charts/kubeintellect/values-kind-secrets.yaml

.PHONY: help \
  dev test-watch log-watch lint-helm-values \
  cli demo demo-debug demo-security demo-scale demo-reset \
  record-demo trim-demo play-demo play-raw upload-demo \
  kind-cluster-create kind-cluster-cleanup kind-start kind-stop \
  kind-generate-secrets \
  kind-build kind-kubeintellect-deploy kind-kubeintellect-clean-deploy \
  kind-dev-deploy kind-dev-restart kind-core-restart kind-dev-create-user \
  kind-langfuse-deploy restart kubeintellect-restart \
  install-metrics-server-kind install-metrics-server \
  install-prometheus-kind install-loki-kind install-event-exporter-kind install-observability-kind \
  port-forward-librechat port-forward-api port-forward-ingress port-forward-langfuse \
  port-forward-prometheus port-forward-grafana \
  get-ingress-ip configure-ingress \
  azure-deploy-all azure-destroy-all \
  azure-login azure-set-up-environment azure-validate-environment \
  azure-cluster-create azure-install-cert-manager \
  azure-kubeintellect-deploy azure-kubeintellect-cleanup azure-kubeintellect-fresh-deploy \
  azure-cluster-cleanup \
  n1-kubeintellect-deploy n1-clean \
  backup mongo-backup mongo-restore postgres-backup postgres-restore \
  runtime-tools-backup pvc-restore read-chats \
  migrate-registry-json _print-access-summary

# ====================== Local dev (no Kubernetes needed) ======================

# Start uvicorn locally with hot-reload. Picks up Python changes in ~1s.
# Requires .env to be populated (cp .env.example .env && fill in values).
dev:
	uv run python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Re-run pytest automatically on every Python file change in app/ or tests/.
# First run fires immediately so you see current state; subsequent runs on save.
# Pass TEST= to scope to a specific file:  make test-watch TEST=tests/test_foo.py
TEST ?= tests/
test-watch:
	bash scripts/dev/test-watch.sh $(TEST)

# Watch KubeIntellect logs after each user interaction and auto-analyze for
# bugs/improvements using Claude. Updates docs/ROADMAP.md automatically.
# Start once at the beginning of a dev session in a separate terminal.
log-watch:
	bash scripts/dev/log-watcher.sh

# ====================== Lint / validation ======================

# Check that values-kind-dev.yaml lists all apps from values-kind.yaml.
# Helm replaces arrays on merge, so a missing app silently disappears on dev deploys.
lint-helm-values:
	bash scripts/ops/lint-helm-values.sh

# ====================== Development shortcuts ======================
n1-clean:
	@echo "Backing up MongoDB..."
	$(MAKE) mongo-backup
	@echo "Backing up runtime tools..."
	$(MAKE) runtime-tools-backup
	@echo "Deleting namespace kubeintellect..."
	kubectl delete namespace kubeintellect || true
	@echo "Cleanup complete."

n1-kubeintellect-deploy:
	$(MAKE) install-metrics-server
	helm upgrade --install kubeintellect charts/kubeintellect \
		-n kubeintellect --create-namespace \
		-f charts/kubeintellect/values-n1.yaml \
		--wait --timeout 3m
	@echo "KubeIntellect deployment complete."
	$(MAKE) mongo-restore
	@echo "MongoDB restoration complete."
	

read-chats:
	@echo "Reading chats from MongoDB..."
	uv run python backups/read_chat_db_mongodb.py

# Port forwarding for remote cluster access
port-forward-librechat:
	@echo "Forwarding LibreChat service to localhost:3080"
	@echo "Access at: http://localhost:3080"
	kubectl -n kubeintellect port-forward svc/librechat 3080:3080

port-forward-api:
	@echo "Forwarding KubeIntellect API to localhost:8000"
	@echo "Access at: http://localhost:8000"
	kubectl -n kubeintellect port-forward svc/kubeintellect-core-service 8000:80

port-forward-ingress:
	@echo "Forwarding Ingress controller to localhost:8080"
	@echo "Access at: http://kubeintellect.chat.local:8080 (add to /etc/hosts)"
	kubectl -n ingress-nginx port-forward svc/ingress-nginx-controller 8080:80

# Fallback: direct pod port-forward (use only if ingress is not working).
# Normally access Langfuse via ingress at http://langfuse.local
port-forward-langfuse:
	@echo "Fallback: forwarding Langfuse directly to localhost:3000"
	@echo "Prefer ingress: http://langfuse.local (requires kind-langfuse-deploy)"
	kubectl -n kubeintellect port-forward svc/langfuse-web 3000:3000

cli:
	uv run kq --url http://kubeintellect.api.local

demo:
	@uv run kq --demo deploy --url http://kubeintellect.api.local

demo-debug:
	@uv run kq --demo debug --url http://kubeintellect.api.local

demo-security:
	@uv run kq --demo security --url http://kubeintellect.api.local

demo-scale:
	@uv run kq --demo scale --url http://kubeintellect.api.local

demo-hitl:
	@uv run kq --demo hitl --url http://kubeintellect.api.local

demo-reset:
	@echo "Clearing tool_registry table and generated tool files..."
	@kubectl exec -n $(NAMESPACE) deployments/postgres -- \
		psql -U kubeuser -d kubeintellectdb -c "DELETE FROM tool_registry;"
	@kubectl exec -n $(NAMESPACE) deployments/kubeintellect-core -- \
		sh -c "rm -f /mnt/runtime-tools/tools/gen_*.py"
	@echo "Done. Registry and PVC tool files cleared."

# ── Demo recording ────────────────────────────────────────────────────────────
# Records the deploy scenario. Edit SCENARIO to record a different one.
SCENARIO ?= deploy
CAST_RAW  ?= demo-$(SCENARIO)-raw.cast
CAST_OUT  ?= docs/demos/kubeintellect-demo-$(SCENARIO).cast

record-demo:
	@clear
	asciinema rec --idle-time-limit 3 --title "KubeIntellect — $(SCENARIO)" \
		--command "uv run kq --demo $(SCENARIO) --url http://kubeintellect.api.local" \
		$(CAST_RAW)
	@echo "Raw recording saved to $(CAST_RAW)"
	@echo "Run 'make trim-demo' to clean it up."

trim-demo:
	@read -p "Cut seconds from start [default 0]: " S; \
	uv run python scripts/demo/trim_cast.py $(CAST_RAW) $(CAST_OUT) \
	  --start $${S:-0} --max-idle 2
	@echo "Trimmed recording: $(CAST_OUT)"

play-demo:
	asciinema play $(CAST_OUT)

play-raw:
	asciinema play $(CAST_RAW)

upload-demo:
	asciinema upload $(CAST_OUT)

# Deploy Langfuse observability stack into the existing Kind cluster.
# Requires: kind-kubeintellect-deploy (or kind-kubeintellect-clean-deploy) was run first.
# Access via ingress at http://langfuse.local after deploy (~2 min to become healthy).
# User, org, project, and API keys are seeded automatically via LANGFUSE_INIT_* — no manual steps.
#   Login: admin@kubeintellect.local / langfuse-admin
kind-langfuse-deploy:
	@grep -qF 'langfuse.local' /etc/hosts || echo "127.0.0.1 langfuse.local" | sudo tee -a /etc/hosts
	helm upgrade --install kubeintellect charts/kubeintellect \
		-n kubeintellect --create-namespace \
		-f charts/kubeintellect/values-kind.yaml \
		--set langfuse.enabled=true \
		--server-side=true --force-conflicts \
		--wait --timeout 5m
	@echo "Langfuse deployed. Open http://langfuse.local (~2 min to become healthy)"
	@echo "  Login: admin@kubeintellect.local / langfuse-admin"
	@echo "  API keys already configured — KubeIntellect will connect automatically."

# Get ingress IP for remote access
get-ingress-ip:
	@echo "=== Ingress Controller Status ==="
	@echo "Service Type:"
	@kubectl get svc -n ingress-nginx ingress-nginx-controller -o jsonpath='{.spec.type}' 2>/dev/null && echo "" || echo "Not found"
	@echo "LoadBalancer IP (cloud):"
	@kubectl get svc -n ingress-nginx ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null && echo "" || echo "  None (not a cloud cluster)"
	@echo "ExternalIP (manual):"
	@kubectl get svc -n ingress-nginx ingress-nginx-controller -o jsonpath='{.spec.externalIPs[0]}' 2>/dev/null && echo "" || echo "  Not set"
	@echo "NodePort HTTP:"
	@kubectl get svc -n ingress-nginx ingress-nginx-controller -o jsonpath='{.spec.ports[?(@.name=="http")].nodePort}' 2>/dev/null && echo "" || echo "  Not configured"
	@echo "NodePort HTTPS:"
	@kubectl get svc -n ingress-nginx ingress-nginx-controller -o jsonpath='{.spec.ports[?(@.name=="https")].nodePort}' 2>/dev/null && echo "" || echo "  Not configured"
	@echo ""
	@echo "Node IP:"
	@kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null && echo "" || echo "  Not found"
	@echo ""
	@echo "💡 Run 'make configure-ingress' to set up remote access"

# Configure ingress for remote access
configure-ingress:
	bash scripts/dev/configure-ingress-access.sh

# Quick restart of the kubeintellect-core pod. Same as kind-core-restart.
# Only useful when the pod is wedged; not needed for Python changes (uvicorn --reload handles those).
# NOTE: This does NOT switch the cluster to dev mode — run make kind-dev-deploy first.
restart:
	kubectl rollout restart deployment kubeintellect-core -n kubeintellect
# ====================== kind ======================

kind-cluster-cleanup:
	KIND_CLUSTER_NAME=$(KIND_CLUSTER_NAME) bash scripts/kind/cleanup-kind-cluster.sh

kind-cluster-create:
	mkdir -p .local-data/postgres
	KIND_CLUSTER_NAME=$(KIND_CLUSTER_NAME) bash scripts/kind/create-kind-cluster.sh

# Build kubeintellect:dev image locally and load it into the Kind cluster.
# Run this once after cloning, and again whenever pyproject.toml/uv.lock changes.
kind-build:
	docker build -t kubeintellect:dev .
	kind load docker-image kubeintellect:dev --name $(KIND_CLUSTER_NAME)

# ====================== Observability ======================

# Install kube-prometheus-stack (Prometheus + Grafana + Alertmanager + kube-state-metrics + node-exporter).
# Release name "prometheus" matches the service names already wired into the Ingress
# (prometheus-kube-prometheus-prometheus:9090, prometheus-grafana:80).
# Loki data source is pre-configured in Grafana. Grafana default password: admin/admin.
install-prometheus-kind:
	@echo "Installing kube-prometheus-stack (Prometheus + Grafana) in kubeintellect namespace..."
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
	helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
		--namespace kubeintellect \
		--set grafana.adminPassword=admin \
		--set grafana.persistence.enabled=true \
		--set grafana.persistence.size=2Gi \
		--set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
		--set prometheus.prometheusSpec.retention=365d \
		--set prometheus.prometheusSpec.retentionSize=45GB \
		--set prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.accessModes[0]=ReadWriteOnce \
		--set prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.resources.requests.storage=50Gi \
		--set alertmanager.alertmanagerSpec.storage.volumeClaimTemplate.spec.accessModes[0]=ReadWriteOnce \
		--set alertmanager.alertmanagerSpec.storage.volumeClaimTemplate.spec.resources.requests.storage=2Gi \
		--set "grafana.additionalDataSources[0].name=Loki" \
		--set "grafana.additionalDataSources[0].type=loki" \
		--set "grafana.additionalDataSources[0].url=http://loki.kubeintellect.svc.cluster.local:3100" \
		--set "grafana.additionalDataSources[0].access=proxy" \
		--set "grafana.additionalDataSources[0].isDefault=false" \
		--wait --timeout 8m
	@echo "kube-prometheus-stack ready."
	@echo "  Prometheus: http://prometheus.local  (retention: 365d / 45GB)"
	@echo "  Grafana:    http://grafana.local  (admin / admin, persistent)"

# Install Loki + Promtail (log aggregation stack).
# Promtail runs as a DaemonSet and ships all pod logs (including KubeIntellect app logs
# and event-exporter events) to Loki automatically.
# Grafana is disabled here — use the one from install-prometheus-kind instead.
install-loki-kind:
	@echo "Installing Loki + Promtail in kubeintellect namespace..."
	helm repo add grafana https://grafana.github.io/helm-charts --force-update
	helm upgrade --install loki grafana/loki-stack \
		--namespace kubeintellect \
		--set grafana.enabled=false \
		--set prometheus.enabled=false \
		--set promtail.enabled=true \
		--set loki.persistence.enabled=true \
		--set loki.persistence.size=100Gi \
		--set loki.config.compactor.retention_enabled=true \
		--set loki.config.limits_config.retention_period=8760h \
		--wait --timeout 5m
	@echo "Loki + Promtail ready."
	@echo "  Loki in-cluster: http://loki.kubeintellect.svc.cluster.local:3100  (retention: 365d / 100Gi)"
	@echo "  Query logs in Grafana → Explore → Loki data source"

# Install kubernetes-event-exporter — persists K8s lifecycle events beyond the default 1h TTL.
# Events are written to stdout as JSON; Promtail ships them to Loki automatically.
# Query in Grafana: {namespace="kubeintellect", app="event-exporter"}
install-event-exporter-kind:
	@echo "Installing kubernetes-event-exporter..."
	kubectl apply -f k8s/observability/event-exporter.yaml
	@echo "kubernetes-event-exporter deployed."

# Install the full observability stack: Prometheus + Grafana + Loki + event-exporter.
# Run AFTER kind-kubeintellect-deploy (namespace must exist first).
# Prometheus goes first (installs CRDs required by ServiceMonitor); Loki and event-exporter
# are independent so they run in parallel to save ~1-2 minutes.
install-observability-kind:
	$(MAKE) install-prometheus-kind
	$(MAKE) -j2 install-loki-kind install-event-exporter-kind
	@echo ""
	@echo "=== Observability stack ready ==="
	@echo "  Prometheus: http://prometheus.local"
	@echo "  Grafana:    http://grafana.local  (admin / admin)"
	@echo "  Loki:       http://loki.kubeintellect.svc.cluster.local:3100 (in-cluster)"
	@echo ""
	@echo "Grafana dashboards to import:"
	@echo "  MongoDB:    https://grafana.com/grafana/dashboards/7353"
	@echo "  PostgreSQL: https://grafana.com/grafana/dashboards/9628"

# Port-forward shortcuts for observability UIs (use when ingress is not available)
port-forward-prometheus:
	@echo "Forwarding Prometheus to localhost:9090"
	kubectl -n kubeintellect port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090

port-forward-grafana:
	@echo "Forwarding Grafana to localhost:3001"
	@echo "Login: admin / admin"
	kubectl -n kubeintellect port-forward svc/prometheus-grafana 3001:80

# Install metrics-server into Kind (requires --kubelet-insecure-tls; Kind has no real TLS certs).
install-metrics-server-kind:
	@echo "Installing metrics-server for Kind (insecure TLS)..."
	helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ --force-update
	helm upgrade --install metrics-server metrics-server/metrics-server \
		--namespace kube-system \
		--set args="{--kubelet-insecure-tls}" \
		--wait --timeout 2m
	@echo "metrics-server ready."

# Install metrics-server into a standard cluster (N1 / bare Kubernetes).
# AKS already includes metrics-server as an addon — do not run there.
install-metrics-server:
	@echo "Installing metrics-server..."
	helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ --force-update
	helm upgrade --install metrics-server metrics-server/metrics-server \
		--namespace kube-system \
		--wait --timeout 2m
	@echo "metrics-server ready."

# Full deploy using the production values (pulls images from GHCR).
# Use for a clean baseline or when testing the exact image that will be released.
kind-kubeintellect-deploy:
	$(MAKE) kind-generate-secrets
	$(MAKE) install-metrics-server-kind
	helm upgrade --install kubeintellect charts/kubeintellect \
		-n kubeintellect --create-namespace \
		-f charts/kubeintellect/values-kind.yaml \
		-f $(KIND_SECRETS_FILE) \
		--server-side=true --force-conflicts \
		--wait --timeout 5m
	$(MAKE) kind-dev-create-user
	$(MAKE) _print-access-summary

# Full clean-slate deploy (production mode — uses GHCR image, NOT the local dev image).
# This is STEP 1. After this completes, run: make kind-dev-deploy  (to enable hot-reload).
# Destroys any existing cluster, recreates it, deploys all services + observability.
kind-kubeintellect-clean-deploy:
	$(MAKE) kind-generate-secrets
	KIND_CLUSTER_NAME=$(KIND_CLUSTER_NAME) bash scripts/kind/cleanup-kind-cluster.sh
	mkdir -p .local-data/postgres
	@grep -qF 'kubeintellect.chat.local' /etc/hosts || echo "127.0.0.1 kubeintellect.chat.local" | sudo tee -a /etc/hosts
	@grep -qF 'kubeintellect.api.local' /etc/hosts || echo "127.0.0.1 kubeintellect.api.local" | sudo tee -a /etc/hosts
	@grep -qF 'langfuse.local' /etc/hosts || echo "127.0.0.1 langfuse.local" | sudo tee -a /etc/hosts
	@grep -qF 'prometheus.local' /etc/hosts || echo "127.0.0.1 prometheus.local" | sudo tee -a /etc/hosts
	@grep -qF 'grafana.local' /etc/hosts || echo "127.0.0.1 grafana.local" | sudo tee -a /etc/hosts
	@grep -qF 'alertmanager.local' /etc/hosts || echo "127.0.0.1 alertmanager.local" | sudo tee -a /etc/hosts
	KIND_CLUSTER_NAME=$(KIND_CLUSTER_NAME) bash scripts/kind/create-kind-cluster.sh
	$(MAKE) install-metrics-server-kind
	# Install observability first so Prometheus CRDs (ServiceMonitor) exist before the kubeintellect chart is deployed.
	kubectl create namespace kubeintellect --dry-run=client -o yaml | kubectl apply -f -
	$(MAKE) install-observability-kind
	helm upgrade --install kubeintellect charts/kubeintellect \
		-n kubeintellect --create-namespace \
		-f charts/kubeintellect/values-kind.yaml \
		-f $(KIND_SECRETS_FILE) \
		--server-side=true --force-conflicts \
		--wait --timeout 20m
	$(MAKE) kind-dev-create-user
	$(MAKE) _print-access-summary

# Generate charts/kubeintellect/values-kind-secrets.yaml from .env.
# Skips if the file already exists (pass FORCE=--force to rotate secrets).
# Called automatically by all Kind deploy targets.
kind-generate-secrets:
	@bash scripts/dev/generate-kind-secrets.sh $(FORCE)

# Create the dev user in LibreChat (idempotent — safe to run multiple times).
# Automatically called by kind-kubeintellect-clean-deploy and kind-kubeintellect-deploy.
kind-dev-create-user:
	EMAIL="$(DEV_USER_EMAIL)" PASSWORD="$(DEV_USER_PASSWORD)" bash scripts/kind/create-dev-user.sh

# Print access summary at the end of every deploy.
_print-access-summary:
	@echo ""
	@echo "============================================================"
	@echo "  KubeIntellect — Deployment Complete"
	@echo "============================================================"
	@echo ""
	@echo "  Chat Interface (LibreChat)"
	@echo "    URL:      http://kubeintellect.chat.local"
	@echo "    Email:    $(DEV_USER_EMAIL)"
	@echo "    Password: $(DEV_USER_PASSWORD)"
	@echo ""
	@echo "  LLM Observability (Langfuse)"
	@echo "    URL:      http://langfuse.local  (~2 min to become healthy)"
	@echo "    Email:    admin@kubeintellect.local"
	@echo "    Password: langfuse-admin"
	@echo ""
	@echo "  Metrics & Dashboards (Grafana)"
	@echo "    URL:      http://grafana.local"
	@echo "    Username: admin"
	@echo "    Password: admin"
	@echo ""
	@echo "  Metrics (Prometheus)"
	@echo "    URL:      http://prometheus.local"
	@echo ""
	@echo "  Alerts (Alertmanager)"
	@echo "    URL:      http://alertmanager.local"
	@echo ""
	@echo "  KubeIntellect API"
	@echo "    URL:      http://kubeintellect.api.local"
	@echo ""
	@echo "============================================================"
	@echo ""

# STEP 2 — Build local dev image, load into Kind, switch deployment to dev mode.
# Dev mode: mounts host source tree into the pod at /app so uvicorn --reload picks up
# Python changes in ~2s without any image rebuild.
#
# Re-run kind-dev-deploy ONLY when pyproject.toml or uv.lock changes (new dependency).
# For all other changes see the daily dev loop below.
#
# Dev loop:
#   Python file changed         → nothing  (uvicorn --reload, ~2s)
#   Config / env var changed    → make kind-dev-restart
#   New Python dependency       → make kind-dev-deploy  (rebuilds image)
#   Full cluster reset          → make kind-kubeintellect-clean-deploy → make kind-dev-deploy
#
# Requires: make kind-kubeintellect-clean-deploy was run at least once to create the cluster.
kind-dev-deploy:
	$(MAKE) kind-generate-secrets
	$(MAKE) kind-build
	helm upgrade --install kubeintellect charts/kubeintellect \
		-n kubeintellect --create-namespace \
		-f charts/kubeintellect/values-kind.yaml \
		-f $(KIND_SECRETS_FILE) \
		-f charts/kubeintellect/values-kind-dev.yaml \
		--server-side=true --force-conflicts \
		--wait --timeout 5m
	$(MAKE) kind-dev-create-user

# Apply dev values (code mount + hot-reload) WITHOUT rebuilding the image.
# Use this when the kubeintellect:dev image is already loaded (i.e. kind-dev-deploy was
# run at least once) and you only changed Helm values or want a clean pod restart.
kind-dev-restart:
	$(MAKE) kind-generate-secrets
	helm upgrade --install kubeintellect charts/kubeintellect \
		-n kubeintellect --create-namespace \
		-f charts/kubeintellect/values-kind.yaml \
		-f $(KIND_SECRETS_FILE) \
		-f charts/kubeintellect/values-kind-dev.yaml \
		--server-side=true --force-conflicts \
		--wait --timeout 5m
	kubectl rollout restart deployment kubeintellect-core -n kubeintellect

# Restart only the KubeIntellect core pod (e.g. after a config/env change).
# Not needed for Python code changes — uvicorn --reload handles those automatically.
kind-core-restart:
	kubectl rollout restart deployment kubeintellect-core -n kubeintellect

# Start all Docker containers belonging to the Kind cluster.
kind-start:
	@echo "Starting Kind cluster containers ($(KIND_CLUSTER_NAME))..."
	docker ps -a --filter "label=io.x-k8s.kind.cluster=$(KIND_CLUSTER_NAME)" --format "{{.Names}}" \
		| xargs -r docker start
	@echo "Kind cluster containers started."

# Stop all Docker containers belonging to the Kind cluster.
kind-stop:
	@echo "Stopping Kind cluster containers ($(KIND_CLUSTER_NAME))..."
	docker ps --filter "label=io.x-k8s.kind.cluster=$(KIND_CLUSTER_NAME)" --format "{{.Names}}" \
		| xargs -r docker stop
	@echo "Kind cluster containers stopped."

kubeintellect-restart:
	kubectl rollout restart deployment kubeintellect-core -n kubeintellect
	kubectl rollout restart deployment librechat -n kubeintellect
	kubectl rollout restart deployment postgres -n kubeintellect

# ====================== Azure ======================

# --- Main Targets ---
azure-deploy-all: azure-login azure-set-up-environment azure-validate-environment azure-cluster-create azure-install-cert-manager azure-kubeintellect-deploy
	@echo "Azure deployment complete."

azure-destroy-all: azure-cluster-cleanup
	@echo "Azure resources destroyed."

# Use FORCE=1 to skip all confirmation prompts: make azure-destroy-all FORCE=1

# --- Individual Steps (in order) ---
# 1. Login to Azure
azure-login:
	cd infrastructure/azure && bash .mohsen.login.sh

# 2. Set up environment variables for AKS
azure-set-up-environment:
	cd infrastructure/azure && bash setup_aks_env.sh

# 3. Validate environment for deployment
azure-validate-environment:
	cd infrastructure/azure && bash validate-environment.sh

# 4. Create the AKS cluster
azure-cluster-create:
	cd infrastructure/azure && bash azure-cluster-create.sh

# 5. Install cert-manager (prerequisite for TLS — must run before Helm deploy)
azure-install-cert-manager:
	bash scripts/azure/setup-certificates.sh

# 6. Deploy KubeIntellect to the AKS cluster (ClusterIssuer + TLS ingresses included)
azure-kubeintellect-deploy:
	helm upgrade --install kubeintellect charts/kubeintellect \
		-n kubeintellect --create-namespace \
		-f charts/kubeintellect/values-azure.yaml \
		--wait --timeout 10m

azure-kubeintellect-cleanup:
	helm uninstall kubeintellect -n kubeintellect || true
	kubectl delete namespace kubeintellect || true
	
azure-kubeintellect-fresh-deploy: azure-kubeintellect-cleanup azure-kubeintellect-deploy
	@echo "KubeIntellect deployment complete."

# --- Cleanup ---
# Deletes the AKS cluster and associated resources.
# Pass FORCE=1 to skip all confirmation prompts (e.g. in CI):
#   make azure-cluster-cleanup FORCE=1
azure-cluster-cleanup:
	cd infrastructure/azure && bash azure-cluster-cleanup.sh $(if $(FORCE),--force,)



# ====================== PostgreSQL ======================

PG_BACKUP_FILE ?= backups/kubeintellect-pg-$(shell date +%Y%m%d-%H%M%S).dump

postgres-backup:
	mkdir -p backups
	kubectl exec -n kubeintellect deploy/postgres -- \
	  env PGPASSWORD=$$(kubectl get secret -n kubeintellect postgres-secret -o jsonpath='{.data.password}' | base64 -d) \
	  pg_dump -U kubeuser -d kubeintellectdb --format=custom --compress=9 > $(PG_BACKUP_FILE)
	@echo "PostgreSQL backup saved to $(PG_BACKUP_FILE)"

postgres-restore:
	@bash scripts/restore/postgres-restore.sh "$(FILE)"


# ====================== MongoDB ======================

BACKUP_FILE ?= backups/kubeintellect-chats-$(shell date +%Y%m%d-%H%M%S).gz

mongo-backup:
	mkdir -p backups
	kubectl exec -n kubeintellect deploy/mongodb -- \
	  mongodump --host mongodb.kubeintellect.svc.cluster.local --port 27017 \
	    --db LibreChat --archive --gzip > $(BACKUP_FILE)
	@echo "Backup saved to $(BACKUP_FILE)"

mongo-restore:
	@bash scripts/restore/mongo-restore.sh "$(FILE)"


backup: mongo-backup postgres-backup
	@echo "Backup saved to $(BACKUP_FILE)"
	bash ./backups/backup-pvc.sh kubeintellect-runtime-tools-pvc

# Backup runtime-tools PVC from kubeintellect-core pod
runtime-tools-backup:
	bash ./backups/backup-runtime-tools.sh

# Restore a PVC from a tar.gz backup
# Usage: make pvc-restore PVC=<pvc-name> FILE=<backup-file.tar.gz>
pvc-restore:
	@bash scripts/restore/pvc-restore.sh "$(PVC)" "$(FILE)"



migrate-registry-json:
	@echo "Importing registry.json → tool_registry Postgres table..."
	kubectl exec -n kubeintellect deploy/kubeintellect-core -- \
		uv run python scripts/migrations/002_import_registry_json.py


help:
	@echo ""
	@echo "╔══════════════════════════════════════════════════════════════════╗"
	@echo "║              KubeIntellect — make help                          ║"
	@echo "╚══════════════════════════════════════════════════════════════════╝"
	@echo ""
	@echo "Usage: make <target> [VARIABLE=value ...]"
	@echo ""
	@echo "Variables:"
	@echo "  KIND_CLUSTER_NAME  Kind cluster name         (default: testbed)"
	@echo "  NAMESPACE          Kubernetes namespace       (default: kubeintellect)"
	@echo "  TAG                Docker image tag           (default: latest)"
	@echo "  FORCE              Pass --force to rotate secrets (kind-generate-secrets)"
	@echo "  SCENARIO           Demo scenario name         (default: deploy)"
	@echo "  FILE               Backup file for restore targets"
	@echo "  PVC                PVC name for pvc-restore"
	@echo "  TEST               Test path for test-watch   (default: tests/)"
	@echo ""
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo " FIRST TIME SETUP — run these in order"
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo ""
	@echo "  Prerequisites: docker · kind · kubectl · helm · uv"
	@echo ""
	@echo "  1) Configure credentials"
	@echo "       cp .env.example .env"
	@echo "       # Set AZURE_OPENAI_API_KEY (or your LLM provider's key) in .env"
	@echo "       # Set AZURE_OPENAI_ENDPOINT in charts/kubeintellect/values-kind.yaml"
	@echo ""
	@echo "  2) Deploy the full Kind stack (creates cluster, installs all services)"
	@echo "       make kind-kubeintellect-clean-deploy      # ~8 min on first run"
	@echo ""
	@echo "  3) Switch to hot-reload dev mode (Python changes reflect in ~2s)"
	@echo "       make kind-dev-deploy"
	@echo ""
	@echo "  4) Open the chat interface"
	@echo "       http://kubeintellect.chat.local           # via Kind ingress"
	@echo "       make port-forward-librechat               # → http://localhost:3080"
	@echo ""
	@echo "  Access summary (URL, email, password) is printed after every deploy."
	@echo ""
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo " DAILY DEV LOOP"
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo ""
	@echo "  Python file changed          → nothing (uvicorn --reload, ~2s)"
	@echo "  .env / Helm values changed   → make kind-dev-restart"
	@echo "  New Python dependency        → make kind-dev-deploy   (rebuilds image)"
	@echo "  Full cluster reset           → make kind-kubeintellect-clean-deploy"
	@echo "                                 make kind-dev-deploy"
	@echo ""
	@echo "  Tail logs:  kubectl logs -f -n kubeintellect deploy/kubeintellect-core"
	@echo "  Auto-watch: make log-watch   (analyzes logs with Claude in a side terminal)"
	@echo ""
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo " KIND — Local Kubernetes cluster"
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo ""
	@echo "  Cluster lifecycle"
	@echo "    kind-cluster-create              Create cluster only (no app deploy)"
	@echo "    kind-cluster-cleanup             Destroy the cluster completely"
	@echo "    kind-start                       Start containers after host reboot"
	@echo "    kind-stop                        Stop containers (saves memory)"
	@echo ""
	@echo "  Deploy (production image from GHCR)"
	@echo "    kind-kubeintellect-clean-deploy  Destroy + recreate cluster, deploy everything."
	@echo "                                     Run once per fresh environment or cluster reset."
	@echo "    kind-kubeintellect-deploy        Deploy to existing cluster (no cluster recreate)."
	@echo "                                     Use to re-apply chart changes."
	@echo ""
	@echo "  Dev mode (run AFTER kind-kubeintellect-clean-deploy)"
	@echo "    kind-dev-deploy                  Build local image, load into Kind, enable hot-reload."
	@echo "                                     Re-run only when pyproject.toml/uv.lock changes."
	@echo "    kind-dev-restart                 Re-apply Helm values + restart pod."
	@echo "                                     Use after .env or config changes. No image rebuild."
	@echo "    kind-core-restart  (= restart)   Restart kubeintellect-core pod only."
	@echo "    kubeintellect-restart            Restart core + librechat + postgres pods."
	@echo ""
	@echo "  Credentials"
	@echo "    kind-generate-secrets            Read .env → generate values-kind-secrets.yaml."
	@echo "                                     Called automatically by all deploy targets."
	@echo "                                     FORCE=--force rotates LibreChat session secrets."
	@echo "    kind-dev-create-user             Create/update LibreChat dev user (idempotent)."
	@echo "                                     Called automatically after every deploy."
	@echo ""
	@echo "  Other"
	@echo "    kind-build                       Build kubeintellect:dev image + load into Kind."
	@echo "    kind-langfuse-deploy             Add Langfuse LLM tracing to existing cluster."
	@echo "                                     Login: admin@kubeintellect.local / langfuse-admin"
	@echo "    lint-helm-values                 Check values-kind-dev.yaml vs values-kind.yaml."
	@echo "    install-metrics-server-kind      Install metrics-server (Kind needs insecure TLS)."
	@echo ""
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo " OBSERVABILITY — Prometheus, Grafana, Loki, Langfuse"
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo ""
	@echo "  Ingress URLs (after kind-kubeintellect-clean-deploy)"
	@echo "    http://prometheus.local          Prometheus"
	@echo "    http://grafana.local             Grafana         (admin / admin)"
	@echo "    http://alertmanager.local        Alertmanager"
	@echo "    http://langfuse.local            Langfuse        (~2 min to start)"
	@echo ""
	@echo "  install-observability-kind         Full stack: Prometheus + Grafana + Loki + events"
	@echo "                                     (called automatically by clean-deploy)"
	@echo "  install-prometheus-kind            Prometheus + Grafana only"
	@echo "  install-loki-kind                  Loki + Promtail (log aggregation)"
	@echo "  install-event-exporter-kind        K8s event-exporter (persists events beyond 1h TTL)"
	@echo "  kind-langfuse-deploy               Langfuse LLM trace viewer"
	@echo ""
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo " PORT FORWARDING — when ingress URLs are not available"
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo ""
	@echo "  port-forward-librechat             LibreChat UI   → http://localhost:3080"
	@echo "  port-forward-api                   KubeIntellect  → http://localhost:8000"
	@echo "  port-forward-ingress               Ingress ctrl   → http://localhost:8080"
	@echo "  port-forward-prometheus            Prometheus     → http://localhost:9090"
	@echo "  port-forward-grafana               Grafana        → http://localhost:3001"
	@echo "  port-forward-langfuse              Langfuse       → http://localhost:3000 (fallback)"
	@echo "  get-ingress-ip                     Show ingress IP / NodePort for remote access"
	@echo "  configure-ingress                  Set up remote ingress access"
	@echo ""
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo " LOCAL DEV — without Kubernetes"
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo ""
	@echo "  dev                                uvicorn on :8000 with hot-reload"
	@echo "                                     Requires: cp .env.example .env && fill in values"
	@echo "  test-watch [TEST=tests/path]        Re-run pytest on every Python file change"
	@echo "  log-watch                          Watch pod logs + auto-analyze (side terminal)"
	@echo "  cli                                Interactive terminal chat (local source checkout)"
	@echo "                                     External users: pip install kube-q → kq --url <url>"
	@echo "                                     https://github.com/MSKazemi/kube_q"
	@echo ""
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo " CLI & DEMO RECORDING"
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo ""
	@echo "  Demo scenarios (run against live cluster at kubeintellect.api.local)"
	@echo "    demo                             Deploy scenario"
	@echo "    demo-debug                       CrashLoopBackOff / OOMKilled debug"
	@echo "    demo-security                    RBAC / privileged container audit"
	@echo "    demo-scale                       Scale + rollout"
	@echo "    demo-hitl                        HITL tool generation"
	@echo "    demo-reset                       Clear tool registry + generated files"
	@echo ""
	@echo "  Recording (asciinema)"
	@echo "    record-demo [SCENARIO=deploy]    Record → CAST_RAW"
	@echo "    trim-demo                        Trim raw → CAST_OUT (docs/demos/)"
	@echo "    play-demo / play-raw             Play recording"
	@echo "    upload-demo                      Upload to asciinema.org"
	@echo ""
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo " AZURE — Production AKS"
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo ""
	@echo "  One-shot"
	@echo "    azure-deploy-all                 login → env → validate → cluster → deploy"
	@echo "    azure-destroy-all                Tear down all Azure resources"
	@echo ""
	@echo "  Step by step"
	@echo "    azure-login                      (1) Log in to Azure"
	@echo "    azure-set-up-environment         (2) Export AKS environment variables"
	@echo "    azure-validate-environment       (3) Pre-flight check"
	@echo "    azure-cluster-create             (4) Create AKS cluster"
	@echo "    azure-install-cert-manager       (5) Install cert-manager (TLS prerequisite)"
	@echo "    azure-kubeintellect-deploy       (6) Deploy KubeIntellect to AKS"
	@echo ""
	@echo "  Maintenance"
	@echo "    azure-kubeintellect-fresh-deploy Uninstall + redeploy (keeps cluster)"
	@echo "    azure-kubeintellect-cleanup      Uninstall from AKS"
	@echo "    azure-cluster-cleanup            Delete AKS cluster  (pass FORCE=1 to skip confirm)"
	@echo ""
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo " BACKUP & RESTORE"
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo ""
	@echo "  backup                             MongoDB + PostgreSQL + runtime-tools PVC"
	@echo "  mongo-backup                       Backup LibreChat chat database"
	@echo "  mongo-restore [FILE=path/to/f]     Restore MongoDB (latest if FILE omitted)"
	@echo "  postgres-backup                    Backup agent state / checkpoint DB"
	@echo "  postgres-restore [FILE=path/to/f]  Restore PostgreSQL (latest if FILE omitted)"
	@echo "  runtime-tools-backup               Backup generated tool files from PVC"
	@echo "  pvc-restore PVC=<n> FILE=<f.gz>    Restore any PVC from a tar.gz backup"
	@echo "  read-chats                         Print chat history from MongoDB to stdout"
	@echo ""
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo " MAINTENANCE"
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo ""
	@echo "  migrate-registry-json              Import legacy registry.json → tool_registry table"
	@echo "  n1-kubeintellect-deploy            Deploy to bare N1 node"
	@echo "  n1-clean                           Backup + delete kubeintellect namespace on N1"
	@echo ""
	@echo "  Docs: https://github.com/MSKazemi/kubeintellect"
	@echo ""
