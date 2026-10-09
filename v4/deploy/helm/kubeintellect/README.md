# KubeIntellect Helm chart

An AI SRE for Kubernetes. It connects to your cluster, investigates with real
tools (kubectl, Prometheus, Loki), explains the root cause in plain English, and
executes a fix **only after a human approves it**.

Peer-reviewed: *KubeIntellect: A Modular LLM-Orchestrated Agent Framework for
End-to-End Kubernetes Management*, Seyedkazemi Ardebili & Bartolini,
**Journal of Grid Computing** 24(3), 2026, DOI `10.1007/s10723-026-09837-6`.

This chart deploys the **server**. The terminal client (`kq`) is installed
separately — `pipx install kube-q` or `uv tool install kube-q`.

## Install

```bash
helm install kubeintellect oci://ghcr.io/mskazemi/charts/kubeintellect \
  --namespace kubeintellect --create-namespace \
  --set config.llmProvider=openai \
  --set secrets.openaiApiKey=sk-... \
  --set postgres.password="$(openssl rand -hex 16)" \
  --set secrets.adminApiKeys="ki-admin-$(openssl rand -hex 12)"
```

Then point the CLI at it:

```bash
kubectl -n kubeintellect port-forward svc/kubeintellect 8000:8000
KUBE_Q_URL=http://localhost:8000 KUBE_Q_API_KEY=ki-admin-... kq
```

To pin a specific chart version, add `--version 2.2.0`. `helm show values
oci://ghcr.io/mskazemi/charts/kubeintellect` prints the full value list.

> **Keep `--namespace` and `namespace` in sync.** This chart writes an explicit
> `metadata.namespace` from `.Values.namespace` (default `kubeintellect`) rather
> than using the release namespace. Installing into a different namespace means
> setting both — `--namespace foo --create-namespace --set namespace=foo` —
> otherwise Helm creates `foo` and the objects still land in `kubeintellect`.

## Requirements

- Kubernetes 1.24+
- An API key for one LLM provider (below)
- A PostgreSQL database — the chart deploys one in-cluster by default, or set
  `postgres.external.enabled=true` and `postgres.external.url` to use RDS /
  Cloud SQL / Azure Database.

## LLM provider

Set `config.llmProvider` to `openai` (default), `azure`, `qwen`, `anthropic` or `local`,
then supply the matching secret. `anthropic` is only used by the V4 cortex path.

| Provider | Required values |
|---|---|
| `openai` | `secrets.openaiApiKey`; optionally `secrets.openaiBaseUrl` for any OpenAI-compatible endpoint |
| `azure` | `secrets.azureOpenaiApiKey`, `secrets.azureOpenaiEndpoint` |
| `qwen` | `secrets.openaiApiKey` + `secrets.openaiBaseUrl=https://dashscope-intl.aliyuncs.com/compatible-mode/v1` |
| `anthropic` | `secrets.anthropicApiKey` **and** `config.cortexV4Enabled=true` |
| `local` | `secrets.openaiBaseUrl` (the model server's in-cluster URL); no API key. See [Local LLM](../../../docs/local-llm.md) |

Never put these in a committed values file — pass them with `--set`, or use a
gitignored override file.

The chart refuses to render (`helm template` / `helm install` fails with an explanation) for
combinations the server would reject at startup: `anthropic` without `config.cortexV4Enabled`,
`local` without `secrets.openaiBaseUrl`, and `config.promqlDetectionEnabled` without
`config.prometheusUrl`.

With `llmProvider=local` the chart also emits a `startupProbe` on `/healthz` (no other provider
gets one). The server's startup check makes a real tool call per model before it opens its port,
and a cold model load can outlast the liveness window (~105 s). The probe window is
`config.localLlmProbeTimeoutSeconds` x `startupProbe.models` + `startupProbe.marginSeconds`
(default 180 x 2 + 120 = 480 s, i.e. `failureThreshold` 48 at a 10 s period). Set
`startupProbe.models=1` if the coordinator and subagent models are the same.

## Optional server features

All off (or at the server's default) unless set; each value maps 1:1 to the setting in
[Configuration](../../../docs/configuration.md).

| Value | Default | Setting |
|---|---|---|
| `config.selfGovernEnabled` | `false` | `SELF_GOVERN_ENABLED` — needs Postgres schema v3 (`effect_log`); the db-init Job applies it |
| `config.promqlDetectionEnabled` | `false` | `PROMQL_DETECTION_ENABLED` — needs `config.prometheusUrl` |
| `config.promqlDetectionIntervalSeconds` | `30` | `PROMQL_DETECTION_INTERVAL_SECONDS` |
| `config.postmortemMinGrounding` | `0.9` | `POSTMORTEM_MIN_GROUNDING` (0.0–1.0) |
| `config.localLlmProbeTimeoutSeconds` | `180` | `LOCAL_LLM_PROBE_TIMEOUT_SECONDS` |

## Safety model

The chart ships closed by default, and the two controls are independent:

- **RBAC** (`rbac.*`) decides what the agent's ServiceAccount *can* do.
  `createClusterReadOnly` is on; `createClusterOps` (delete/patch/scale),
  `allowExec` (pods/exec) and `clusterAdmin` are all **off**. The read-only
  grants are not hand-maintained: `tests/test_rbac_covers_the_playbooks.py`
  derives them from what the shipped playbooks actually read, so a playbook
  cannot ship a step the role would answer with `Forbidden`. **Secrets are
  deliberately excluded** — `get` on a Secret returns its values, and RBAC
  cannot grant key-only read.
- **Human-in-the-loop** gates every mutating action at request time, regardless
  of RBAC. Three API-key tiers: `adminApiKeys` (high + medium risk),
  `operatorApiKeys` (medium only — delete/drain blocked), `readonlyApiKeys`
  (writes always rejected). Leaving all three empty disables auth entirely, so
  set at least one on any cluster you care about.

`config.blockedNamespaces` and `config.blockedResources` are hard denies applied
before either of the above — by default the agent can never read Secrets or
ServiceAccounts, or touch `kube-system`.

## Commonly changed values

| Key | Default | Purpose |
|---|---|---|
| `image.tag` | `""` | Empty means the chart's `appVersion`. Pin only to run a different image. |
| `replicaCount` | `1` | Server replicas. |
| `drainSeconds` | `5` | preStop sleep that drains a rolling update. This, not the readiness probe, is what stops requests being dropped — see `app/core/readiness.py`. `0` disables it. |
| `terminationGracePeriodSeconds` | `45` | Total shutdown budget. **Must be at least `drainSeconds` + 15**, and the chart refuses to render otherwise: the preStop sleep is spent inside this budget, so a smaller value means Kubernetes SIGKILLs the pod mid-drain and drops exactly the in-flight requests the hook exists to protect. The remaining 15s is for those requests and for closing the Postgres pools. |
| `service.type` / `service.port` | `ClusterIP` / `8000` | How the API is exposed. |
| `ingress.enabled` | `false` | Ingress with `ingress.className`, `hosts`, `tls`. |
| `config.prometheusUrl` / `config.lokiUrl` | `""` | Enables metric and log questions. Empty still allows kubectl-based work. |
| `config.autonomyLevel` | `A1` | Autonomy ladder. A1 investigates but never auto-fixes. |
| `config.cortexV4Enabled` | `false` | The V4 tiered reasoning graph instead of the V2 coordinator. |
| `postgres.external.enabled` | `false` | Use a managed database instead of the in-cluster StatefulSet. |
| `resources` | 250m/512Mi → 1/1Gi | Requests and limits. |

Observability services are expected in the `monitoring` namespace, e.g.
`http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090`.

## Links

- Source: <https://github.com/MSKazemi/kubeintellect>
- Documentation: <https://mskazemi.github.io/kubeintellect/>
- Questions: <https://github.com/MSKazemi/kubeintellect/discussions>

