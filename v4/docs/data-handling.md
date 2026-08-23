# Data handling in KubeIntellect v4

Read this before connecting KubeIntellect to a production cluster. The server
is the trust boundary: it can read cluster state, put selected results into an
LLM request, and persist several kinds of operational history. This page
describes the current v4 implementation. It is not a promise that arbitrary
application output is safe to send to a provider.

## The short version

- The model can receive the user's request, the v4 system prompt, cluster
  snapshots, and results returned by the tools.
- `kubectl` output is passed through with size limits, not a general-purpose
  secret scrubber. Prometheus results include series labels and values. Loki
  results include stream labels and log lines; application logs are the
  highest-risk input because an application can print arbitrary content.
- With the default resource blocklist, `Secret` and `ServiceAccount` resources
  are rejected by the kubectl tool before kubectl is called. This check also
  applies to `superadmin`; that role only bypasses the infrastructure-namespace
  block.
- The redactor is primarily a storage safeguard. It is not an egress filter for
  the live prompt, tool results, or telemetry callbacks.
- Langfuse tracing is off by default. When enabled and configured, treat the
  configured Langfuse host as an additional recipient of trace data.

## What can reach an LLM provider

The coordinator builds a request from the system prompt, the current
conversation, memory context, and the current cluster snapshot. Tool calls can
add more context before a response is produced. Depending on which tools are
used, that context can contain:

| Source | Examples of data returned to the agent | Important boundary |
|---|---|---|
| `run_kubectl` | Resource names, images, namespaces, conditions, events, descriptions, and `kubectl logs` output | Output is capped, but is not generally redacted before it becomes model context. Namespace listings have a separate blocked-namespace filter. |
| `query_prometheus` | PromQL text, metric labels, current values, min/average/max values for range results, and the single value of a `scalar`/`string` result | No secret redaction is applied to labels or values. |
| `query_loki` | LogQL text, stream labels, timestamps, and log lines | Treat every returned log line as potentially sensitive or instruction-like. Loki is optional and returns an error when `LOKI_URL` is unset. |
| Conversation and memory | User text, remembered preferences, session notes, failure hints, recalled episodes, and recent KG changes | These are assembled into the live prompt when the corresponding memory features are enabled. |

The tool and prompt paths do apply size limits (`kubectl` 8,000 characters;
Prometheus and Loki 6,000 characters; coordinator tool messages 2,000
characters). A size limit controls volume, not sensitivity, and does not make
the remaining text trustworthy.

The server sends the resulting LLM request to the configured provider. If
`PROMETHEUS_URL` or `LOKI_URL` points outside the cluster, the server also sends
queries to those configured endpoints; the returned data then follows the
model-context path above.

## What the kubectl tool blocks

The default configuration is:

```text
KUBECTL_BLOCKED_RESOURCES=secret,secrets,serviceaccount,serviceaccounts
KUBECTL_BLOCKED_NAMESPACES=kubeintellect,monitoring,kube-system,kube-public,kube-node-lease,ingress-nginx,cert-manager
```

For a resource or verb that the tool classifies, `_check_protected_access`
rejects a blocked resource before executing kubectl and before presenting an
approval prompt. The resource check is independent of the requester's role,
including `superadmin`. The namespace check also protects infrastructure
namespaces. `superadmin` bypasses that namespace block **entirely — including
reads** of infrastructure namespaces, not only writes: the bypass clears the
namespace error for any verb whenever the target resource is not itself
blocklisted. It never clears the resource block.

This is a structural tool-layer policy for the configured blocklist, not a
claim that every possible cluster string is harmless. In particular:

- A Secret object and a ServiceAccount resource cannot be requested through
  `run_kubectl` — **and no configuration can change that.** These four types
  (`secret`, `secrets`, `serviceaccount`, `serviceaccounts`) are re-added to the
  blocklist unconditionally by the server, because `KUBECTL_BLOCKED_RESOURCES`
  *replaces* the list rather than extending it: before the floor existed, an
  operator adding `configmap` silently removed `secret`.
- An application can still print a credential into its own logs. `kubectl logs`
  and Loki are data sources, not Secret-object reads, so review and restrict
  log access separately.
- Changing the blocklist changes this policy. Keep the defaults in production
  unless you have reviewed the consequences.

## What is persisted

The normal v4 deployment uses the KubeIntellect PostgreSQL database. Local
SQLite mode stores the LangGraph checkpoint at `SQLITE_PATH`, but the separate
PostgreSQL memory store and flight recorder are disabled or unavailable in that
mode.

| Store | Backend and contents | Redaction and retention in the current code |
|---|---|---|
| LangGraph checkpoint | PostgreSQL by default, or the SQLite checkpoint file in local mode. It stores graph state for a session, including conversation messages and tool results needed to resume a thread. | The checkpoint path does not call `redact_secrets`. No application TTL is defined here; manage database/file retention yourself. |
| L1 episodes | PostgreSQL `episodes` rows: cluster and namespace identifiers, trigger detail, summary, root cause, actions, outcome, confidence, and timestamps. | `trigger_detail`, `summary`, and `root_cause` go through the heuristic redactor with field caps. The coordinator prepares mutation action payloads with the same redactor before writing them, but the generic episode writer only caps the serialized `actions` field; do not treat arbitrary nested action data as independently scrubbed. No episode TTL is defined in the schema. |
| L2 temporal knowledge graph | PostgreSQL `kg_entities` and `kg_edges`: cluster-scoped entity names, namespaces, relationships, attributes, source identifiers, and valid/ingested timestamps. | The KG write path has no call to the generic secret redactor. Treat names, labels, and attributes as operational data that still need least-privilege and database protection. No KG TTL is defined in the schema. |
| Flight recorder | PostgreSQL `decision_log`: non-token typed events, tool/action metadata, and hash-chain fields. Token frames are skipped. | When `REFLEXION_REDACT_SECRETS=true` (the default), **every string in the payload** is redacted, at any nesting depth (walked to a bound of 6 levels). Until 2026-08-20 the scrubber iterated only the top level, so a string inside a list or a nested object — a `plan`'s steps, an OTel span's attributes — was persisted verbatim; which payloads were covered was an accident of how each call site happened to shape its dict. The 1,500-char cap still applies to top-level fields only: a nested string arrives already capped by whoever produced it (a rollback pre-state at 4,000 chars), and re-capping it here would cost that capture its restorability. The redactor itself remains heuristic — see below. No automatic flight-recorder retention period is defined. |
| Preferences | PostgreSQL `user_prefs` rows contain explicit or inferred operator preferences. | Preference values are redacted and capped at 300 characters. Inferred preferences can be excluded after the configured decay window (default 60 days); explicit preferences are not purged by that policy. |
| Session notes and reflexion | PostgreSQL `session_notes`, `rca_outcomes`, and `failure_patterns` can contain session notes, RCA text, pattern descriptions, and recommended fixes. | The current session-note and RCA write paths do not apply the generic redactor; failure patterns seeded from RCA text should be treated the same way. A database helper can purge RCA outcomes after 90 days and low-confidence patterns after 30 days, but the schema does not establish that a scheduler calls it. No session-note TTL is defined. |
| Request audit | PostgreSQL `request_log`: request/session/user identifiers, role, route, method, status, duration, and timestamp. | The schema does not store prompt or tool-result content in this table. No application TTL is defined. |

The database schema also contains derived memory such as semantic rules,
prospective memory, summaries, fleet memory, and the memory audit chain. There
is no single redaction pass across those tables, so treat their text and JSON
attributes as operational data as well. A database backup, replica, log
exporter, or disk snapshot is another copy outside the application-level
lifecycle described above.

## What the redactor does — and does not do

`app/utils/redact.py` is deliberately heuristic. It keeps the **key** of a
credential-bearing field and replaces its **value**, masks token-shaped strings
and PEM armour, replaces URL hosts, and caps the result. Its own design notes
say that a full enumeration of secret formats is impossible.

Redaction is line-*aware*, not line-*wise*, and that distinction is the whole
point: YAML puts the name of a thing and its value on different lines. Until
2026-08-20 each line was classified alone and any line holding a keyword was
dropped, so a Deployment's `env:` block stored as

```yaml
# <redacted-line>          <- the name DB_PASSWORD, deleted
  value: hunter2-prod-db   <- the credential, kept
```

The credential survived and the word "password" did not, which also defeated the
review procedure the module prescribes (grep the store for what the heuristics
missed). A `tls.key: |` block scalar leaked its whole PEM body for the same
reason. The rule is now the other way round: keep the label, drop the payload,
and follow a credential key onto the lines its value actually occupies.

Everything above is line-shape work, and until 2026-08-20 the only line shape it knew was
YAML's. `kubectl -o json` — an equally ordinary read, and the form the API itself returns —
writes the same object with quoted keys, so no line matched at all and the credential was
stored verbatim:

```text
kubectl get pod    -o yaml  ->   value: <redacted>
kubectl get pod    -o json  ->   "value": "hunter2-prod-db"          <- stored
kubectl get secret -o yaml  ->   tls.key: <redacted>
kubectl get secret -o json  ->   "tls.key": "LS0tLS1CRUdJTiBSU0E="   <- stored
```

Whether a credential was caught depended on the `-o` flag the caller happened to pass. Quoted
keys are now parsed (the quote is back-referenced, so an opening quote requires a closing one)
and the value's own punctuation is preserved, which means **a redacted JSON document is still
valid JSON** — `tests/test_the_redactor_was_a_yaml_redactor.py` asserts both formats redact the
same object and that the result parses.

Known limits, asserted in
`tests/test_redaction_keeps_the_label_not_the_secret.py` so they cannot be
mistaken for coverage: a value with no secret-looking key near it (`foo: hunter2`)
is kept, because nothing in the text marks it; and a base64 blob embedded
mid-line survives unless the whole value is base64, because widening the token
pattern across `+` and `/` would redact filesystem paths that diagnostics need.

The redactor is used on selected storage paths and on pre-state captured for a
rollback point. It is not inserted between a tool and its `ToolMessage`, and it
is not an outbound policy engine for the LLM provider. Therefore a sensitive
value can still be present in the live prompt even when a related stored
episode or flight-recorder field would be redacted.

## Langfuse and other telemetry

`LANGFUSE_ENABLED` defaults to `false`. Tracing only becomes active when it is
enabled, both Langfuse credentials are present, and the Langfuse package is
available. The workflow then attaches the LangChain `CallbackHandler` to graph
invocations and streams, with the session ID and v4 tag in run metadata.

Because the callback is attached to the live graph, configure the Langfuse
destination as a data recipient: prompt inputs, model outputs, and tool-related
trace context may be recorded according to the Langfuse/LangChain callback
behavior. The KubeIntellect storage redactor is not applied to the live request
before that callback. Use a self-hosted Langfuse deployment or leave tracing
disabled if that destination is not acceptable.

## Self-hosted inference

`OPENAI_BASE_URL` is passed to the OpenAI-compatible client and is used by the
Qwen/DashScope path as well. That setting is a compatibility hook, not proof
that an arbitrary local or air-gapped provider is tested or supported. The
project tracks first-class, tested self-hosted provider support in [issue
#17](https://github.com/MSKazemi/kubeintellect/issues/17).

An in-cluster endpoint can keep the model request inside the cluster network,
but the endpoint operator can still see the prompt. It also does not disable
Langfuse, database backups, or other configured telemetry and persistence.

## Reducing exposure

Before connecting a real cluster:

1. Give the deployment ServiceAccount only the read permissions it needs, and
   use `KUBEINTELLECT_READONLY_KEYS` for users who should not mutate the
   cluster. Readonly access reduces mutation risk; it does not prevent those
   permitted reads from reaching the model.
2. Keep `KUBECTL_BLOCKED_RESOURCES` and the infrastructure namespace blocklist
   intact. Do not put credentials in application logs, ConfigMaps, or other
   model-readable resources.
3. Leave `rbac.allowExec: false` in production. The Helm default disables
   `pods/exec`; local development values enable it for convenience.
4. Leave `LOKI_URL` unset unless the server genuinely needs log access. If
   Loki is required, use the narrowest query and retention policy available,
   and assume log lines may contain secrets or prompt-injection text.
5. Keep Langfuse disabled unless the configured host is approved for prompt and
   tool telemetry. Protect the PostgreSQL database, checkpoint file, backups,
   and replicas with the same care.
6. If using a hosted model, review its data-use and retention terms. If using a
   compatible self-hosted endpoint, validate the deployment yourself; issue
   [#17](https://github.com/MSKazemi/kubeintellect/issues/17) tracks the missing
   first-class support and test coverage.

This page documents the existing boundaries. It does not add an egress-redaction
layer or make a claim that cluster-derived text is safe to disclose.

## Source paths checked

The claims above are based on the current v4 source in:

- `packages/kubeintellect-server/app/tools/kubectl_tool.py`
- `packages/kubeintellect-server/app/tools/prometheus_tool.py`
- `packages/kubeintellect-server/app/tools/loki_tool.py`
- `packages/kubeintellect-server/app/utils/redact.py`
- `packages/kubeintellect-server/app/memory/episodes.py`
- `packages/kubeintellect-server/app/memory/preferences.py`
- `packages/kubeintellect-server/app/memory/kg.py`
- `packages/kubeintellect-server/app/db/flight_recorder.py`
- `packages/kubeintellect-server/app/db/schema.sql`
- `packages/kubeintellect-server/app/agent/workflow.py`
- `packages/kubeintellect-server/app/core/llm.py`
