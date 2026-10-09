# Local / self-hosted LLM

Run KubeIntellect against a model server you operate — [Ollama](https://ollama.com),
[vLLM](https://docs.vllm.ai), [LM Studio](https://lmstudio.ai), llama.cpp's `llama-server`,
or any other server that speaks the OpenAI chat-completions API. Prompts, and the cluster
data in them, then go to that server and nowhere else.

```bash
LLM_PROVIDER=local
```

`local` uses the same OpenAI-compatible client as `openai` and `qwen`, configured by the same
variables. What it adds:

- **No API key is required.** If `OPENAI_API_KEY` is unset, a placeholder is sent; set it only
  if your server checks one (for example vLLM started with `--api-key`).
- **The base URL is never empty.** It defaults to Ollama's `http://localhost:11434/v1`. An empty
  base URL would mean `api.openai.com`; with `LLM_PROVIDER=local` that can no longer happen by
  omission.
- **A startup check that refuses rather than pretends** — see
  [What is checked at startup](#what-is-checked-at-startup).

It works on both graphs: the default V2 graph, and the V4 cortex (`CORTEX_V4_ENABLED=true`).

## Quick start with Ollama

```bash
# 1. Install and start Ollama, then pull a model that supports tool calling.
ollama serve &
ollama pull qwen2.5:14b

# 2. Point KubeIntellect at it (~/.kubeintellect/.env, or the environment).
kubeintellect set LLM_PROVIDER=local
kubeintellect set OPENAI_BASE_URL=http://localhost:11434/v1     # the default; shown for clarity
kubeintellect set OPENAI_COORDINATOR_MODEL=qwen2.5:14b
kubeintellect set OPENAI_SUBAGENT_MODEL=qwen2.5:14b

# 3. Start the server. It probes the model before opening its port.
kubeintellect serve
```

The equivalent `.env` block:

```bash
LLM_PROVIDER=local
OPENAI_BASE_URL=http://localhost:11434/v1   # default for `local` (Ollama)
OPENAI_COORDINATOR_MODEL=qwen2.5:14b        # synthesis / large tier
OPENAI_SUBAGENT_MODEL=qwen2.5:14b           # subagents, triage and specialists / small tier
# OPENAI_API_KEY=                           # only if your server requires one
# LOCAL_LLM_PROBE_TIMEOUT_SECONDS=180       # per-model startup probe; raise for slow cold loads
```

The two model variables keep their OpenAI defaults (`gpt-4o`, `gpt-4o-mini`) unless you set
them. A local server does not serve those names, so the startup check will refuse until you
do — set both.

## Other servers

=== "vLLM"

    Tool calling must be enabled when the server starts; without it vLLM rejects every
    tool request, and KubeIntellect refuses to start.

    ```bash
    vllm serve Qwen/Qwen2.5-14B-Instruct \
      --enable-auto-tool-choice --tool-call-parser hermes

    LLM_PROVIDER=local
    OPENAI_BASE_URL=http://localhost:8000/v1
    OPENAI_COORDINATOR_MODEL=Qwen/Qwen2.5-14B-Instruct
    OPENAI_SUBAGENT_MODEL=Qwen/Qwen2.5-14B-Instruct
    ```

    The right `--tool-call-parser` depends on the model family; see the vLLM tool-calling
    documentation.

=== "LM Studio"

    Start the local server from LM Studio (Developer → Start server), then:

    ```bash
    LLM_PROVIDER=local
    OPENAI_BASE_URL=http://localhost:1234/v1
    OPENAI_COORDINATOR_MODEL=<model id shown by LM Studio>
    OPENAI_SUBAGENT_MODEL=<model id shown by LM Studio>
    ```

=== "llama.cpp"

    ```bash
    llama-server -m model.gguf --jinja --port 8080

    LLM_PROVIDER=local
    OPENAI_BASE_URL=http://localhost:8080/v1
    OPENAI_COORDINATOR_MODEL=<a model id listed by GET $OPENAI_BASE_URL/models>
    OPENAI_SUBAGENT_MODEL=<a model id listed by GET $OPENAI_BASE_URL/models>
    ```

## What is checked at startup

With `LLM_PROVIDER=local`, the server checks the endpoint **before it opens its port**, and
exits with status 1 and a message saying what to change if any check fails:

| Check | Fails when | Typical fix |
|---|---|---|
| Reachable | `GET {OPENAI_BASE_URL}/models` cannot connect | Start the server; fix `OPENAI_BASE_URL` (see [Containers](#containers-and-kubernetes)) |
| Authorised | the server answers 401 / 403 | Set `OPENAI_API_KEY` to the key the server expects |
| Model served | a configured model is not in the server's model list | `ollama pull <model>`, or set the model variable to a listed id |
| Tool calling | the server rejects a request that carries tools | Choose a model with tool support; for vLLM add `--enable-auto-tool-choice --tool-call-parser …` |
| Tool calling | the model accepts tools but answers in text | Choose a model with reliable tool calling (often a larger one) |
| Responsive | no answer within `LOCAL_LLM_PROBE_TIMEOUT_SECONDS` (default 180) | Raise the timeout, or use a smaller model |

The tool-calling probe sends one short request per distinct model — *"use the
`get_pod_status` tool to check pod `api-0` in namespace `prod`"* — through the same client
both graphs use. It contains no cluster data, and nothing is executed: the stub tool is never
run.

**Why a model that cannot call tools is refused.** KubeIntellect's agents read the cluster
only through tool calls (`kubectl`, PromQL, LogQL). A model that never emits one still
produces fluent answers — about a cluster it never looked at. That is the failure this
project treats as the worst one, so it is a startup error, not a warning. There is no
setting that skips the check.

If a server does not implement `GET /models`, the model-served check is skipped with a
warning and the tool-calling probe still runs.

After startup, an endpoint that goes away surfaces as a connection error on the request
that needed it; nothing substitutes an answer.

To run the same kind of check without starting the server:

```bash
cd v4
LLM_PROVIDER=local OPENAI_COORDINATOR_MODEL=qwen2.5:14b OPENAI_SUBAGENT_MODEL=qwen2.5:14b \
  uv run python scripts/verify_llm.py
```

## Containers and Kubernetes

`localhost` inside a container or a pod is that container, not the machine running Ollama.

- **Docker / Docker Compose:** use the host's address, e.g.
  `OPENAI_BASE_URL=http://host.docker.internal:11434/v1` (on Linux, add
  `--add-host=host.docker.internal:host-gateway`), or run the model server as another
  Compose service and use its service name.
- **Helm:** set `config.llmProvider: local` and `secrets.openaiBaseUrl` to the model
  server's in-cluster URL, e.g. `http://ollama.ollama.svc.cluster.local:11434/v1`, plus
  `secrets.openaiCoordinatorModel` / `secrets.openaiSubagentModel`. The
  `make aws-deploy-kubeintellect` / `gcp-deploy-kubeintellect` targets require
  `OPENAI_BASE_URL` in `.env` for `LLM_PROVIDER=local`.
- **Probe timing:** the chart's liveness probe allows roughly 105 seconds (15 s initial delay,
  then three failures 30 s apart), while the startup check may wait up to
  `LOCAL_LLM_PROBE_TIMEOUT_SECONDS` per model. With `config.llmProvider: local` the chart
  therefore adds a `startupProbe` on `/healthz` that holds liveness and readiness off until the
  server has opened its port. Its window is `config.localLlmProbeTimeoutSeconds` x
  `startupProbe.models` (default 2) + `startupProbe.marginSeconds` (default 120) — 480 s by
  default (`failureThreshold` 48 at a 10 s period). Set `startupProbe.models: 1` when both
  models are the same, and raise `config.localLlmProbeTimeoutSeconds` (not the probe) for a
  slower machine; the chart refuses an explicit `startupProbe.failureThreshold` that is too
  small. Other providers get no startup probe. Keeping the model loaded on the server (for
  Ollama, `OLLAMA_KEEP_ALIVE`) still makes restarts of the pod fast.
- **Fail-fast:** `helm template` / `helm install` fails if `local` is selected without
  `secrets.openaiBaseUrl`.

## Choosing a model

The model must support **tool / function calling** on your server — Ollama marks such models
with a *tools* tag in its library (for example `qwen2.5`, `llama3.1`, `mistral-nemo`). Beyond
that, quality scales with size: the agents plan multi-step investigations and read long tool
output, and small models are more likely to stop calling tools part-way. The startup check
proves a model *can* call a tool; it does not measure how well it diagnoses.

You can use one model for both variables, or a smaller one for `OPENAI_SUBAGENT_MODEL`:
it serves the parallel subagents on the V2 graph and the triage and specialist tiers on
the V4 cortex, while `OPENAI_COORDINATOR_MODEL` serves the coordinator (V2) and the final
synthesis (V4). See [V2 vs V4 (models)](v2-vs-v4-models.md).

## What stays local — and what does not

`LLM_PROVIDER=local` decides where **model requests** go. It does not turn off anything else
that receives data: Langfuse tracing (if enabled), the database and its backups, and any
other configured telemetry keep their own destinations. Whoever operates the model server
can read the prompts. See [Data handling](data-handling.md).

## Status of this support

The configuration, the client wiring and the startup check are covered by unit tests that
use stubbed endpoints. Answer quality depends entirely on the model you choose and is not
benchmarked by this project.
