# KubeIntellect — Observability

KubeIntellect has two independent observability concerns that should not be conflated:

| Concern | Scope |
|---|---|
| **KubeIntellect app** | Is the app healthy? What are LLMs costing? Are agents routing correctly? |
| **Managed cluster** | Prometheus/Grafana for the target Kubernetes cluster's workloads — deployed as a prerequisite; KubeIntellect *queries* it |

This document covers observability of the KubeIntellect application itself. For managed-cluster observability setup, see [`docs/development.md`](development.md).

---

## Recommended Stack (priority order)

```
1. Langfuse          — LLM call traces, token cost, per-agent latency
2. Prometheus        — API health metrics, custom agent/tool counters
3. Grafana           — unified dashboard for all signals
4. Loki              — structured log aggregation (requires structured logging first)
5. DB exporters      — mongodb_exporter + postgres_exporter → same Prometheus
```

Grafana is the single pane of glass for everything. All five components integrate with it.

---

## Langfuse: LLM Observability

### Why this is the most critical gap

KubeIntellect has 11 agents (Supervisor + 10 workers), each making multiple LLM calls per workflow. A single RCA flow can chain 6–8 LLM calls. Without LLM tracing you are completely blind to:

- Which agent made which call and what it cost
- Where latency is coming from (supervisor routing, a worker agent, or a tool call)
- Why the supervisor routed to the wrong agent (bad prompt? ambiguous query?)
- Total token spend per query and per day

### Langfuse vs LangSmith

Both integrate via the same LangChain callback mechanism. The config in `app/core/config.py` already has `LANGCHAIN_API_KEY` / `LANGCHAIN_PROJECT` for LangSmith, but LangSmith is hosted SaaS. **Langfuse is the self-hostable open-source replacement** — same integration path, deployable in your own cluster.

### Integration points

```python
# app/core/llm_gateway.py — add to both get_supervisor_llm() and get_worker_llm()
from langfuse.callback import CallbackHandler

langfuse_handler = CallbackHandler(
    public_key=settings.LANGFUSE_PUBLIC_KEY,
    secret_key=settings.LANGFUSE_SECRET_KEY,
    host=settings.LANGFUSE_HOST,
)

# Pass as callback to every llm.invoke() / llm.stream() call
llm = llm.with_config(callbacks=[langfuse_handler])
```

New env vars to add to `app/core/config.py` and `.env.example`:
```
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=http://langfuse.kubeintellect.svc.cluster.local
LANGFUSE_ENABLED=true
```

### Key metrics to watch in Langfuse

| Signal | What it tells you |
|---|---|
| Token usage by agent | Which agents are prompt-heavy; where to optimize |
| Cost per query | Real cost of a single user interaction |
| LLM latency p95 by agent | Bottleneck identification |
| Supervisor routing distribution | How often each worker agent is invoked |
| Failed completions | Timeout, rate limit, or context-length errors |

### Deployment (self-hosted)

Langfuse is bundled directly in the KubeIntellect Helm chart. `langfuse.enabled: true` is already set in `charts/kubeintellect/values-kind.yaml` — it deploys automatically as part of the standard Kind cluster setup.

```bash
make kind-kubeintellect-clean-deploy   # Langfuse deploys as part of this — no extra step
# OR, to deploy/update Langfuse only into an existing cluster:
make kind-langfuse-deploy

# Access via ingress: http://langfuse.local  (~2 min to become healthy on first start)
# Fallback (if ingress isn't working):
make port-forward-langfuse   # → http://localhost:3000
```

**After a fresh cluster wipe**, Langfuse seeds itself automatically — no manual registration needed.

On first startup, the `LANGFUSE_INIT_*` env vars (injected via `langfuse-secret`) auto-create:
- Admin user: `admin@kubeintellect.local` / `langfuse-admin`
- Org: `KubeIntellect`
- Project: `KubeIntellect`
- API keys: fixed, matching the values already in `kubeintellect-core-secret`

KubeIntellect connects automatically — no key-copying or restart required.

The seeded keys and user credentials are configured in `charts/kubeintellect/values-kind.yaml` under `langfuse.initUser`, `langfuse.initOrg`, and `langfuse.initProject`.

The app env vars (`LANGFUSE_ENABLED=true`, `LANGFUSE_HOST`, keys) are already wired in the ConfigMap and Secret in `values-kind.yaml` — no `.env` changes needed for Kind.

The integration is wired in `app/core/llm_gateway.py` — all four LLM factory functions
(`get_supervisor_llm`, `get_worker_llm`, `get_code_gen_llm`, `get_llm_with_params`) attach the
Langfuse `CallbackHandler` when `LANGFUSE_ENABLED=true`. When disabled, behaviour is unchanged.

### langfuse Python package

The `langfuse` package (v4+) is in `pyproject.toml` and `uv.lock`. It is installed automatically
during `docker build`. The integration uses the v4 API:

```python
# app/core/llm_gateway.py
from langfuse import Langfuse                    # initialize OTel tracer with explicit credentials
from langfuse.langchain import CallbackHandler   # attach to LangChain LLMs (v4 import path)
```

> **Note:** In langfuse v3 the import was `from langfuse.callback import CallbackHandler` and the
> constructor accepted `secret_key`/`host` directly. In v4 both changed — the `Langfuse(...)` client
> is initialized once with credentials, then `CallbackHandler()` is called with no arguments.

---

## API Metrics (Prometheus)

### FastAPI instrumentation

```python
# app/main.py — add at startup
from prometheus_fastapi_instrumentator import Instrumentator
Instrumentator().instrument(app).expose(app)
```

This exposes `/metrics` with standard HTTP metrics (request count, latency histograms, error rate) automatically.

### Custom counters to add

Define in a new `app/utils/metrics.py`:

```python
from prometheus_client import Counter, Histogram

agent_invocations = Counter(
    "kubeintellect_agent_invocations_total",
    "Total agent invocations",
    ["agent"]
)
tool_calls = Counter(
    "kubeintellect_tool_calls_total",
    "Total tool calls",
    ["tool", "status"]  # status: success | error
)
workflow_duration = Histogram(
    "kubeintellect_workflow_duration_seconds",
    "End-to-end workflow duration",
    buckets=[1, 2, 5, 10, 30, 60]
)
hitl_decisions = Counter(
    "kubeintellect_hitl_decisions_total",
    "HITL approval/denial decisions",
    ["decision"]  # approved | denied
)
```

### Grafana dashboard panels

- Request rate + error rate (from `prometheus_fastapi_instrumentator`)
- Workflow duration p50/p95/p99
- Agent invocation heatmap (which agents are most used)
- HITL approval rate over time

---

## Structured Logging

This is a prerequisite for Loki. Currently KubeIntellect emits unstructured text logs.

### Target log format (JSON)

```json
{
  "timestamp": "2026-03-26T14:23:01.123Z",
  "level": "INFO",
  "logger": "app.orchestration.routing",
  "message": "Routing to logs_agent",
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "user_id": "user_abc",
  "agent": "logs_agent",
  "duration_ms": 142
}
```

### Implementation

```bash
uv add python-json-logger
```

```python
# app/utils/logger_config.py
import logging
from pythonjsonlogger import jsonlogger

def setup_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(jsonlogger.JsonFormatter(
        "%(timestamp)s %(level)s %(name)s %(message)s"
    ))
    logging.root.setLevel(logging.INFO)
    logging.root.addHandler(handler)
```

Inject `request_id` at the chat completions entry point and propagate through workflow state so all log lines for a single user query are correlated.

---

## Loki: Log Aggregation

> **Prerequisite:** Structured logging must be in place first — there is no value in shipping unstructured text to Loki.

Loki is lightweight and Grafana-native (unlike ELK). It covers two distinct log streams:

| Stream | Source | Purpose |
|---|---|---|
| KubeIntellect app logs | Structured JSON from `kubeintellect-core` pods | Debug routing, agent errors, HITL events |
| Managed cluster workload logs | All pods in managed namespaces | Historical log queries via `logs_agent` |

### Deployment

```bash
helm repo add grafana https://grafana.github.io/helm-charts
helm install loki grafana/loki-stack -n observability \
  --set fluent-bit.enabled=true \
  --set grafana.enabled=false  # use existing Grafana instance
```

Fluent Bit is deployed as a DaemonSet and ships logs to Loki automatically.

### KubeIntellect tool integration

Once Loki is deployed, add a `query_loki_logs` tool in `app/agents/tools/tools_lib/log_store_tools.py` so the `logs_agent` can query historical logs rather than only live K8s API logs.

---

## Database Monitoring

### MongoDB (LibreChat chat history)

Deploy `mongodb_exporter` and point Prometheus at it. Key signals:

| Metric | Alert threshold |
|---|---|
| `mongodb_connections_current` | > 80% of `maxIncomingConnections` |
| `mongodb_op_counters_total{type="query"}` | Sudden spike = LibreChat query issue |
| Storage size growth | > 80% of PVC capacity |
| Slow queries (>100ms) | Any pattern repeating |

Grafana community dashboard ID: **7353**

### PostgreSQL (HITL checkpoints)

Key signals:

| Metric | Alert threshold |
|---|---|
| `pg_stat_activity_count` | Approaching `POSTGRES_POOL_MAX_CONN` → HITL hangs |
| `pg_stat_user_tables_n_dead_tup{table="workflow_checkpoints"}` | High dead tuples → needs VACUUM |
| `pg_stat_user_tables_n_live_tup{table="workflow_checkpoints"}` | Row count growth → unbounded checkpoints |
| Query latency | p99 > 500ms |

Grafana community dashboard ID: **9628**

---

## Grafana: Unified Dashboard

All five signal sources (Langfuse, Prometheus, Loki, mongodb_exporter, postgres_exporter) integrate into Grafana. Use a single Grafana deployment in the `observability` namespace.

```bash
helm install grafana grafana/grafana -n observability \
  --set persistence.enabled=true \
  --set adminPassword=<from-secrets>
```

Configure data sources:
1. Prometheus → `http://prometheus-server.observability.svc.cluster.local`
2. Loki → `http://loki.observability.svc.cluster.local:3100`
3. Langfuse → native Langfuse UI (separate, not a Grafana data source)

---

## What NOT to do

- **Do not embed Prometheus/Loki inside KubeIntellect's Helm chart** — these are shared cluster infrastructure; deploy them independently in an `observability` namespace
- **Do not skip structured logging before deploying Loki** — unstructured logs in Loki are queryable but nearly useless
- **Do not use ELK instead of Loki** — ELK is 5–10× heavier; Loki is sufficient for this workload and integrates with Grafana natively
- **Do not rely on LangSmith for production** — it is SaaS and exports your prompts/completions to Anthropic/LangChain infrastructure; use self-hosted Langfuse

---

## Data Retention Policy (1-Year Minimum)

All observability components are configured for at least 365 days of persistent storage.

| Component | PVC Size | Retention Setting | Storage Rate |
|-----------|----------|-------------------|--------------|
| Prometheus TSDB | 50Gi (`retentionSize=45GB`) | `retention=365d` | ~80MB/day compressed |
| Alertmanager | 2Gi | N/A (silence history) | Negligible |
| Grafana | 2Gi | N/A (dashboards/settings) | Negligible |
| Loki | 100Gi | `retention_period=8760h` (365d) | ~200MB/day compressed |
| Langfuse ClickHouse | 30Gi | `LANGFUSE_RETENTION_DAYS=365` | ~50MB/day |
| Langfuse MinIO | 50Gi | `LANGFUSE_RETENTION_DAYS=365` | ~100MB/day trace payloads |
| Langfuse PostgreSQL | 10Gi | N/A (metadata only) | ~5MB/day |
| Langfuse Redis | 2Gi | N/A (ephemeral queue) | No long-term data |

**Implementation notes:**
- Prometheus: `retentionSize=45GB` is set 10% below the 50Gi PVC to prevent TSDB corruption from a full disk. Data is deleted oldest-first when the size limit is hit.
- Loki: `compactor.retention_enabled=true` is required for the compactor to enforce `retention_period`. Without it, chunks accumulate regardless of the period setting.
- Langfuse: `LANGFUSE_RETENTION_DAYS=365` is injected into both the `langfuse-web` and `langfuse-worker` containers. Langfuse uses this to TTL-expire old traces in ClickHouse via its built-in housekeeping jobs.
- All PVCs use the `Retain` reclaim policy on Azure (`managed-csi-retain`) so data survives pod and even release deletion.

**Resizing existing PVCs (if already deployed):**
- Azure (managed-csi): online resize is supported — patch the PVC `spec.resources.requests.storage` and restart the pod.
- Kind (hostPath): requires delete + recreate (data is lost; Kind clusters are ephemeral by nature).

---

## Deployment

```bash
# Full observability stack (run after kind-kubeintellect-clean-deploy):
make install-observability-kind

# Individual components:
make install-prometheus-kind    # Prometheus + Grafana + Alertmanager
make install-loki-kind          # Loki + Promtail
make install-event-exporter-kind  # kubernetes-event-exporter

# Access UIs:
# Prometheus: http://prometheus.local
# Grafana:    http://grafana.local  (admin / admin)
# Loki:       query via Grafana → Explore → Loki

# Port-forward fallbacks (if ingress not working):
make port-forward-prometheus    # → localhost:9090
make port-forward-grafana       # → localhost:3001
```

### After deploying Grafana — import dashboards
- MongoDB exporter: Import dashboard ID **7353** from grafana.com
- PostgreSQL exporter: Import dashboard ID **9628** from grafana.com

