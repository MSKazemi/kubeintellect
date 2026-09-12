---
description: >-
  Deploy KubeIntellect and its demo cluster onto a single Hetzner Cloud VM —
  Kind, host nginx with Let's Encrypt, and the OpenAI provider — for about
  EUR 20 per month.
---
# Deploy to a single Hetzner Cloud VM

This is the smallest deployment that still answers real questions about a real
cluster. Everything runs on **one** server:

- **Compute** → one Hetzner Cloud CX43 (8 vCPU / 16 GB / 160 GB NVMe)
- **Cluster** → Kind, two nodes, ingress on host port `18080`
- **TLS** → nginx on the host with certbot, not in-cluster
- **LLM** → any OpenAI-compatible endpoint through the standard `openai` provider —
  `api.openai.com`, or **AWS Bedrock** (see below). No inference runs on this box.

It is the profile behind `api.kubeintellect.com`. If you want managed
Kubernetes instead, see [Cloud / VM (Helm)](cloud.md); the Helm values here are
the same chart with a different profile.

---

## 0. Prerequisites

- A Hetzner Cloud project and an SSH key uploaded to it.
- An OpenAI API key from <https://platform.openai.com/api-keys>.
- A domain whose DNS you control, pointed at the server once it is up.
- Local tools: `ssh`, and the KubeIntellect repo checked out on the server.

## 1. Pick the size honestly

The full stack is Kind (2 nodes) + ingress-nginx + kube-prometheus-stack +
loki-stack + the app + its Postgres. That fits in 16 GB. Two things do not, and
both are off in the shipped profile:

| | Why it is off |
|---|---|
| **Langfuse** | The Helm chart is six pods — postgres, clickhouse, redis, minio, web, worker — with roughly 36 Gi of PVCs and **no resource limits on any template**. ClickHouse expands into whatever the box has. Point `config.langfuseHost` at an instance elsewhere if you need tracing. |
| **Alertmanager** | Disabled in `deploy/kind/monitoring-values.yaml` along with the etcd/scheduler/controller-manager scrapers, which do not exist on a Kind control plane anyway. |

If you drop the observability stack entirely (`prometheusUrl` and `lokiUrl`
empty), the app still answers every kubectl-based question and a 4 GB CX23 is
enough — but metric and log questions then fail, loudly, by design.

## 2. Provision

Create the server (Ubuntu 24.04) and, before anything else, attach a firewall:

- **22/tcp** — your address only
- **80/tcp, 443/tcp** — open
- everything else — closed

Then install `docker`, `kind`, `kubectl`, `helm`, `nginx`, `certbot` and `uv`.

Set the reverse DNS record on the IPv4 address to your API hostname; some
mail and TLS tooling checks it.

## 3. Bring up the cluster

From the repo root on the server:

```bash
make kind-cluster-create-vm     # 2-node Kind, ingress on hostPort 18080/18443
make monitoring-install         # kube-prometheus-stack + loki-stack, `monitoring` ns
```

The VM Kind config deliberately maps ingress to `18080`/`18443` rather than
`80`/`443`, so host nginx can own the standard ports.

## 4. Configure the release

Copy the shipped profile and fill in the blanks:

```bash
cp v4/deploy/helm/kubeintellect/values-hetzner.yaml.example \
   v4/deploy/helm/kubeintellect/values-hetzner.yaml     # gitignored
```

The two settings that matter most on a public deployment:

```yaml
config:
  requireAuth: true
  allowedOrigins: "https://example.com,https://www.example.com"
```

!!! danger "`requireAuth` is not optional on a public ingress"
    With `requireAuth: false` and no API keys configured, the server resolves
    **every unauthenticated caller to `admin`** — HITL-gated, but with write
    access to the cluster. That is the documented fail-open default for local
    development, and it is wrong for anything reachable from the internet.

    The server refuses to start if `requireAuth` is true and no key tier is
    set, so the two cannot drift apart. Verify after deploying:

    ```bash
    curl -s -o /dev/null -w '%{http_code}\n' https://<your-api-host>/v1/namespaces
    # expect 401 — a 200 means the API is open
    ```

### Using AWS Bedrock instead of OpenAI

Bedrock speaks the OpenAI Chat Completions API, so it is a configuration change, not a code
one. In `values-hetzner.yaml`:

```yaml
config:
  llmProvider: openai
secrets:
  openaiBaseUrl: "https://bedrock-runtime.eu-central-1.amazonaws.com/openai/v1"
  openaiCoordinatorModel: eu.anthropic.claude-<version>
  openaiSubagentModel: eu.anthropic.claude-<smaller-version>
```

and pass the Bedrock API key as `secrets.openaiApiKey`. Choose a region near the server —
this box is in Falkenstein, so `eu-central-1` keeps the round trip inside Europe.

!!! warning "Two Bedrock traps that both fail at the first request"
    **1. Claude on-demand needs a cross-Region inference profile, not the bare model id.**
    Calling `anthropic.claude-...` directly in an EU Region fails with *"Invocation with
    on-demand throughput isn't supported"*. Use the profile id — the base id with the
    geography prefix, e.g. `eu.anthropic.claude-...`. Valid prefixes are `us`, `eu`,
    `apac`, `jp`, `au` and `global`.

    **2. That rules out the `bedrock-mantle` endpoint for this setup.** Cross-Region
    inference is supported on `bedrock-runtime` and *not* on `bedrock-mantle`. Since Claude
    on-demand requires a profile, use `bedrock-runtime`. Per-token pricing is identical
    between the two, so nothing is lost.

    Do not copy a version string from a guide — model ids move. List what your account
    actually has, which the OpenAI-compatible endpoint supports directly:

    ```bash
    curl -s "$OPENAI_BASE_URL/models" -H "Authorization: Bearer $OPENAI_API_KEY" \
      | python3 -c 'import json,sys; [print(m["id"]) for m in json.load(sys.stdin)["data"]]'
    ```

### Keeping Bedrock spend bounded

Bedrock bills per token. The exposure here is not the API — it is the **public `/kq`
terminal**, whose PTY websocket is unauthenticated by design and spawns a real `kq` process
per visitor, each of which costs tokens. Three controls, in order of how much they matter:

1. **The container's `KUBE_Q_API_KEY` must be the `readonly` key.** It bounds what a visitor
   can make the agent do, and therefore how much work it will perform.
2. **A CloudWatch billing alarm on the Bedrock service**, so spend is noticed the day it
   moves rather than at the invoice.
3. **`RATE_LIMIT_ENABLED`** (on by default, 120/min) caps request volume per caller on the
   API itself.

Prove it works **before** deploying. This exercises the real LLM factory, including the
function calling the subagents depend on:

```bash
cd v4 && uv run python scripts/verify_llm.py
```

A model that chats but emits no tool call produces an agent that answers confidently and
never reads the cluster, so treat that check as load-bearing rather than a formality.

## 5. Deploy

Keys are passed at install time so they never enter a values file:

```bash
cd v4
helm upgrade --install kubeintellect deploy/helm/kubeintellect \
  -f deploy/helm/kubeintellect/values.yaml \
  -f deploy/helm/kubeintellect/values-hetzner.yaml \
  --namespace kubeintellect --create-namespace \
  --set postgres.password="$POSTGRES_PASSWORD" \
  --set-string secrets.openaiApiKey="$OPENAI_API_KEY" \
  --set-string secrets.adminApiKeys="$KUBEINTELLECT_ADMIN_KEYS" \
  --set-string secrets.operatorApiKeys="$KUBEINTELLECT_OPERATOR_KEYS" \
  --set-string secrets.readonlyApiKeys="$KUBEINTELLECT_READONLY_KEYS"

make db-init      # schema.sql is idempotent
```

Pin a released image tag rather than `latest`, so you know what is running:

```bash
  --set-string image.tag="v2.4.1"
```

## 6. nginx and TLS

```bash
bash v4/scripts/vm/setup-nginx.sh    # reverse proxy → 127.0.0.1:18080
bash v4/scripts/vm/setup-tls.sh      # certbot --nginx, then rewrites the vhost
```

Edit the `server_name` in both scripts to your hostname first. Do not remove
the `proxy_buffering off` / `proxy_cache off` lines — SSE streaming stops
working the moment nginx buffers the response, and it fails as a hang rather
than an error.

Point DNS at the server **before** running the TLS script; certbot's HTTP-01
challenge needs the name to resolve.

## 7. Verify

Nothing counts as deployed until these pass:

```bash
curl -s https://<api-host>/healthz                        # status ok, expected version
curl -s -o /dev/null -w '%{http_code}\n' \
     https://<api-host>/v1/namespaces                     # 401, not 200
curl -s -H "Authorization: Bearer $KI_READONLY_KEY" \
     https://<api-host>/v1/namespaces                     # the real namespace list
```

Then ask the agent to scale a deployment and confirm it **stops for approval**
rather than acting, and ask it about a namespace that does not exist and
confirm it refuses rather than producing a plausible answer.

## 8. What this costs

Hetzner's June 2026 price adjustment raised the CPX and CCX lines far more than
CX and CAX, which makes the plain **CX** line the best value at this size — and
cheaper than the equivalent ARM CAX, so there is no reason to take on ARM.

Figures below are from Hetzner's own API for `fsn1`, not a price list. VAT is whatever
applies to your billing country — the gross column here is at 22%, so adjust for yours.

| Item | Net | Gross (22% VAT) |
|---|---|---|
| CX43 — 8 vCPU, 16 GB, 160 GB NVMe, 20 TB traffic | €15.99 | €19.51 |
| Primary IPv4 | €0.50 | €0.61 |
| **Total** | **€16.49** | **€20.12** |
| Backups (optional, +20% of the server) | €3.20 | €3.90 |

OpenAI tokens are billed separately. Set a monthly usage limit on the API key.

## Related

- [Cloud / VM (Helm)](cloud.md) — the generic managed-Kubernetes path
- [Container Image](image.md) — what is in the image and how it is built
- [Configuration](../configuration.md) — every setting and its default
