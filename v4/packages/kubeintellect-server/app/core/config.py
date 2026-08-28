from __future__ import annotations

import logging
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ~/.kubeintellect/.env is written by `kubeintellect init` for laptop installs.
# The project-local .env (CWD) overrides it so developers can still override
# settings per-project without touching the global config.
_HOME_ENV = Path.home() / ".kubeintellect" / ".env"


# Resource types no deployment can unblock. `KUBECTL_BLOCKED_RESOURCES` replaces the blocklist
# rather than extending it, so an operator adding one entry drops every other — and these four
# are credential material: Secrets carry the cluster's passwords and this app's own API keys,
# ServiceAccount objects carry token references. Everything else on the blocklist is a policy
# choice an operator may legitimately reverse; these are not.
ALWAYS_BLOCKED_RESOURCES = frozenset({"secret", "secrets", "serviceaccount", "serviceaccounts"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Later files in the tuple have higher priority.
        # Home env is loaded first; the CWD .env wins if both define the same key.
        env_file=(str(_HOME_ENV), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── API ───────────────────────────────────────────────────────────────────
    API_V1_STR: str = "/v1"

    # ── LLM provider ─────────────────────────────────────────────────────────
    LLM_PROVIDER: str = Field(default="azure")

    # Sampling temperature for every model call. 0.0 remains the default because determinism
    # is what an operator wants, but it is now configurable because it cannot always be honoured:
    # gpt-5-mini, gpt-5.5 and gpt-5.6-sol reject `temperature=0.0` outright with HTTP 400
    # ("Only the default (1) value is supported"), while gpt-4o and gpt-5.4 accept it. Before
    # this setting existed the value was hardcoded, so those three models could not be used at
    # all — and the failure was close to invisible: the request 400s, the agent still streams a
    # reply to the user, and the run completes and is graded while reporting zero tokens and
    # zero cost. Any experiment comparing models must set ONE temperature that every model in
    # the comparison accepts, or the model axis is confounded with the sampling temperature.
    LLM_TEMPERATURE: float = 0.0

    # Azure OpenAI
    AZURE_OPENAI_API_KEY: str | None = None
    AZURE_OPENAI_ENDPOINT: str | None = None
    AZURE_OPENAI_API_VERSION: str = "2024-10-01-preview"  # enables automatic prefix caching
    AZURE_COORDINATOR_DEPLOYMENT: str = "gpt-4o"
    AZURE_SUBAGENT_DEPLOYMENT: str = "gpt-4o-mini"

    # OpenAI direct (also used for any OpenAI-compatible endpoint, e.g. Alibaba
    # DashScope / Qwen: set OPENAI_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
    # with LLM_PROVIDER=openai and OPENAI_*_MODEL=qwen-max / qwen-plus).
    OPENAI_API_KEY: str | None = None
    OPENAI_BASE_URL: str | None = None  # override for OpenAI-compatible providers (Qwen/DashScope, LiteLLM, etc.)
    OPENAI_COORDINATOR_MODEL: str = "gpt-4o"
    OPENAI_SUBAGENT_MODEL: str = "gpt-4o-mini"
    # Alibaba DashScope / Qwen key aliases. The OpenAI-compatible client reads the
    # key from OPENAI_API_KEY, but when LLM_PROVIDER=qwen you can name it honestly:
    # DASHSCOPE_API_KEY (Alibaba's own convention) or QWEN_API_KEY populate it.
    DASHSCOPE_API_KEY: str | None = None
    QWEN_API_KEY: str | None = None

    # ── PostgreSQL ────────────────────────────────────────────────────────────
    # When DATABASE_URL is set (e.g. external managed DB), it takes precedence
    # over the individual POSTGRES_* vars below.
    DATABASE_URL: str | None = None
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: str = "5432"
    POSTGRES_DB: str = "kubeintellect"
    POSTGRES_USER: str = "kubeintellect"
    POSTGRES_PASSWORD: str = "password"
    POSTGRES_POOL_MIN_CONN: int = Field(default=1, gt=0)
    POSTGRES_POOL_MAX_CONN: int = Field(default=10, gt=0)

    @property
    def POSTGRES_DSN(self) -> str | None:
        if self.USE_SQLITE:
            return None
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    # ── SQLite fallback (local / no-Docker mode) ──────────────────────────────
    # Set USE_SQLITE=true in .env or auto-detected by `kubeintellect serve`.
    # Not used in Helm deployments — DATABASE_URL is always set there.
    USE_SQLITE: bool = False
    SQLITE_PATH: str = "~/.kubeintellect/kubeintellect.db"

    # ── Kubernetes ────────────────────────────────────────────────────────────
    KUBECONFIG_PATH: str = "~/.kube/config"
    # Explicit cluster identity. Set this whenever more than one cluster shares a database:
    # in-cluster deployments have no kubeconfig context to derive a name from, so identity
    # falls back to the "unknown" sentinel and every such cluster shares one memory scope.
    CLUSTER_ID: str = ""
    KUBECTL_TIMEOUT_SECONDS: int = 30
    KUBECTL_DESTRUCTIVE_TIMEOUT_SECONDS: int = 300

    # Namespaces the kubectl tool will never touch, regardless of user role.
    # In Helm deployments this is set via config.blockedNamespaces in values.yaml
    # (injected as KUBECTL_BLOCKED_NAMESPACES env var by the ConfigMap).
    # For local dev, override in .env or ~/.kubeintellect/.env.
    KUBECTL_BLOCKED_NAMESPACES: str = Field(
        default="kubeintellect,monitoring,kube-system,kube-public,kube-node-lease,ingress-nginx,cert-manager"
    )

    # Resource types the kubectl tool will never access, regardless of user role.
    # In Helm deployments set via config.blockedResources in values.yaml.
    # NOTE: this REPLACES the list. The credential types in ALWAYS_BLOCKED_RESOURCES are
    # re-added unconditionally by the property below and cannot be configured away.
    KUBECTL_BLOCKED_RESOURCES: str = Field(
        default="secret,secrets,serviceaccount,serviceaccounts"
    )

    @property
    def allowed_origins(self) -> list[str]:
        """CORS origins, **whitespace-stripped**, empties dropped.

        `main.py` passed `ALLOWED_ORIGINS.split(",")` straight to `CORSMiddleware`, which
        compares origins as exact strings. The natural way to write a list —
        `"http://localhost:3080, http://app.example.com"` — therefore produced a second entry
        of `" http://app.example.com"`, which matches no browser's `Origin` header. Measured
        2026-08-24: a request from `http://app.example.com` came back with **no**
        `access-control-allow-origin` header at all, so the origin the operator listed was
        silently not allowed and nothing anywhere said why.

        Stripping can only ever allow origins the operator explicitly wrote, never widen the
        set. What stripping cannot repair — a trailing slash, a missing scheme, a bare `*` —
        is reported by :mod:`app.core.config_audit` rather than guessed at here.
        """
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

    @property
    def kubectl_blocked_namespaces(self) -> frozenset[str]:
        """The protected-namespace set, **case-folded**.

        Eight comparison sites across `kubectl_tool`, `helm_tool` and `namespace_guard` read
        `<value>.lower() in blocked` — the normalisation was applied to one side only. This
        set kept whatever case the operator typed, so a single capital letter in
        `KUBECTL_BLOCKED_NAMESPACES` disabled the protection with no error anywhere. Measured
        2026-08-20 with `KUBECTL_BLOCKED_NAMESPACES="Kube-System"`: `kubectl get pods -A`
        returned two `kube-system` rows that should have been filtered,
        `kubectl delete deployment coredns -n kube-system` was **allowed** where it is
        normally a `[Protected]` refusal, and the Loki/Prometheus query guard passed a
        `{namespace="kube-system"}` selector straight through.

        Kubernetes namespace names are RFC 1123 labels and therefore always lowercase, so
        case-folding here can only ever *add* protection; it cannot unblock anything. Entries
        that still cannot match a legal namespace (a glob, a slash, a stray character) are
        reported by `app.core.config_audit` rather than silently ignored.
        """
        return frozenset(
            ns.strip().lower()
            for ns in self.KUBECTL_BLOCKED_NAMESPACES.split(",")
            if ns.strip()
        )

    @property
    def kubectl_blocked_resources(self) -> frozenset[str]:
        """Operator-tunable, with a floor that configuration cannot lower.

        `KUBECTL_BLOCKED_RESOURCES` replaces the list, it does not extend it — and the Helm
        values file invited exactly that ("Override to add …"). An operator adding `configmap`
        to the blocklist therefore *removed* `secret`, silently unblocking every Secret in the
        cluster including KubeIntellect's own namespace, which holds the LLM and admin API
        keys. Measured 2026-08-19: all four probes went from BLOCKED to ALLOWED.

        Credential material is the one part that was never meant to be tunable — the guard's
        own message promises Secrets are "shielded from inspection to protect cluster
        credentials". This makes that promise true. Namespaces deliberately have no floor:
        deciding the agent may investigate `monitoring` is a legitimate operator choice,
        whereas letting it read Secrets is not.

        Case is folded here for the same reason it is folded for namespaces: the guard tests
        `_resource_spellings(token) & blocked`, and that helper lower-cases the *token* only.
        Measured 2026-08-20 with `KUBECTL_BLOCKED_RESOURCES="ConfigMap"` — the spelling
        Kubernetes itself uses for `kind:` — `kubectl get configmap`, `get ConfigMap`,
        `get configmaps` and `get cm` were **all allowed**: the operator's entry matched nothing
        at all. The credential floor was unaffected, so this under-blocks what an operator added
        rather than unblocking Secrets.
        """
        return ALWAYS_BLOCKED_RESOURCES | frozenset(
            r.strip().lower()
            for r in self.KUBECTL_BLOCKED_RESOURCES.split(",")
            if r.strip()
        )

    # ── Observability ─────────────────────────────────────────────────────────
    # URLs of Prometheus and Loki running in the target cluster.
    # Empty = not configured; metric/log queries will return a "no data source" error
    # rather than crashing — the app remains fully functional for kubectl-based queries.
    PROMETHEUS_URL: str = ""
    LOKI_URL: str = ""

    LANGFUSE_ENABLED: bool = False
    LANGFUSE_PUBLIC_KEY: str | None = None
    LANGFUSE_SECRET_KEY: str | None = None
    # Set by each deployment method: localhost:3001 (compose), in-cluster DNS (Helm).
    # Empty default keeps Langfuse disabled until a host is explicitly configured.
    LANGFUSE_HOST: str = ""

    # Version tag stamped on every Langfuse trace (version:<KI_VERSION>). All versions
    # share ONE Langfuse project, so this tag is what lets cost/tokens be broken down
    # per KubeIntellect version (per-version projects need Langfuse Enterprise).
    KI_VERSION: str = "v4"

    # Build stamp, set by the container image only (Dockerfile: ENV KI_BUILD_VERSION=${VERSION},
    # fed by docker-publish.yml's `git describe --tags --always`). It exists because the image
    # installs dependencies but NOT the workspace packages -- module trees are copied in flat and
    # imported from the working directory -- so there is no dist-info to read and
    # importlib.metadata cannot answer "which release am I?". Empty everywhere else, where the
    # installed package is the authority and this is never consulted.
    KI_BUILD_VERSION: str = ""

    # ── App ───────────────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "text"
    DEBUG: bool = False
    ALLOWED_ORIGINS: str = "http://localhost:3080"

    # ── KubeIntellect behavior flags ───────────────────────────────────────────
    # Each behavior is feature-flagged so it can be toggled without a redeploy.
    KUBECTL_ERROR_HINTS_ENABLED: bool = True
    INVESTIGATION_PLAN_ENABLED: bool = True
    PLAYBOOKS_ENABLED: bool = True
    # off | lenient | strict
    SNAPSHOT_SUFFICIENCY_MODE: str = "lenient"
    # Snapshot is treated as fresh for this many seconds; older = always fetch.
    SNAPSHOT_FRESHNESS_SECONDS: int = 30

    # Reflexion loop — persist completed RCA outcomes so future sessions inherit
    # learned failure patterns. Reads are always on (memory_loader); writes are
    # gated below. Minimum confidence threshold filters out low-quality RCAs.
    REFLEXION_ENABLED: bool = True
    REFLEXION_MIN_CONFIDENCE: float = 0.7

    # Reflexion v2 — production-grade controls.
    #   VERIFY_RESOLUTION: re-snapshot the cluster after a fix and only promote
    #     patterns whose mutation actually moved the cluster to healthy.
    #   REDACT_SECRETS:    apply secret/URL/token scrubbing before persisting.
    #   PATTERN_COOLDOWN_HOURS: skip occurrence_count bumps within this window
    #     (test-loop spam protection).
    #   PATTERN_DECAY_DAYS: read filter — patterns not seen in this many days
    #     stop being injected into prompts.
    REFLEXION_VERIFY_RESOLUTION: bool = True
    REFLEXION_REDACT_SECRETS: bool = True
    REFLEXION_PATTERN_COOLDOWN_HOURS: int = 1
    REFLEXION_PATTERN_DECAY_DAYS: int = 30

    # ── Agent loop budgets (safety, not tuning) ───────────────────────────────
    # LangGraph 1.x defaults `recursion_limit` to 10007 (~3,300 ReAct steps), so
    # an agent that fails to terminate keeps *acting* until it exhausts that. The
    # read-only RCA subagents were already bounded (50, see agent/nodes/subagent.py);
    # the coordinator — which holds the write-capable toolset — was not, and neither
    # was the outer graph. These make both bounds explicit.
    #
    # Deliberately generous: they are a runaway backstop set well above any observed
    # real usage, NOT a tuned budget. Tightening them needs usage evidence. Exhaustion
    # halts and escalates to the operator with partial findings; it never truncates
    # silently. Set to a smaller value to test the escalation path.
    #
    # Outer graph: a turn costs 3 steps + 2 per coordinator↔investigation cycle,
    # so 120 allows ~58 cycles.
    AGENT_GRAPH_RECURSION_LIMIT: int = Field(default=120, gt=0)
    # Coordinator's inner ReAct loop: ~3 recursion units per step (LLM + tool call +
    # tool response), so 150 allows ~50 tool calls in one coordinator turn — triple
    # the read-only subagent's 50/~16, because the coordinator also orchestrates.
    AGENT_COORDINATOR_RECURSION_LIMIT: int = Field(default=150, gt=0)

    # ── Flight recorder (V4 ADR-005) ──────────────────────────────────────────
    # Append-only, hash-chained decision_log of every non-token event, keyed by
    # episode (== session in V2 instrumentation). Fire-and-forget: an outage
    # degrades auditability, never availability. Requires Postgres.
    FLIGHT_RECORDER_ENABLED: bool = True

    # ── Sensorium / detectors (V4 ADR-006) ────────────────────────────────────
    # Always-on kubectl --watch perception feeding the compiled detector
    # engine (zero-token known-failure detection). Degrades gracefully when
    # kubectl/RBAC is unavailable.
    SENSORIUM_ENABLED: bool = True
    # Bounded buffer between the kubectl watch streams and the detector engine. Past this depth
    # the OLDEST observation is dropped and a shed counter increments, so an overloaded sensorium
    # reports lossy detection instead of growing until the container is OOMKilled. See
    # app/sensorium/k8s_watcher.py and design/enterprise-readiness.md (A5).
    SENSORIUM_QUEUE_MAXSIZE: int = 10000
    # The SECOND bounded buffer on the same observation stream: the sensorium sink calls
    # `engine.process(obs)` AND `enqueue_observation(obs)`, and the latter feeds this queue,
    # drained onto the knowledge graph. It was a hardcoded 10_000 in app/memory/service.py
    # while its twin above was configurable, so an operator who widened the sensorium buffer
    # on a busy cluster had not widened this one and kept losing observations at the same
    # depth. Drops here are counted and surfaced as `observations_dropped` on /healthz -- the
    # loss was always visible; the knob was the half that was missing.
    MEMORY_OBS_QUEUE_MAXSIZE: int = 10000
    # Namespaces the watch streams cover. Empty (the default) means `-A` — every namespace.
    #
    # This is the ONE lever that scales perception on a large cluster, and it works by watching
    # LESS, not faster: `get pods -A --watch` relists the entire cluster on every reconnect, so
    # past a few thousand pods the honest options are to narrow the scope or to accept counted
    # loss (see SENSORIUM_QUEUE_MAXSIZE above). Comma-separated; whitespace is trimmed.
    #
    # ⚠️ Scoping creates a blind spot BY DESIGN, so it is disclosed rather than silent: the
    # perception surfaces (`/v1/findings`, `kq findings`, the morning digest) report the scope as
    # a gap, because an empty findings list from a scoped sensorium is not a statement about the
    # cluster. See ADR-020 and app/detectors/perception.py.
    SENSORIUM_WATCH_NAMESPACES: str = ""

    # ── Leader election (multi-replica safety) ────────────────────────────────
    # Every background worker here is a singleton by ASSUMPTION, enforced only by the chart's
    # `replicaCount: 1`. Two replicas do not share perception work, they duplicate it: two watch
    # streams, two engines firing the same finding, two watchtowers acting on it. See
    # app/core/leader.py. With this on, exactly one replica runs them and the rest serve the API
    # as warm standbys — which is what makes replicaCount > 1 (and therefore HA) safe at all.
    #
    # Default ON: it is a no-op for the single-replica deployments that exist today (an
    # uncontended lock is acquired instantly at startup) and it is the only thing standing between
    # a scaled Deployment and duplicated autonomous action against a live cluster.
    LEADER_ELECTION_ENABLED: bool = True
    LEADER_ELECTION_POLL_SECONDS: float = 10.0

    # ── Predictive / anticipatory detection (V4 ADR-010) ──────────────────────
    # Deterministic trend predicates: project a range-PromQL metric toward its
    # threshold (least-squares slope, zero tokens) and fire a `predicted` finding
    # before the failure manifests. Predicted findings are capped at A1 in the
    # watchtower (never auto-fix). Default off until validated. Fail-open.
    PREDICTIVE_DETECTION_ENABLED: bool = False
    PREDICTIVE_TREND_INTERVAL_SECONDS: int = 60

    # ── Natural-language detector authoring (V4 ADR-012) ──────────────────────
    # Compile a plain-English failure into a detect block, stage it as a SHADOW
    # candidate, and require human promotion before it can act. Shadow detectors
    # never reach the watchtower. Default off (new + LLM + touches detection).
    NL_DETECTOR_AUTHORING_ENABLED: bool = False
    DB_DETECTOR_REFRESH_SECONDS: int = 120   # pick up promotions without a restart

    # ── Incident postmortems (V4 ADR-011) ─────────────────────────────────────
    # Read-only grounded narrative over the flight recorder. The deterministic
    # seq-cited timeline is always available; the LLM narrative is opt-in (the
    # only token-spending part) and falls back to the timeline on any failure.
    POSTMORTEM_ENABLED: bool = True
    POSTMORTEM_LLM_NARRATIVE: bool = False

    # ── Memory hierarchy (V4 ADR-002) ─────────────────────────────────────────
    # L1 episodic + L2 temporal KG + consolidation worker. Postgres-native;
    # recall uses pg_trgm (pgvector optional, added when embeddings are wired).
    MEMORY_HIERARCHY_ENABLED: bool = True
    # Memory V5 P1 (ADR-014): fuse the pg_trgm channel with a full-text (ts_rank)
    # lexical channel via Reciprocal Rank Fusion for episode recall. Off = the
    # baseline single-channel trgm recall. Additive; no schema change required
    # (ts_rank is computed inline; a stored tsvector + GIN index is the P0 speedup).
    MEMORY_HYBRID_RETRIEVAL: bool = False
    # Minimum pg_trgm similarity used by the baseline recall and summary channels.
    # Keep this configurable for deployments with different memory vocabularies.
    MEMORY_RECALL_SIMILARITY_FLOOR: float = Field(default=0.02, ge=0.0, le=1.0)
    # Memory V5 P2 (ADR-013): bi-temporal KG. When on, edge writes stamp event-time
    # valid_from (from the observation) and supersede via retract (retracted_at) instead
    # of only closing (valid_to); point-in-time `as_of(valid_t, tx_t)` queries become
    # available. Off = the single valid-time axis (current behaviour). The transaction-time
    # columns are populated regardless (default now()); the flag gates the new *semantics*.
    MEMORY_BITEMPORAL_ENABLED: bool = False
    # Memory V5 P3 (ADR-014): multi-hop blast-radius via Personalized PageRank. When on,
    # `kg.ppr_blast_radius(seeds)` pulls a bounded ≤N-hop subgraph with a recursive CTE and
    # ranks related entities with in-process PPR (power iteration) — NOT PPR-in-SQL (review
    # F1). Off = the query returns []. Bounded subgraph keeps it fast and dependency-free.
    MEMORY_KG_PPR: bool = False
    # Memory V5 P4 (ADR-015): reconcile the KG write path. When on, `kg.reconcile_edge()`
    # decides ADD/UPDATE/RETRACT/NOOP against existing memory (Mem0-style dedup + supersede)
    # behind a query-independent salience gate; RETRACT sets retracted_at, never DELETEs
    # (review F4), and defaults to ADD when confidence is low. Off = plain open_edge (ADD).
    # Intended for the extracted/LLM fact path, not sensor ingest (ground-truth valid-time).
    MEMORY_WRITE_RECONCILE: bool = False
    # Memory V5 P5 (ADR-016): promotion pipeline. When on, the consolidation worker promotes
    # verified, recurring episodes into `semantic_rules` (IF context → THEN guidance); rules
    # that recur enough go 'active' (injected into the prompt) and are eligible to seed a
    # detector *candidate* — which still requires human review (ADR-006/012). Off = no promotion.
    MEMORY_PROMOTION: bool = False
    # Memory V5 P6 (ADR-017): importance/surprise-weighted retention. When on, each
    # episode write is scored for `importance` (from incident severity — regression >
    # partial > resolved, boosted by verified + confidence) and `surprise` (a KG-novelty
    # proxy: how unlike recent memory the episode is), both stored on the row. Recall then
    # ranks recency × importance × relevance (R6.1) — importance modulates RANKING ONLY,
    # never retention/audit (R6.2). Surprise gates redundant *low-value* auto-writes
    # (unverified + report-only near-duplicates) so noise does not accrete (R6.3); verified
    # or actioned episodes are always kept. Off = flat relevance+recency recall, all writes.
    MEMORY_IMPORTANCE: bool = False
    # Memory V5 P6 (ADR-017): first-class prospective memory. When on, the watchtower
    # records a "re-check condition C at/after T" after an autonomous fix, and the
    # consolidation scheduler fires due re-checks and records the outcome (R6.4). Firing
    # routes through the autonomy ladder (A0 namespaces never fire). Off = no re-checks.
    MEMORY_PROSPECTIVE: bool = False
    # Memory V5 P7 (ADR-018): security-hardened memory write path. When on, user-derived
    # memory writes pass an audit-before-write guard with DIVERSE, non-LLM-primary validators
    # (F5: a quorum of the same LLM fails together under MINJA) — provenance/trust scoring
    # (sensor=ground-truth, user-chat=low-trust MINJA surface), a heuristic memory-injection
    # pattern check, and a per-requester rate limiter; low-trust or poisoned writes are
    # quarantined (skipped), not persisted as trusted memory. Sensor-derived writes skip the
    # guard (R8.1). A [0,1] provenance `trust` is stamped on each episode (R8.2). Off = every
    # write is admitted as today. (RLS tenancy R8.3 is scaffolded in schema.sql, enabled
    # separately with the tenant-GUC connection discipline; RTBF R8.4 is always available.)
    MEMORY_SECURITY_HARDENING: bool = False
    MEMORY_WRITE_RATE_PER_MIN: int = 30   # max user-derived memory writes per requester / minute
    MEMORY_TRUST_FLOOR: float = 0.35      # user-derived writes below this trust are quarantined
    # How often the running server re-verifies the memory audit chain, in seconds. 0 disables
    # the periodic check and leaves only the one at startup; a negative value disables both.
    #
    # ⚠️ Why this setting exists at all, dated 2026-08-28: `verify_memory_chain` was written,
    # tested and documented, and then **nothing in a running server ever called it** — the only
    # callers were the test suite and `scripts/memory_pg_probe.py`. Tamper-evidence that is
    # never evaluated is not weaker evidence than a live check, it is none: a chain can only
    # accuse if somebody asks it. The check is periodic rather than per-request because
    # verifying reads every audit row for the cluster, and `/healthz` is probed every few
    # seconds — an on-demand verifier would make probe latency a function of cluster history.
    MEMORY_CHAIN_VERIFY_INTERVAL_S: int = 900
    # Memory V5 P8 (spec R7): a RAPTOR/GraphRAG-style summary hierarchy over episode clusters.
    # When on, the consolidation worker builds one theme summary per (cluster, playbook|namespace)
    # signature so the agent can answer theme-level questions without scanning every episode.
    # Regeneration is tied to KG change-rate (rebuild only when new episodes arrived or the
    # cluster's KG edge count moved), never a fixed clock alone. Deterministic (no LLM);
    # abstractive LLM summarization deferred. Off = no summary tree. Default off (additive).
    MEMORY_SUMMARY_TREE: bool = False
    MEMORY_SUMMARY_MIN_CLUSTER: int = 3   # min episodes in a theme before it gets a summary

    # ── Data retention / lifecycle (enterprise A10) ───────────────────────────
    # Age out the telemetry and completed-work tables so a long-lived install stays bounded.
    # 0 = keep everything, and that is the default on purpose: a data-deleting default would
    # silently discard an operator's history on upgrade. `app/memory/retention.py` carries the
    # per-table rules AND the tables it refuses to prune with a dated reason — the hash-chained
    # ledgers (`decision_log`, `memory_audit`) are never pruned by a clock, because deleting
    # their newest rows is invisible to `verify_chain` and would contradict the head anchors.
    # `promotion_outcomes` has a hard floor at the ADR-102 window: setting this below it does
    # NOT prune the samples the A3 statistical brake demotes on.
    MEMORY_RETENTION_DAYS: int = 0

    # ── API rate limiting (enterprise A16) ────────────────────────────────────
    # Per-caller token bucket, keyed by a hash of the bearer token (never the key itself).
    # ON by default: a limiter that ships off is not a limiter, and 120/min per key is far above
    # any interactive use of `kq`. Probe and metrics paths are exempt — see `api/rate_limit.py`
    # for why answering 429 to a kubelet probe would be worse than having no limiter at all.
    # NOTE: the counters are PER REPLICA. N replicas admit up to N x RATE_LIMIT_PER_MIN per
    # caller; this is a fair-use bound, not a fleet-wide quota, and volumetric abuse belongs at
    # the ingress.
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_PER_MIN: int = 120         # sustained requests per minute, per caller
    RATE_LIMIT_BURST: int = 30            # bucket capacity — how much idle allowance accrues
    RATE_LIMIT_MAX_TRACKED: int = 10000   # bucket-table cap; least-recently-seen is evicted
    # How many proxies sit in front of this server. 0 (the default) means `X-Forwarded-For` is
    # ignored entirely, which is the only safe default: the header is written by the client and
    # honouring it unconditionally lets one caller mint a fresh bucket per request.
    #
    # It matters because a caller with no bearer token is keyed by address, and behind the
    # Ingress this chart ships every such request arrives from the ingress pod — so all anonymous
    # traffic shared ONE bucket and any single client could shut out the rest (measured
    # 2026-08-28). Set this to the number of trusted hops and the client address is read that
    # many entries from the right of the header, the end proxies append to and a client cannot
    # forge. One ingress controller and nothing else in front of it is `1`.
    RATE_LIMIT_TRUSTED_PROXY_HOPS: int = 0

    # ── Operator-preference memory (MemoryAgent) ──────────────────────────────
    # Remembers how each user likes to operate (explicit + behaviour-inferred),
    # injects them into the prompt, and forgets stale inferred ones over time.
    PREFERENCE_MEMORY_ENABLED: bool = True
    PREFERENCE_DECAY_DAYS: int = 60        # inferred prefs not reinforced within this window may be forgotten
    PREFERENCE_MIN_CONFIDENCE: float = 0.3  # inferred prefs below this (and stale) are forgotten / not injected
    PREFERENCE_INFER_MIN_OCCURRENCE: int = 3  # min behaviour repetitions before an inferred pref is recorded

    # ── Cortex V4 (ADR-001) ───────────────────────────────────────────────────
    # Explicit-node reasoning graph (triage → gather loop → synthesize →
    # remember) replacing create_react_agent. Off = the V2 graph (kept for the
    # LOFA L4 A/B; retired once V4 meets the eval baseline).
    CORTEX_V4_ENABLED: bool = False
    # Bound on gather-loop LLM↔tools iterations per turn.
    CORTEX_MAX_GATHER_ROUNDS: int = 8

    # ── Cortex V5 / K8s-ACI (v5 design specs/00, specs/01; ADR-101) ────────────
    # Additive, default-off v5 slices layered on the V4 server (NOT a fork).
    # With every KI_V5_* flag off the server is byte-identical to V4.
    # Master switch for all v5 slices.
    CORTEX_V5_ENABLED: bool = False
    # K8s-ACI v0: bounded, normalized, never-silent read-only verbs
    # (inspect/search/logs/diff_change) wrapping run_kubectl. The read-verb
    # allowlist is exported regardless of this flag so the P2 harness subagent
    # runner can constrain investigation subagents to exactly these verbs.
    KI_V5_ACI_READ_VERBS_ENABLED: bool = False
    # Output-window bound for ACI verbs (100-line rule from the ACI SOTA).
    KI_V5_ACI_MAX_LINES: int = 100
    # Soft character cap applied after the line window (≈2k-token guard).
    KI_V5_ACI_MAX_CHARS: int = 8000
    # OTel GenAI spans (specs/02): emit agent/tool/mcp/mutation spans as additive
    # hash-chained decision_log rows (kind=ki_otel_span). Reuses ADR-005 storage.
    KI_V5_OTEL_SPANS_ENABLED: bool = False
    # Harness gather-loop (ADR-101): apply the never-silent, line-aligned ≤2k
    # summary bound to gather-tool output instead of v4's silent mid-line 8k chop,
    # and dispatch the read-only investigation fan-out from inside the gather node.
    KI_V5_HARNESS_FANOUT: bool = False
    # Upper bound on concurrent read-only investigation subagents the harness runner
    # may fan out per gather round (ADR-101). P0 lands the contract-guarded seam +
    # skeleton; the real parallel dispatch body is P2 (gated on ADR-101 acceptance).
    KI_V5_HARNESS_MAX_SUBAGENTS: int = 4
    # Bound on ACI-verb rounds an individual investigation subagent may take before it
    # must summarize (P2 body; keeps each isolated investigator cheap and terminating).
    KI_V5_HARNESS_MAX_SUBAGENT_ROUNDS: int = 3
    # Run fan-out investigation subagents on the larger (coordinator) model tier instead of the
    # small specialist. Small models (e.g. gpt-4o-mini) mis-investigate — draw wrong conclusions
    # like a false "not found". Costs more tokens per subagent; default off.
    KI_V5_HARNESS_SUBAGENT_LARGE_MODEL: bool = False
    # Verification ladder, read side (P2, 01-architecture §3.6): an adversarial fresh-context
    # reviewer checks the synthesized RCA against the gathered evidence and appends a calibrated
    # verification note (supported / unsupported-claim caveats). Default-off; flag off = no review.
    KI_V5_VERIFY_LADDER: bool = False
    # Responsiveness (P2, REQ-developer-19): never let a long investigation present as a silent
    # block — emit a progress heartbeat every KI_V5_HEARTBEAT_SECONDS while a slow phase runs, and
    # track the first-signal / full-investigation latency budgets. Default-off; flag off = no
    # heartbeat and byte-identical V4 streaming.
    KI_V5_RESPONSIVENESS: bool = False
    KI_V5_HEARTBEAT_SECONDS: float = 10.0      # SSE gap ceiling for a running phase (>10 s rule)
    KI_V5_FIRST_SIGNAL_BUDGET_S: float = 30.0  # p90 first-useful-result target
    KI_V5_FULL_BUDGET_S: float = 120.0         # p90 full-investigation target
    # Escalation-avoidance briefs (P2, A-CH-17-07): append a responder-skill-calibrated brief with
    # EXPLICIT escalate-only-if bounds to an investigation, so responders don't escalate by default
    # (only ~20% of incidents resolve without escalation, ~5 engineers each — C4). Default-off.
    KI_V5_ESCALATION_BRIEFS: bool = False
    # Default responder skill the brief calibrates to (junior|intermediate|senior) when the caller
    # does not specify one.
    KI_V5_RESPONDER_LEVEL: str = "intermediate"
    # Runbooks-as-skills v0 (P2): render the matched playbooks as on-demand SKILL.md blocks injected
    # into the gather prompt — only the ones whose triggers fired, not all 18 (prompt-budget win).
    # Default-off; flag off = the gather prompt is unchanged.
    KI_V5_RUNBOOK_SKILLS: bool = False
    # L0 file plane (P2): the consolidation worker regenerates CLUSTER.md + MEMORY.md ≤25KB
    # projections of the memory (files are a projection; Postgres stays the source of truth).
    # Default-off; the files are written only when the flag is on.
    KI_V5_FILE_PLANE: bool = False
    KI_V5_FILE_PLANE_DIR: str = "ki-memory"       # output dir for CLUSTER.md / MEMORY.md
    KI_V5_FILE_PLANE_MAX_BYTES: int = 25_000       # per-file byte ceiling (the ≤25KB rule)
    # Investigation write-back (P2): confirm/contradict traversed KG edges via reconcile_edge after
    # an investigation, so the topology self-corrects at near-zero cost. Default-off.
    KI_V5_INVESTIGATION_WRITEBACK: bool = False
    # Change-first RCA (P2, A-CH-02-07): rank candidate root causes by the recent-change prior
    # (≈79% of outages follow a change). Needs the P1 change ledger to be populated to have effect.
    KI_V5_CHANGE_FIRST_RCA: bool = False
    # Change ledger (P1 evidence substrate): capture the changes KubeIntellect itself applies (its
    # mutating kubectl commands) into a per-cluster ledger that feeds the change-first RCA prior.
    # v0 records the agent's own mutations (reliable, cluster-watch-free); cluster-wide capture off
    # the sensorium event stream is the richer follow-up. Default-off.
    KI_V5_CHANGE_LEDGER: bool = False
    # Blast-radius / spend budget gate (P3 Trust plane): an additional fail-closed gate on
    # autonomous writes. FAIL-CLOSED for write authority (governance unreachable ⇒ no auto-write);
    # it NEVER blocks workloads or human break-glass (that path is independent of the agent stack).
    # Default-off; flag off = the autonomy ladder is unchanged.
    KI_V5_BLAST_RADIUS_BUDGET: bool = False
    KI_V5_KILL_SWITCH: bool = False            # engage ⇒ deny ALL autonomous writes (fail-closed)
    KI_V5_CHANGE_FREEZE: bool = False          # deny-by-default change-freeze window
    KI_V5_SPEND_CAP_USD: float = 0.0           # per-scope spend ceiling (0 = unlimited; needs usage source)
    KI_V5_SPEND_IN_PRICE_PER_1K: float = 0.0025    # USD per 1k input tokens (default gpt-4o-ish)
    KI_V5_SPEND_OUT_PRICE_PER_1K: float = 0.01     # USD per 1k output tokens
    # Staged propagation (P3 blast-radius): a multi-target change is released in bounded stages with
    # a wait window between them — never instant-global (roadmap §5 verification gate). Default-off.
    KI_V5_STAGED_PROPAGATION: bool = False
    KI_V5_STAGE_SIZE: int = 1                    # max targets applied per stage
    KI_V5_STAGE_WINDOW_SECONDS: float = 300.0    # min wait between stages
    # Change-schedule + failure-domain budget (P3 blast-radius, REQ-sysadmin-18): a disruptive op is
    # denied if it would push a failure domain (zone/rack) past its unavailability cap, or if it
    # falls outside an allowed maintenance window. Default-off.
    KI_V5_FAILURE_DOMAIN_BUDGET: bool = False
    KI_V5_MAX_UNAVAILABLE_PER_ZONE: float = 0.34   # never take >~1/3 of a failure domain down at once
    # Statistical autonomy promotion engine (P3 Trust plane, ADR-102): an action-class earns a rung
    # only when its shadow-agreement Wilson-LCB clears the per-transition threshold, and is auto-
    # demoted on regression. ON: each cluster-verified autonomous fix is recorded as one sample for
    # `watchtower-autofix`, and the watchtower reads that record before it lets an investigation fix
    # anything -- REVOKING A3 auto-fix when the record has collapsed. Revocation only: these samples
    # come from writes the allowlist already permitted, so earning authority from them would be
    # circular, and the system does not run shadow agreement yet. The allowlist stays the only way
    # up. Default-off; off => A3 is exactly the ladder + allowlist + kill-switch/freeze gate.
    KI_V5_STATISTICAL_PROMOTION: bool = False
    # ADR-102 arm-3 offline-shadow weighting: offline-derived promotion outcomes count this much (≤
    # the 0.5 cap) vs a live shadow run's 1.0. The MECHANISM is built (`weighted_lcb`); the exact cap
    # awaits offline-vs-live calibration on real production shadow data (arm-3, gated on elapsed time).
    KI_V5_OFFLINE_SHADOW_WEIGHT: float = 0.5
    # ACI mutating-verb chokepoint (P3 Action/Trust): every proposed mutation is stamped with a
    # rollback class and routed through a write-authority decision (budget gate + earned rung +
    # reversibility) BEFORE execution. This flag governs the proposal/decision path only; actual
    # execution + server-side dry-run + Kyverno/VAP admission are separately cluster-gated. Default-off.
    KI_V5_ACI_MUTATING_VERBS: bool = False
    # Heterogeneous model routing + air-gap floor (P4, ADR-103): small/local model for triage &
    # tool-call formatting, frontier for RCA synthesis; when disconnected, degrade to a read-only
    # triage floor on the small model (unsupervised edge writes are an explicit non-goal). Default-off.
    KI_V5_MODEL_ROUTING: bool = False
    KI_V5_AIRGAP_FLOOR: bool = False
    # Per-change ephemeral watchdog (P4, A-CH-02-14): a change in the ledger arms a bounded,
    # TTL-scoped read-only investigation that "reads the diff" — change-conditioned MTTD. Consumes
    # the P1 change ledger; degrades gracefully to status quo when off. Default-off.
    KI_V5_CHANGE_WATCHDOG: bool = False
    KI_V5_WATCHDOG_TTL_SECONDS: int = 300
    KI_V5_WATCHDOG_MAX_ACTIVE: int = 5
    # Rightsizing recommendations (P4, A-CH-08): evidence-grounded resource-limit advice from
    # observed OOM/peak-memory/CPU-throttle signals (recommendations are commodity; being believed
    # is the product). Default-off. A resize applied autonomously would go through the P3 chokepoint.
    # A recommendation carries `assessed`: silence because usage is in-band and silence because the
    # container was never observed are different answers, and a no-op reads as an all-clear.
    KI_V5_RIGHTSIZING: bool = False
    # Predictive-detection fusion (P4): a predicted (ADR-010 TrendPredicate) finding now LAUNCHES a
    # read-only investigation of the leading indicators before the failure realizes, instead of
    # capping at an A1 finding. Read-only only — a prediction never drives a mutation. Default-off.
    KI_V5_PREDICTIVE_FUSION: bool = False
    # NL-detector → statistical ladder (P4, ADR-012): a shadow NL-authored detector accumulates
    # precision stats; when its true-positive-rate LCB clears the bar over enough firings it is
    # SURFACED for human promotion to active. Human review stays the final gate — never auto-activates.
    KI_V5_NL_DETECTOR_LADDER: bool = False
    KI_V5_DETECTOR_MIN_FIRINGS: int = 20            # min shadow firings before eligible
    KI_V5_DETECTOR_PRECISION_THETA: float = 0.90    # true-positive-rate LCB bar to surface for review
    # Agentic-workload SRE + GPU-health detectors (P4, D17/SD-D): the incumbent-empty surface —
    # agent runaway (tool-call rate/cost), sandbox-escape, and GPU/ResourceClaim health on the C7
    # ACI-2026 device types. Default-off.
    KI_V5_AGENTIC_WORKLOAD_DETECTOR: bool = False
    KI_V5_GPU_HEALTH_DETECTOR: bool = False
    KI_V5_AGENT_TOOL_RATE_CAP: float = 60.0         # tool calls/min above this ⇒ runaway
    KI_V5_AGENT_COST_RATE_CAP: float = 1.0          # USD/min above this ⇒ runaway spend
    # Predictive pre-capture (P4, A-CH-20-16): a predicted finding with a near-term ETA arms
    # high-verbosity recorders / a CRIU checkpoint BEFORE the failure, so post-mortem evidence exists
    # instead of being lost at death — targeted spend only on imminent predictions. Default-off.
    KI_V5_PREDICTIVE_PRECAPTURE: bool = False
    KI_V5_PRECAPTURE_ETA_MIN: float = 15.0          # only pre-capture when ETA ≤ this (targeted spend)
    # Fleet memory exchange (P5, ADR-105): cross-cluster memory sharing with strict tenant isolation
    # (a cluster only ever reads its own tenant's fleet knowledge). Default-off.
    KI_V5_FLEET_EXCHANGE: bool = False
    # Fleet-wide signal pooling (P5): pool agent-runaway / detector hits across a tenant's clusters —
    # the same pattern on >= N clusters is a fleet-wide incident (a bad agent version, a shared
    # dependency) that single-cluster detection misses. Tenant-scoped. Default-off.
    KI_V5_FLEET_SIGNAL_POOLING: bool = False
    KI_V5_FLEET_PATTERN_MIN_CLUSTERS: int = 3       # same signal on >= this many clusters ⇒ fleet alert
    # Two-axis capability sandbox (P3 Trust): the agent acts through an impersonated ServiceAccount
    # scoped to a capability role (read-only / namespace-write / never-cluster-admin), so RBAC — not
    # just the app's own policy — bounds what a mutation can touch (defense in depth). Default-off.
    KI_V5_CAPABILITY_SANDBOX: bool = False
    KI_V5_SANDBOX_SA_NAMESPACE: str = "kubeintellect"
    KI_V5_SANDBOX_READONLY_SA: str = "ki-readonly"
    KI_V5_SANDBOX_WRITER_SA: str = "ki-writer"

    # Anthropic provider (optional; LLM_PROVIDER=anthropic, needs langchain-anthropic)
    ANTHROPIC_API_KEY: str | None = None
    ANTHROPIC_LARGE_MODEL: str = "claude-sonnet-4-6"
    ANTHROPIC_SMALL_MODEL: str = "claude-haiku-4-5-20251001"

    # ── Autonomy ladder + watchtower (V4 ADR-003) ─────────────────────────────
    # A0 observe | A1 investigate+report | A2 propose | A3 auto-fix (allowlist).
    # Protected namespaces are always pinned to A0 for autonomous action.
    WATCHTOWER_ENABLED: bool = True
    WATCHTOWER_ROLE: str = "operator"
    AUTONOMY_LEVEL: str = "A1"
    AUTONOMY_NAMESPACE_LEVELS: str = ""     # "prod=A0,dev=A2"
    AUTONOMY_A3_ALLOWLIST: str = ""         # "CrashLoopBackOff/dev-*"

    # ── Auth / RBAC ───────────────────────────────────────────────────────────
    # Four-tier role model (all comma-separated; empty = auth disabled):
    #   superadmin — admin capabilities + write access to all namespaces (no ns block)
    #   admin      — high + medium risk ops, always HITL-gated; infra ns writes blocked
    #   operator   — medium risk ops only (create, apply, scale, exec…), HITL-gated; high-risk blocked
    #   readonly   — read-only ops only; all writes rejected before reaching the agent
    KUBEINTELLECT_SUPERADMIN_KEYS: str = Field(default="")
    KUBEINTELLECT_ADMIN_KEYS: str = Field(default="")
    KUBEINTELLECT_OPERATOR_KEYS: str = Field(default="")
    KUBEINTELLECT_READONLY_KEYS: str = Field(default="")

    # AUTH_BACKEND controls how readonly (demo) keys are validated:
    #   static — keys are checked against KUBEINTELLECT_READONLY_KEYS (default)
    #   hmac   — keys of the form ki-ro-<payload>.<sig> are validated via HMAC;
    #            no list needed — any unexpired key signed with the right secret is valid
    AUTH_BACKEND: str = Field(default="static")

    # Secret used to sign and verify HMAC demo keys (AUTH_BACKEND=hmac).
    # Rotate to invalidate all outstanding demo keys instantly.
    # Generate: openssl rand -hex 32
    DEMO_KEY_HMAC_SECRET: str | None = None

    # ── Production auth hardening ─────────────────────────────────────────────
    # With no keys configured the server treats EVERY unauthenticated caller as `admin` (see
    # app/api/v1/auth.py). That default is right for a first-run `kubeintellect serve` and
    # catastrophic in a data centre: full HITL-gated write access to the cluster, granted
    # silently, with nothing in the product looking wrong.
    #
    # REQUIRE_AUTH converts that silent grant into a loud startup failure. Off by default so the
    # local quickstart is unchanged; the chart turns it on for any deployment that sets keys.
    REQUIRE_AUTH: bool = False

    # ── Self-observability ────────────────────────────────────────────────────
    # Prometheus metrics about THIS process at /metrics: request rate, latency histogram, error
    # ratio. The instrumentator dependency was declared but never mounted, so the component that
    # watches every other workload was the one nobody could graph.
    METRICS_ENABLED: bool = True

    # Default TTL for demo keys minted via POST /v1/auth/demo-keys.
    DEMO_KEY_DEFAULT_TTL_HOURS: int = Field(default=24 * 7)   # 7 days
    # Hard cap so a misbehaving caller can't request multi-year keys.
    DEMO_KEY_MAX_TTL_HOURS: int = Field(default=24 * 30)      # 30 days

    @property
    def superadmin_keys(self) -> set[str]:
        return {k.strip() for k in self.KUBEINTELLECT_SUPERADMIN_KEYS.split(",") if k.strip()}

    @property
    def admin_keys(self) -> set[str]:
        return {k.strip() for k in self.KUBEINTELLECT_ADMIN_KEYS.split(",") if k.strip()}

    @property
    def operator_keys(self) -> set[str]:
        return {k.strip() for k in self.KUBEINTELLECT_OPERATOR_KEYS.split(",") if k.strip()}

    @property
    def readonly_keys(self) -> set[str]:
        return {k.strip() for k in self.KUBEINTELLECT_READONLY_KEYS.split(",") if k.strip()}

    @property
    def auth_enabled(self) -> bool:
        return bool(self.superadmin_keys or self.admin_keys or self.operator_keys or self.readonly_keys or self.DEMO_KEY_HMAC_SECRET)

    @model_validator(mode="after")
    def _validate_provider(self) -> Settings:
        valid = {"azure", "openai", "anthropic", "qwen"}
        if self.LLM_PROVIDER not in valid:
            raise ValueError(
                f"LLM_PROVIDER must be one of {sorted(valid)}, got {self.LLM_PROVIDER!r}.\n"
                f"  Fix: set LLM_PROVIDER=qwen (or openai / azure / anthropic) in ~/.kubeintellect/.env"
            )
        # 'qwen' is a first-class alias for the OpenAI-compatible DashScope path:
        # it auto-targets the DashScope endpoint and qwen-* model defaults so an
        # Alibaba/Qwen Cloud deployment needs only LLM_PROVIDER=qwen + an API key.
        if self.LLM_PROVIDER == "qwen":
            if not self.OPENAI_API_KEY:
                self.OPENAI_API_KEY = self.DASHSCOPE_API_KEY or self.QWEN_API_KEY
            if not self.OPENAI_BASE_URL:
                self.OPENAI_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
            if self.OPENAI_COORDINATOR_MODEL == "gpt-4o":
                self.OPENAI_COORDINATOR_MODEL = "qwen-max"
            if self.OPENAI_SUBAGENT_MODEL == "gpt-4o-mini":
                self.OPENAI_SUBAGENT_MODEL = "qwen-plus"
        if self.LLM_PROVIDER == "anthropic":
            if not self.CORTEX_V4_ENABLED:
                logging.warning(
                    "LLM_PROVIDER=anthropic is only used by the V4 cortex — "
                    "set CORTEX_V4_ENABLED=true (the V2 graph supports azure/openai only)."
                )
            if not self.ANTHROPIC_API_KEY:
                logging.warning(
                    "LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set. "
                    "LLM calls will fail until it is set in ~/.kubeintellect/.env"
                )
        if self.LLM_PROVIDER == "azure":
            missing = [
                k for k, v in {
                    "AZURE_OPENAI_API_KEY": self.AZURE_OPENAI_API_KEY,
                    "AZURE_OPENAI_ENDPOINT": self.AZURE_OPENAI_ENDPOINT,
                }.items() if not v
            ]
            if missing:
                logging.warning(
                    f"LLM_PROVIDER=azure but missing: {', '.join(missing)}. "
                    f"LLM calls will fail until these are set in ~/.kubeintellect/.env"
                )
        elif self.LLM_PROVIDER in ("openai", "qwen") and not self.OPENAI_API_KEY:
            where = (
                "DashScope key at https://dashscope-intl.console.aliyun.com/"
                if self.LLM_PROVIDER == "qwen"
                else "key at https://platform.openai.com/api-keys"
            )
            logging.warning(
                f"LLM_PROVIDER={self.LLM_PROVIDER} but OPENAI_API_KEY is not set. "
                f"Get your {where} and add OPENAI_API_KEY=... to ~/.kubeintellect/.env"
            )
        valid_modes = {"off", "lenient", "strict"}
        if self.SNAPSHOT_SUFFICIENCY_MODE not in valid_modes:
            raise ValueError(
                f"SNAPSHOT_SUFFICIENCY_MODE must be one of {valid_modes}, "
                f"got {self.SNAPSHOT_SUFFICIENCY_MODE!r}."
            )
        return self


def _load_settings() -> Settings:
    """Load Settings, converting Pydantic validation errors into readable messages."""
    import sys as _sys
    try:
        return Settings()
    except Exception as exc:
        cfg_file = Path.home() / ".kubeintellect" / ".env"
        # Strip the raw Pydantic traceback and show something actionable
        raw = str(exc)
        print(f"\nConfiguration error: {raw.splitlines()[0]}", file=_sys.stderr)
        if "llm_provider" in raw.lower():
            print(
                f"  LLM_PROVIDER must be 'openai' or 'azure'.\n"
                f"  Edit {cfg_file} and set: LLM_PROVIDER=openai",
                file=_sys.stderr,
            )
        elif "pool" in raw.lower() or "postgres_pool" in raw.lower():
            print(
                f"  POSTGRES_POOL_MIN_CONN and POSTGRES_POOL_MAX_CONN must be positive integers.\n"
                f"  Edit {cfg_file} to fix these values.",
                file=_sys.stderr,
            )
        else:
            print(
                f"  Config file: {cfg_file}\n"
                f"  Run 'kubeintellect init' to reconfigure.",
                file=_sys.stderr,
            )
        raise


settings = _load_settings()
