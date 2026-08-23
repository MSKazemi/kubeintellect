# Changelog

All notable changes to this repository are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/).

> **Scope.** This is the **repo-wide** changelog. It tracks shared infrastructure and the
> **active development line** — currently the **v4** platform and the **v5** design tier
> (whose slices ship as default-off flags inside the v4 server, ADR-101). The frozen
> generations keep their own history: **v3** has `v3/CHANGELOG.md`; **v1** and **v2** are
> versioned by their git tags (`v1.0`, `v2.0.x`). See the root `README.md` for the v1→v5 lineage.

## [Unreleased]

### Fixed
- **Every knowledge-graph edge claimed a provenance it could not resolve.** `kg_edges.source_kind`
  is `NOT NULL DEFAULT 'observation'` and is the sole input to the memory write-admission trust
  score, where `observation` scores 1.0 — yet every edge the sensorium ingest path wrote carried
  that claim with `source_id = NULL`, so *which* observation was unanswerable. Edges derived from
  the cluster watch now cite the apiserver's `uid` + `resourceVersion` for the exact object version
  behind the fact. An observation with no identity still writes its edges, with no citation rather
  than a synthetic one — a reference that looks resolvable and is not would be worse than a blank.
- **A chat client could claim sensor provenance and bypass the whole memory-poisoning guard.**
  `MEMORY_SECURITY_HARDENING`'s write-admission guard treats provenance as its primary, non-LLM
  validator: at trust ≥ 0.9 a write is admitted as `sensor_trusted` *before* the injection-signature
  check, the rate limiter and the contradiction check run. The cortex `remember` node derived that
  provenance from `state["user_id"]`, which is `body.user` — a free-form field of the chat request —
  so a caller who sent `{"user": "watchtower"}` had their episode stored as detector-derived at trust
  1.0 with every validator skipped, which is precisely the MINJA query-only attack the guard exists
  to stop. Provenance is now a separate turn-state field (`trigger_source`) that only an in-process
  caller can set, applied *after* the `extra_state` merge so no caller-supplied key can override it;
  the watchtower asks for `detector` explicitly and the HTTP endpoint cannot pass it at all.
  Both flags are off by default, so no default deployment was exposed.
- **A dropped SSE frame was indistinguishable from a frame the server never sent.** Both
  `kube-q` SSE parsers swallowed any frame they could not decode, and neither could see a stream
  that ended mid-frame. They now count losses into an optional `SseStats` without changing what
  they yield, and the chat stream warns that an answer may be incomplete instead of silently
  presenting a partial one as whole. See the `kube-q` changelog.
- **A stored detector that can never fire was still loaded, and could still be promoted.** The
  liveness check added for natural-language authoring guarded one door; `memory/consolidation.py`
  writes to the `detectors` table as well, `promote_candidate` only flipped `status` without
  re-reading the predicate, and any row written before that gate existed was still in the table.
  Three dead rows — #114's spaced alternation, a `kind` the engine never matches, and a `Pod`
  predicate with no `status_regex` — loaded as two active and one shadow detector. The shadow
  case is the damaging one: shadow detectors are promoted on their precision record, and a
  detector that cannot fire shows zero firings, which is indistinguishable from a condition that
  never occurred. `load_db_detectors` now drops such a row with a warning naming it and the
  reason (per row — one bad candidate cannot cost the cluster its other detectors), and
  `POST /v1/detectors/{name}/promote` answers **409** with the reason instead of a cheerful
  `status: active`. A store read failure is still not treated as evidence of deadness, and a
  missing detector is still a 404; demotion is never blocked.
- **A mistyped `triggers:` key silently removed a playbook from the router.** `Trigger` reads
  exactly `pod_status_regex`, `event_reason_regex` and `event_message_regex` via `raw.get`, so
  a near miss — `reason_regex:` one level up, or a key one character short — compiled to a
  trigger holding nothing. The playbook still loaded, still counted toward the playbook total
  and still passed the schema check, while `match_playbooks` iterated it forever without ever
  matching; the `if not pb.triggers` guard did not fire either, because the tuple is non-empty.
  This is the router-side twin of #114's dead `detect:` predicate. The loader now warns with
  the playbook name and the offending keys, and `tests/test_every_playbook_is_reachable.py`
  turns it into a failure — it also checks every one of the 41 shipped trigger regexes actually
  routes to its own playbook, so the two streams cannot be crossed unnoticed. All 23 shipped
  playbooks were already reachable; nothing that fires today stops firing.
- **A natural-language-authored detector could be staged as a permanent no-op, and its zero
  firings would read as "the condition never occurred".** `validate_detect_block` documented
  itself as *"the compiler **is** the validator"* — but the compiler only proves a predicate is
  well-formed, never that it can match anything. Four shapes passed it with zero errors: a
  `kind` the engine does not handle, a `kind` in the wrong case (`matches()` is
  case-sensitive), a `Pod`/`Node` predicate with no `status_regex`, and a space inside an
  anchored alternation — #114's exact mistake, which a model writing regexes from prose makes
  more readily than a person reading the schema. A new `detectors/predicate_shape` module
  expands a pattern into every string it can produce and requires each to be a value a cluster
  actually emits (`message_regex` is exempt — an event message is prose; an unexpandable
  pattern is treated as *unknown*, not dead, so a valid-but-exotic regex still passes). The
  validator now returns those as errors, and the 30 shipped predicates are checked the same way
  in CI. Asserting mere satisfiability does **not** work here and was tried first: the sample is
  generated from the pattern, so the stray space rides along and the assertion passes.
- **`kq replay` printed a blank summary for every detector firing.** The hash-chained
  `decision_log` has two readers — `app/digest/postmortem.py`, which builds the incident
  narrative, and `kq replay`, which streams the record back to a terminal — and each turned a
  row into a line of text with its own code. The postmortem handled all eleven recorded kinds;
  the CLI matched seven *top-level field names*, so any row whose content lives elsewhere
  rendered as an empty cell: `finding` (a `findings:<cluster>` episode contains nothing else,
  so `kq replay findings:default` printed N rows of nothing while the detectors had certainly
  fired), `plan` (all in `steps`) and `ki_otel_span` (all in `attributes`). A blank cell does
  not read as *no summary available*; it reads as *this event carried nothing*, which is the
  one thing a tamper-evident log must never say falsely. The summariser now lives once, in
  `ki_protocol.record.summarise_record`, and both readers call it. Separately, the replay
  endpoint yielded the payload alone, so the `type` column depended on each payload happening
  to echo its own kind — every hand-written recorder call remembered to, but `otel_spans`
  never did, and those rows replayed as type `?`; the row's `kind` is now authoritative. A new
  suite reads *both* artefacts, so a kind nothing summarises fails a test instead of quietly
  rendering as an empty cell.
- **The two halves of `ki-protocol` did not agree, and the SDK dropped payloads because of it.**
  `ki_protocol.wire` (server emission models) and `ki_protocol.events` (the client's typed union
  and `parse_event`) are one contract whose own docstring says *"wire-format changes must update
  both modules together"* — a rule kept by discipline and by nothing else: no test in either suite
  imported both halves, so each was only ever checked against its own fixtures. Serialising every
  emission model and feeding it to `parse_event` showed five of the eight arriving stripped —
  `tool_result.output`, `tool_call.command`, `error.error` and all four `hitl_request` fields were
  dropped into models that declare different names for the same things, and `plan` (emitted since
  V2, and on every Cortex turn) was discarded entirely because the client union never carried it.
  A dropped payload is worse than a rejected frame: `summary=""` is the shape of a tool that
  returned nothing. `parse_event` now maps the wire's field names onto the client's, only where
  the client-side name is not already set, and `PlanEvent`/`PlanData` join the union. The `kq`
  REPL was never affected — it reads the raw frames — but `KubeQClient`/`AsyncKubeQClient`, the
  documented SDK entry points, were. A generative test now walks every model in `wire` and fails
  if one has no client counterpart.
- **The connection/identity refusal turned the v5 capability sandbox off.** Refusing every
  `--as` flag is right for one a model wrote; `app/tools/aci/sandbox.py` is not a caller
  choosing an identity but app code narrowing it to a ServiceAccount with strictly fewer
  rights — and it runs with `hitl_bypass=True` on exactly that basis. Measured, `run_as("get
  pods -n prod", "read-only")` returned `[Protected] '--as' is not permitted.` and reached
  kubectl not at all: the app-side gate given up, the cluster-side gate never applied, and a
  refusal string handed back where the caller expected command output. `run_kubectl` now
  accepts one impersonation token when the run config names that exact token
  (`sandbox_identity`, injected by the graph the way `hitl_bypass` and `user_role` are, never
  writable by a model); a different value, a second identity flag, or any other connection flag
  beside it is refused as before. The existing sandbox tests could not see the break — every
  case injects a `_runner`, so none of them crosses the seam into the real tool.

### Documentation
- **The architecture code map pointed at modules that do not exist.** `docs/architecture.md`
  carries the map a contributor or integrator reads instead of the source tree. Three of its 34
  `.py` entries named a module that exists nowhere under the server package — `endpoints/stream.py`,
  `endpoints/memory.py` and `db/memory.py` — and one endpoint annotation, `GET
  /v1/chat/stream/{session_id}`, named a route the server does not expose (cited twice: in the
  request-flow diagram and in the map). Following the page, an integrator would have opened an SSE
  connection to a path that 404s and read it as a server fault. In fact `POST /v1/chat/completions`
  **is** the stream — it returns a `StreamingResponse` with `media_type="text/event-stream"` — and
  the separate SSE route is `GET /v1/events/replay/{session_id}`, served by `events.py`; pinned
  context is `preferences.py`, and the store lives under `app/memory/`. Map and diagram corrected,
  and `db/` now lists the flight recorder it had omitted. 14 tests hold every module the map names
  and every `/v1` path any doc page cites to what the server actually has, with non-vacuity floors
  so a regex that stops matching fails instead of passing silently.

### Security
- **The "your setting did nothing" report could not say it about eleven of its own entries.**
  `UNWIRED_EXPERIMENTAL_FLAGS` (`app/core/version.py`) lists the v5 settings that are declared,
  documented and read by no code, and both its own comment and `docs/v5-experimental-flags.md` —
  a public page in the docs nav — promise that setting one surfaces it under
  `set_but_unwired_flags` in `GET /healthz`, `GET /v1/v5/status` and the startup line. But
  `set_but_unwired_flags()` was `_on_booleans() & UNWIRED_EXPERIMENTAL_FLAGS`, and `_on_booleans()`
  filters `isinstance(value, bool)` — while **11 of the 26 entries are `float` or `int`**. No value
  an operator could give them would ever reach that report. Measured 2026-08-20 with three knobs
  moved off their defaults next to one boolean as a control: `KI_V5_RIGHTSIZING=true` was reported
  by `/healthz` and `version_line()`; `KI_V5_AGENT_COST_RATE_CAP=0.10`,
  `KI_V5_SPEND_OUT_PRICE_PER_1K=0.99` and `KI_V5_DETECTOR_MIN_FIRINGS=3` were reported by nothing,
  anywhere. The cost cap is the one that matters — it reads as a spend brake, the page describes it
  as *"USD/min above this ⇒ runaway spend"*, and it was the quietest of the eleven. A knob now
  counts as set when it differs from its **declared default**, not when it is truthy:
  `KI_V5_AGENT_COST_RATE_CAP=0` is a deliberate instruction, and `KI_V5_STAGE_SIZE=1` is already
  the default. `active_experimental_flags()` is deliberately unchanged — a knob is configuration,
  not on/off runtime identity, which is a different question from "did what I set do anything".
  117 tests pin it, including a negative control over all 21 *wired* experimental knobs and a gate
  that walks every ⚠️ row on the public page and checks it can actually be surfaced.
- **The reporter that exists to catch guards protecting nothing had that exact hole, and it
  failed open.** `app/core/config_audit.py` was written so an operator who configures a
  protection is told when it does nothing; `GET /v1/v5/status` and `kq v5-status` carry the
  result. `autonomy_override_problems()` validated an entry's `=` and its *level* and never the
  namespace it names — but `level_for_namespace` is an exact dict hit on a lowercased key, so an
  entry whose namespace is not a real namespace name parses cleanly, stores a key nothing can
  match, and was reported as fine. Measured with `AUTONOMY_LEVEL=A3` and
  `AUTONOMY_NAMESPACE_LEVELS="prod-*=A0"` — a natural thing to write, because the sibling
  `AUTONOMY_A3_ALLOWLIST` *does* take globs and `docs/configuration.md` said so one line away:
  `unenforceable_guard_config()` returned `[]`, `level_for_namespace("prod-web")` returned **A3**
  rather than the pinned A0, and with `AUTONOMY_A3_ALLOWLIST="CrashLoopBackOff/prod-*"`,
  `a3_allowed("CrashLoopBackOff", "prod-web")` returned **True** — the watchtower would auto-fix
  in precisely the namespaces the operator believed were pinned to investigate-only. A glob, a
  `?`, a slash and an embedded space were all silent. `a3_allowlist_problems()` had the same class
  of gap (empty playbook, empty pattern, a second `/`), failing closed rather than open. Both now
  validate the whole entry, and the glob message names the setting where globs *do* work. The
  empty key is deliberately still not reported: `level_for_namespace("")` is the cluster-scoped
  lookup, so `=A0` really does pin cluster-scoped objects. 53 tests pin it, each asserting the
  underlying behaviour first so the report is never its own only evidence.
- **The break-glass page promised a stop button the product exposes nowhere.**
  `docs/autonomy.md#stopping-the-agent-break-glass` — the page an operator reads *during* an
  incident — said the two write brakes bind "without a redeploy" and listed the kill switch as
  engageable by "`KI_V5_KILL_SWITCH=true`, or the runtime toggle (no restart)". Measured across
  every operator surface: no API route matches kill/stop/freeze/brake in any of the 19 paths
  (`GET /v1/v5/status` reports the brakes and nothing sets one); no `kq` command engages one; the
  chart's ConfigMap is an explicit key allowlist with no `extraEnv` escape, so neither setting is
  reachable through Helm values; and `engage_kill_switch()` has no caller in `app/` outside
  `budget.py`. Settings are read once at process start — setting `KI_V5_KILL_SWITCH=true` in the
  environment of a running process leaves `kill_switch_engaged()` False. So engaging a brake
  required the restart the page said was unnecessary, and the in-process toggle sets a module
  global in one process: even wired to a route it would stop only the replica that served the
  request. The gates themselves were correct; the sentence was not. The page now documents what
  exists (`kubectl set env deploy/kubeintellect KI_V5_KILL_SWITCH=true`, confirm with
  `kq v5-status`) and states plainly which surfaces are missing and why the toggle is per-replica;
  `budget.py`'s module docstring carried the same claim and is corrected. 24 tests hold the claim
  to the mechanism — they re-permit it automatically if the toggle ever gets a production caller.
- **`GET /v1/v5/status` read a brake source directly instead of its reader.** `kill_switch_engaged`
  was reported through `kill_switch_engaged()`; `change_freeze` was reported as
  `settings.KI_V5_CHANGE_FREEZE`, bypassing the `change_freeze_active()` reader the write gates
  use. Behaviour is identical today — no caller injects freeze windows — so this is a consistency
  fix, not a live defect; it removes the second place a future freeze source could be honoured by
  the gates and not by the surface that reports them.
- **A declared change freeze stopped one of the two write gates.** `KI_V5_CHANGE_FREEZE` is an
  operator saying *stop* — `GET /v1/v5/status` reports it and `kq v5-status` prints it. Its sibling
  brake, the kill switch, is read through one `kill_switch_engaged()` that composes its two sources,
  so both write gates obey it. The change freeze had no such reader: `auto_write_permitted` (the
  watchtower's A3 path) read the settings flag, while `gate_write` (the ACI write chokepoint) read
  only an injected `(now_epoch, freeze_windows)` pair — which its one caller passes as neither. With
  `KI_V5_CHANGE_FREEZE=true` and nothing else set, `gate_write()` returned *allow* and
  `decide_write("kubectl scale …", earned_rung="L4")` returned **auto**, while the same settings
  denied on the kill switch. Both gates now read one `change_freeze_active()`. Scope, stated plainly:
  `decide_write`/`plan_mutation` have no production caller yet, so unlike the kill-switch defect this
  was latent rather than live — what was live is a status surface reporting a brake that half the
  gate surface did not implement. 45 tests pin it, including that patching the single reader moves
  both gates.
- **A `readonly` API key could grant itself `cluster-admin` via `kubectl auth reconcile`.** The
  verb logic is an allowlist — a verb is a write unless it is on the read-only list — so a verb
  wrongly *on* that list is an open door, not a missing rule. `auth` was on it because
  `kubectl auth can-i` and `auth whoami` ask questions, but `kubectl auth reconcile` **writes**:
  it creates and updates Roles, RoleBindings, ClusterRoles and ClusterRoleBindings from a
  manifest. Measured 2026-08-20 with a stubbed kubectl, a `readonly` key ran
  `kubectl auth reconcile -f -` carrying a ClusterRoleBinding to `cluster-admin` with no approval
  prompt, while `kubectl create -f -` with the identical manifest was refused. `auth` now sits in
  `_READ_ONLY_SUBCOMMANDS` alongside `rollout`, `config` and `certificate`, so `can-i` and
  `whoami` still read and everything else — including a subcommand a future kubectl adds — is a
  write. The two tables are additionally asserted disjoint, because a verb in both reads as a
  blanket read before its subcommand is consulted.
- **`kubectl cluster-info dump` returned the contents of every protected namespace.** Read-only
  against the cluster is not read-only against what may be read. `cluster-info dump` walks every
  namespace and prints pod specs, events and container logs; no namespace filter reaches it,
  because the verb names no resource type and both filters key off that. Measured 2026-08-20, a
  `readonly` key ran `kubectl cluster-info dump --all-namespaces` unfiltered. It is a concatenated
  dump with no per-object shape to filter, so it is now refused for every role on the same rule
  `-o custom-columns` is, with a message pointing at `-n <namespace>`. Bare `cluster-info` is
  untouched. 55 tests added across both fixes.
- **A boolean listed as a value flag made one shared parse swallow the verb — and every gate with
  it.** `_skip_flags` is the single walk that finds the verb for every gate in `run_kubectl`; it
  consults `_VALUE_FLAGS` to decide whether a flag consumes the token after it.
  `--warnings-as-errors` is a boolean (pflag accepts it bare) and it was in that table, so the
  walk consumed the verb as its value. Measured 2026-08-20 with `hitl_bypass` on (an
  `auto_approve` session or "approve all"): `kubectl --warnings-as-errors get secrets -n prod` and
  `... get sa -n prod` **ran and returned credential rows** where the unprefixed forms are
  refused, and `kubectl --warnings-as-errors delete namespace shop` ran with **no always-confirm
  prompt**. At the gate level the verb read as `secrets` / `namespace` / `image`, so
  `_extract_resource_type` returned `None` (nothing to compare against the blocklist),
  `_classify_risk` fell from `high` to `medium`, and `_requires_always_confirm` returned `False`.
  Routing every gate through one parse — the fix for four earlier defects in this family — is what
  made a single wrong table row move all of them at once. `--warnings-as-errors` is removed from
  the table, a `_BOOLEAN_GLOBAL_FLAGS` set records kubectl's boolean globals, the two are asserted
  disjoint, and a command corpus asserts that a flag carrying no meaning about the request changes
  no gate's answer. 84 tests added.
- **`helm get manifest` stripped the release's Secrets — unless you wrote `-n` before the verb.**
  `run_helm` removes protected kinds from rendered manifests because `helm get manifest` returns
  a release's own `kind: Secret` objects with their base64 `data:` intact, which is exactly what
  `kubectl get secret` is refused for under every role. It decided when to strip from the first
  non-flag token in `tokens[2:]`, so any global flag before the verb put its *value* there:
  measured 2026-08-20, `helm -n prod get manifest shop`, `helm --namespace prod get manifest
  shop` and `helm -n prod get all shop` all returned the base64 password in full, while
  `helm get manifest shop -n prod` stripped it. `helm get hooks` renders manifests too and was
  never on the enumerated list at all. Stripping now runs on **every** `helm get` — the decision
  is removed rather than the parse repaired — and `_extract_verb`'s flag walk is a shared
  `_skip_flags()` helper, matching `run_kubectl`.
- **A quoted or comment-suffixed `kind:` kept a Secret in a Helm manifest.** The same stripper
  matched the kind as a bare token to end-of-line (`^kind:\s*([A-Za-z0-9.-]+)\s*$`), so
  `kind: "Secret"` and `kind: Secret  # managed by the platform team` — both ordinary YAML a
  chart can render — failed to match and the document was returned with its `data:` block. Quotes
  and trailing comments are now part of the line, not part of the value. 40 tests added across
  both fixes.
- **The namespace filter handled `-o jsonpath` by splitting the output on spaces — that is one
  jsonpath.** A bare `kubectl get namespaces` is allowed *because* the protected entries are
  stripped from the answer. For `-o jsonpath` the filter dropped whitespace-separated tokens
  equal to a blocked name, which works only for the expression that prints bare names separated
  by spaces. jsonpath prints whatever the caller asks for: measured 2026-08-20 against the
  default blocklist, `{range .items[*]}{.metadata.name}{","}{end}` returned
  `default,kube-system,monitoring,` and the `=`/`:` variants returned `kube-system=Active` — in
  full, **with no withheld note**, so the answer looked complete. The name was still there, it
  was simply no longer a whole token, and there is no separator jsonpath cannot produce.
  `-o custom-columns` and `-o go-template` were already refused for exactly this reason, and the
  `--all-namespaces` sibling already refused jsonpath too — two functions doing one job gave two
  answers for one format. The branch is removed rather than patched, so jsonpath now falls to the
  same `_FIXED_SHAPE_FORMATS` allowlist as every other caller-shaped format. Table, `-o wide`,
  `-o name`, `-o json`, `-o yaml` and `describe` are filtered exactly as before. 25 tests added.
- **The protected-namespace guard read one name, in one of the two places kubectl puts it.**
  `_targeted_namespace` exists because an infrastructure namespace can be a command's
  positional target rather than its `-n` value, and the documented rule is a hard refusal
  including reads. It took the first non-flag token *after* the resource kind — so it missed
  the `resource/name` shorthand its own sibling `_extract_resource_type` documents and handles,
  and it missed every name after the first. Measured 2026-08-20 against the default blocklist:
  `kubectl delete ns/kube-system`, `kubectl delete namespace/kube-system`,
  `kubectl delete ns shop kube-system` and the **ungated read** `kubectl get ns/kube-system`
  all ran; only `kubectl delete ns kube-system` was refused. Now `_targeted_namespaces()`
  returns every name in both spellings and the guard tests all of them, while
  `kubectl delete ns tenant-a tenant-b` stays a normal HITL-gated operation. Its remaining
  `args.index(verb)` went too — `_operand_index()` is the one parse the verb, the resource
  type, the always-confirm gate and this guard now share. 40 tests added.
- **One flag between the verb and its target turned off the gate that cannot be turned off.**
  `_requires_always_confirm` is the only gate in `run_kubectl` that fires *through*
  `hitl_bypass` — cascading deletes (`namespace`, `pv`, `crd`) and live workload mutations
  (`set image`, `set resources`) prompt the human even on an auto-approve session, because
  none has a rollback path. It read its target as the fixed index `args[2]`, which is the
  operand only when the command is written verb-first with nothing in between. Measured
  2026-08-20: `kubectl delete -n prod namespace shop`, `kubectl -n prod delete namespace
  shop`, `kubectl delete --force namespace shop`, `kubectl delete --ignore-not-found pv
  my-volume` and `kubectl -n prod set image deploy/api api=nginx` all returned `False` — so
  the *natural* way to write a cascading namespace deletion executed with no prompt while the
  awkward way stopped and asked. `drain` was never affected (matched on the verb alone). The
  same positional trap had already been fixed in both sibling parsers; this one was missed, so
  all three now share one `_operand_after_verb()` helper. That helper also removes a second
  assumption in `_extract_resource_type`, which located the verb with `args.index(verb)` — the
  *first* place the verb string appears, which a flag value can take: in a namespace named
  `get`, `kubectl --namespace get get secrets` read the resource as `get` and missed the
  Secret block. 54 tests added.
- **"approve all" bypassed HITL for one turn; four documentation surfaces said "for the rest
  of the session".** `stream_events` rebuilds its run config on every call, and `hitl_bypass`
  comes from `auto_approve` — the request body (`kq --auto-approve`) or the *current* message.
  Nothing persists it, and the `kq` REPL does not latch it either. Measured over three turns:
  `"approve all"` → bypass on, then the next two turns → bypass off, gated again. Meanwhile
  `docs/security.md` said *"session-wide bypass"*, `docs/examples.md` and `docs/cli-reference.md`
  said *"for the rest of a session"*, `docs/api-reference.md` said *"for the rest of the
  session"*, and the log line announced *"HITL bypass enabled for session=…"*. The gap is in the
  **safe** direction — the gate stays on — so the behaviour is left alone and the claim is
  corrected everywhere, with `tests/test_approve_all_is_one_turn.py` pinning what actually
  happens. Whether the bypass *should* span a session is an owner decision: widening the
  product's central safety gate is not a side effect of fixing a sentence.
- **The secret redactor was a YAML redactor, and reported itself applied to any text.**
  Everything in `app/utils/redact.py` is line-aware — `key: value` matching, following a
  credential key onto the lines its value occupies, recognising keys that are secrets by
  convention — and every one of those rules was written against the shape `kubectl -o yaml`
  produces. `kubectl -o json`, an equally ordinary read and the form the API itself returns,
  writes quoted keys, which `_LINE_RE` did not match at all; both lines of the Kubernetes env
  idiom fell through to the free-text branch. Measured on the same object: `-o yaml` gave
  `value: <redacted>`, `-o json` stored `"value": "hunter2-prod-db"`, and a Secret's `tls.key`
  went the same way. Whether a credential was caught depended on the `-o` flag the caller
  happened to pass. The parser now captures the key's quote (back-referenced, so an opening
  quote requires a closing one) and `_unwrap_value` gives the value's punctuation back to the
  emitter — so a redacted JSON document is **still valid JSON**, keys and non-secret values
  intact, rather than mangled text. A new suite renders the same objects both ways and asserts
  neither leaks.
- **The flight recorder's secret scrubber walked one level of the payload and reported itself
  applied.** `REFLEXION_REDACT_SECRETS` (on by default) is documented as *apply secret/URL/token
  scrubbing before persisting*, and `flight_recorder._scrub` said *redact secrets from string
  fields*. What it did was iterate `payload.items()` and redact the values that happened to be
  strings — anything inside a list or a nested object went into the hash-chained `decision_log`
  verbatim. Measured: `{"attributes": {"ki.action": "kubectl … --token=AKIA…"}}` kept the token,
  and so did a `plan`'s `steps`. Which payloads were covered was an accident of how each call
  site shaped its dict; `rollback_point.pre_state` is safe only because `kubectl_tool` redacts
  every capture itself before handing it over. The scrubber now walks dicts and lists to a bound
  of six levels. The 1,500-character cap deliberately keeps its current reach — top-level fields
  only — because a nested string arrives already capped by its producer, and re-capping a
  rollback pre-state at 1,500 would cost that capture its restorability to enforce a limit
  nothing asked for. `docs/data-handling.md`, which had stated the old limit accurately, is
  updated.
- **A rollback capture whose only redaction was a private key was described as, in full,
  "redacted".** `kubectl_tool._capture_note` tells the operator what redaction did to a
  pre-state capture, and it counted three of the six markers `redact_secrets` can emit — a
  hand-copied subset. A PEM block (`<redacted-pem-block>`) and a Secret's `data:` block
  (`<redacted-block>`) were not among them, so the most secret-dense objects there are produced
  the least informative note. The vocabulary now lives once, as `redact.REDACTION_MARKERS` +
  `count_redactions`, and a test reads the tuple against the literals in `redact.py`'s own
  source so a new marker cannot be added without joining it.
- **The cluster snapshot pasted into every prompt was a second, unguarded kubectl.**
  `context_fetcher` pre-fetches `kubectl get pods --all-namespaces` and
  `kubectl get events --all-namespaces --field-selector=type=Warning` through its own
  `subprocess.run`, not through `run_kubectl`, and enforced none of that tool's policy. The
  identical command *through* the tool has its blocked-namespace rows removed; the snapshot
  pasted the whole table into the coordinator's system prompt on every turn, warning `MESSAGE`
  column included — which is where a `FailedMount` event names the secret it could not find and
  a failing probe names the apiserver's address. `snapshot_pod_count` counted those pods too, so
  the model was told a number it could never reproduce with a tool call. Worse, the same executor
  is handed `-n <namespace>` built from the `TARGETED:` line the **model** writes: `run_kubectl`
  refuses `describe pod etcd-control-plane -n kube-system`, while the same read through the
  snapshot path returned the full description — environment variable names, mounted certificate
  paths — into the prompt. The blocklist and the connection/identity flag family are now enforced
  at the one place the subprocess is launched, cluster-wide tables are row-filtered with the same
  `namespace_guard` helper `run_kubectl` uses, and a filtered listing says so rather than
  reading as a complete one.
- **Detector findings carried raw Kubernetes event text out of the blocked namespaces.** The
  sensorium runs `kubectl get pods -A --watch` and `kubectl get events -A --watch` as raw
  subprocesses, not through `run_kubectl`, and `app/sensorium/` has no reference to the namespace
  blocklist. That is deliberate — a watchtower that cannot see the infrastructure namespaces
  cannot tell a quiet cluster from an unwatched one — but the free text came with it: a finding
  in `kube-system`, `kubeintellect` or `monitoring` carried up to 140 characters of raw event
  message (`MountVolume.SetUp failed … secret "kubeintellect-secrets" not found …`), and
  `GET /v1/findings` returns it to every caller with no role parameter at all. An event `message`
  is arbitrary cluster text; every other field a finding carries is an enum or an object name.
  The message is now withheld for a blocked namespace and everything else is kept, so the
  operator still learns that coredns is crash-looping. Only `pod_status` observations reach the
  knowledge graph, so no event text was stored there — verified, not assumed. See
  `tests/test_the_watch_channel_respects_the_blocklist.py`.

### Documentation

- **Corrected the RBAC table in `docs/security.md`.** It stated that infrastructure-namespace
  access, reads included, is blocked for admin, operator and readonly. True of every agent tool;
  never true of the sensorium, which is cluster-wide by design. The table now carries an explicit
  row for detector findings from those namespaces instead of leaving it to be discovered.

- **`kubectl get pods --server=http://attacker.example.com -A` ran, and so did `--as=system:masters`.**
  Every gate in `run_kubectl` and `run_helm` reasons about *what* is being asked — the verb, the
  resource, the namespace, the role. Nothing looked at *which cluster the command talks to* or
  *under whose identity*, and nothing rejected the flags that decide it. Measured by capturing
  the argv that reaches `subprocess.run`, `--as`, `--as-group`, `--server`, `--kubeconfig`,
  `--context`, `--token`, `--insecure-skip-tls-verify` and Helm's `--kube-as-user`,
  `--kube-apiserver`, `--kube-token`, `--kube-context` all executed byte-for-byte on the plain
  read path with no role required. Impersonation still needed the ServiceAccount to hold
  `impersonate`, which the shipped chart does not grant — it failed closed *at the API server*,
  not in-app, and the chart offers `rbac.clusterAdmin: true` under which it would have worked.
  Redirection needs no cluster permission at all: the response is then whatever that endpoint
  returns, handed to the model as cluster truth, with the namespace filters reporting how much
  they withheld from attacker-supplied text. Both tools now refuse the connection/identity family
  — refused, not stripped, since silently dropping a flag answers a different question than the
  one asked. The `--as…` and `--kube-*` families are matched by prefix. See
  `tests/test_the_cluster_and_identity_are_not_arguments.py`.

- **`kubectl get pods -A -o custom-columns=...` returned the protected namespaces' rows.** Both
  namespace filters ended in a branch that assumes a kubectl table with NAMESPACE (or NAME) as
  the first column. They refused `-o name` and `-o jsonpath` *by name* — a deny-list of the two
  formats someone thought of — and let everything else reach that branch. `-o custom-columns`,
  `-o go-template`, `-o template` and their `-file` variants render whatever the caller asked for
  in whatever order, so the assumption is false: measured through the real tool, the `kube-system`
  and `monitoring` rows came back whole and unannotated, from `get pods -A` and from
  `get namespaces` alike. The same command with `NAME` in the *first* column was filtered
  correctly, which is what makes it an assumption rather than a check. Inverted to an
  **allowlist** of the shapes kubectl itself decides (`""`, `wide`, `json`, `yaml`), so an
  unanticipated format fails closed; a structured payload that is not a list of items now fails
  closed too. The tool's own parse-error message, which advised using `-o custom-columns`, now
  points at `-o json`. See `tests/test_only_a_shape_kubectl_chose_can_be_filtered.py`.

- **`query_prometheus` printed `= N/A` over live metrics, and crashed on `scalar(...)`.** The
  tool discarded Prometheus's `resultType` and chose its renderer from `range_minutes` — the
  *caller's* argument. An instant query whose expression carries a range selector
  (`container_cpu_usage_seconds_total{...}[5m]`; the tool's own docstring examples use
  `rate(x[5m])`) comes back as a **matrix**, whose entries have `values`, not `value` — so every
  series rendered `= N/A`, which is the shape of *no data*, over samples that were right there.
  A `scalar`/`string` result is a bare `[timestamp, "value"]` pair rather than a list of series,
  so it reached the namespace filter and raised `AttributeError: 'int' object has no attribute
  'get'` straight out of the tool — the guard was what destroyed the answer. Fixed: dispatch on
  `resultType`, render scalars and strings, never hand a non-mapping to the filter, and have
  `series_labels()` return `{}` for one instead of raising. `query_prometheus_series` (the
  detector entry point) now reports an unprojectable shape as an error rather than an empty
  list, so "no data" keeps meaning one thing. Detector paths were checked and were never
  affected. See `tests/test_prometheus_renders_what_it_was_sent.py`.

- **`query_loki` returned `kube-system` metric series with the namespace blocklist switched
  off.** The guard was applied to the wrong key on most metric queries. The tool decided log-vs-
  metric by testing whether the LogQL text *starts with* one of eight function names, and that
  guess also chose where the filter looked for labels — `stream` for logs, `metric` for metrics.
  A Loki **matrix** response has no `stream` key, so a misclassified metric query filtered
  against `{}`, `""` is in no blocklist, and every series passed. Seven of ten ordinary metric
  expressions failed the test, including `sum by (namespace) (rate({app="web"}[5m]))` — a space
  after `sum`, not a parenthesis. The answer came back with the blocked series present, no labels
  printed, and no notice that anything had been filtered. Fixed twice over, since either repair
  alone closes it: rendering and filtering now follow Loki's own `data.resultType` (it had
  already said what it returned), and `series_labels()` consults every known label container so a
  wrong hint cannot disable the guard. The classification survives only to choose request
  parameters. See `tests/test_loki_namespace_filter_survives_a_misroute.py`.

- **`kubectl logs … | grep -A 3 Traceback` answered "(no matching lines)" for a log containing
  the traceback.** `run_kubectl` reimplements `grep` in Python for its `|` support — a
  documented defence layer that had **no tests at all**. The parser skipped every token starting
  with `-` that was not `-v`, `-i` or `-E`, which produced two silently wrong answers, both
  measured against this machine's `/usr/bin/grep`:
  a **value-taking flag left its value in the pattern** (`grep -A 3 Traceback` searched for
  `"3 Traceback"` and returned nothing where real grep returns five lines — and `-A`/`-B`/`-C`
  is *the* idiom for reading a stack trace out of a log, so the agent was told the traceback in
  front of it did not exist); and **combined short flags vanished** (`-iv` matches neither `-i`
  nor `-v`, so `grep -iv info` ran as `grep info` and returned the exact **complement** of the
  requested set). `-c`, `-l`, `-q`, `-m`, `-o` were likewise ignored rather than honoured.
  The emulator now parses grep's arguments the way grep does — short clusters, attached values
  (`-A3`), `--flag=value`, `--` — and implements `-v -i -E -F -w -x -c -n -o -s -a -A -B -C -m
  -e`. **Anything it does not implement is named and refused**, which is the rule the module's
  own docstring already stated for unsupported *commands*. Correctness is held by a differential
  test that runs every supported combination through both implementations and compares byte for
  byte. `tests/test_pipe_grep_matches_real_grep.py`.
- **A filtered listing and a complete listing were the same bytes.** The namespace blocklist
  enforces itself two ways — a *refusal* (`kubectl get ns monitoring` → `[Protected] …`) and a
  *filter* (rows removed from a listing). The refusal is impossible to miss; the filter was
  silent on five of its six paths. Measured 2026-08-20: `kubectl get namespaces` dropped 3 rows,
  `kubectl get ns -o name` 2, `kubectl get ns -o json` 2, `kubectl describe namespaces` 2 and
  `helm list -A` 2 — none of them said anything. Only `kubectl get pods -A` appended a notice.
  The consequence is a false statement about the cluster: an agent asked whether the
  `monitoring` namespace exists runs `kubectl get namespaces`, receives a list it has no way to
  know is short, and answers **no**. Every filter now says what it withheld, and the note says
  *"This listing is NOT the complete set."*
  The sixth path told the truth and broke the format doing it: the `-A` filter appended its
  `[Protected]` sentence *after* `json.dumps`, so `kubectl get pods -A -o json` returned output
  that `json.loads` rejects with `Extra data` — a pre-existing defect an existing test had
  worked around by parsing only `out.split("\n[Protected]")[0]`. Structured output now carries
  the notice as a `withheldByPolicy` field inside the document.
  `kubectl_tool` also carried a byte-identical private copy of the notice helper; there is now
  one wording, in `namespace_guard`, for kubectl, Helm and the observability tools alike.
  **One documented limit**: `helm list -A -o json` is a bare JSON array — no field can hold the
  notice and nothing may follow it without making the payload unparseable. That case is logged
  server-side and asserted as a limit rather than left to be discovered.
  `tests/test_a_filtered_listing_says_so.py`.
- **The secret redactor deleted the label and kept the credential.** `redact_secrets` is the
  single funnel every stored artefact passes through — rollback captures, mutation captures,
  episode summaries and root causes, preferences, flight-recorder payload fields. It classified
  each line independently and dropped any line containing a keyword. YAML puts the name of a
  thing and its value on *different* lines, so measured against a plain Deployment:
  `- name: DB_PASSWORD` was dropped as `# <redacted-line>` and `value: hunter2-prod-db` was
  **kept verbatim**. The stored record was worse than an unredacted one — the credential
  survived and the only occurrence of the word "password" did not, so the review procedure the
  module's own docstring prescribes (*grep stored data for patterns we missed*) returned nothing
  on a record that was leaking. A `tls.key: |` block scalar stored its entire PEM body for the
  same reason plus two more: `tls.key` contains no keyword, and
  `-----BEGIN RSA PRIVATE KEY-----` does not match `private_key`.
  Redaction is now line-*aware*: the key is kept and the value replaced, a credential key is
  followed onto the lines its value actually occupies (block scalars and the `name:`/`value:`
  pair Kubernetes uses for env vars), PEM armour is redacted wherever it appears, and keys that
  are credentials by convention with no keyword in them (`tls.key`, `.dockerconfigjson`,
  `id_rsa`) are recognised. Structural fields such as `kind:` now survive, because a type name
  is not a credential and deleting it was the wrong half to delete.
  Two limits are asserted rather than left to be discovered: an unlabelled value (`foo: hunter2`)
  is kept, and a base64 blob embedded mid-line survives unless the whole value is base64 —
  widening the token pattern across `+` and `/` would redact filesystem paths that diagnostics
  need. This guards what is **stored**; it is not applied to the prompt sent to the model
  provider. `tests/test_redaction_keeps_the_label_not_the_secret.py`.
- **`KUBECTL_BLOCKED_RESOURCES="ConfigMap"` — the spelling Kubernetes itself uses for `kind:` —
  blocked nothing at all.** The shipped defaults are kubectl's lowercase plural
  (`secret,secrets,serviceaccount,serviceaccounts`), and every comparison folded only the
  command line, so an operator extending the list had to guess both the case *and* the number
  the code expected. Measured 2026-08-20 against the real `_check_protected_access`: with
  `ConfigMap` configured, `get configmap`, `get ConfigMap`, `get configmaps` and `get cm` were
  **all allowed**; with the lowercase singular `configmap`, `get configmaps` was still allowed.
  The configured side is now case-folded in `Settings.kubectl_blocked_resources` and expanded
  across singular and plural in `kubectl_tool._blocked_resources()` with the `-es`/`-ies` rules
  Kubernetes resource names actually follow (`ingress` ⇒ `ingresses`, `networkpolicy` ⇒
  `networkpolicies`, not the naive `+ "s"` that produces `ingres`); `helm_tool`'s manifest
  stripping reads the same expansion, so the two tools cannot disagree. An entry that can never
  match a resource type is reported through `config_audit` / `GET /v1/v5/status` / `kq v5-status`
  like any other unenforceable guard setting.
  **The credential floor was never affected** — `ALWAYS_BLOCKED_RESOURCES` is re-added
  unconditionally, and `get secrets` was measured blocked in every configuration tried.
  **kubectl short names are still not derived**: they come from API discovery, not from the
  string, so blocking `configmaps` does not block `cm`. That limit is asserted by a test rather
  than left to be discovered. `tests/test_blocked_resources_spelling.py`.
- **One capital letter in `KUBECTL_BLOCKED_NAMESPACES` disabled every namespace guard, silently.**
  Eight comparison sites across `kubectl_tool`, `helm_tool` and `namespace_guard` read
  `<value>.lower() in blocked` — the normalisation was applied to one side only, and
  `Settings.kubectl_blocked_namespaces` kept whatever case the operator typed. Measured
  2026-08-20 with `KUBECTL_BLOCKED_NAMESPACES="Kube-System"`, against the real guards:
  `kubectl get pods -A` returned the two `kube-system` rows the filter exists to remove;
  `kubectl delete deployment coredns -n kube-system` was **allowed**, where it is normally a
  `[Protected]` refusal at every role; the Loki/Prometheus query guard passed a
  `{namespace="kube-system"}` selector straight through; and the autonomy ladder returned `A1`
  where protected namespaces are meant to be pinned to `A0`. Nothing logged, nothing errored —
  the configuration *looked* correct. `ladder._normalise` even carried the docstring *"Match how
  the kubectl tool compares namespaces, so the two cannot disagree"*: it folded the namespace
  under test but not the set, and the kubectl gate folded neither. The blocklist is now folded in
  `config.py` (Kubernetes namespace names are RFC 1123 labels, so folding can only ever add
  protection), the command-line side is folded too — an LLM-written `-nKUBE-SYSTEM` no longer
  slips past — and a test asserts the property directly: a namespace is pinned to `A0` exactly
  when the kubectl gate refuses it.
- **Every guard setting is a comma-separated string whose parser discards silently.** What
  case-folding cannot repair is now reported instead of vanishing: a `KUBECTL_BLOCKED_NAMESPACES`
  entry that is not a legal namespace name (`kube-*` — a glob `AUTONOMY_A3_ALLOWLIST` supports and
  this setting does not — or `ingress/nginx`, or anything over 63 characters); an
  `AUTONOMY_NAMESPACE_LEVELS` entry the parser drops, which fails **open** by leaving the
  namespace on the permissive default the override existed to tighten; and a malformed
  `AUTONOMY_A3_ALLOWLIST` entry, which fails closed. New `app/core/config_audit.py` logs each at
  startup as `guard_config_unenforceable` and `GET /v1/v5/status` returns them under
  `unenforceable_guard_config`, rendered by `kq v5-status` — the same treatment
  `set_but_unwired_flags` already gives a switch that does nothing, one level down. Never fatal:
  an operator typo must not take the agent offline, only become impossible to miss.
- **The morning digest called a window quiet that `kq findings` refused to call clear.** Two
  surfaces answer the same question about the same window — `GET /v1/findings` (rendered by
  `kq findings`) and the digest (`kq digest`) — and each computed the answer itself. The digest
  validated its *recording* sources (recorder flag, SQLite mode, watchtower flag, pool, and both
  queries) and never asked whether anything had been **looking**. Measured 2026-08-20 with a stub
  Postgres pool answering every query truthfully with zero rows, all recorder flags on, and no
  watch stream connected: `/v1/findings` returned `{"sensorium": "starting", "findings": []}` and
  `kq findings` printed *"Sensorium is not watching … an empty result here does NOT mean the
  cluster is healthy"*, while `kq digest` over the same window printed **"Quiet watch: no findings
  in the last 24h."** The same held with `SENSORIUM_ENABLED=false`, where no detector could ever
  have fired. A flawless empty record and an empty cluster read identically. The classification
  now lives once in `app/detectors/perception.py` (`perception_state` / `perception_gaps`) and both
  surfaces read it, so they cannot answer differently; a disconnected watch stream or a blind
  predictive layer is a `degraded_reason` like any unreadable source, and the digest leads with
  *"Digest INCOMPLETE … This is NOT a quiet watch"*. Stated in the docs rather than implied: stream
  health is the state **now**, not a history of the window, so the reported gaps are a lower bound
  on the blindness in it.
- **The anticipatory detectors reported an all-clear that a connection refusal produced.** The
  layer whose entire job is to warn *before* a failure had two ways to go quiet without saying so.
  `query_prometheus_range_raw` returned only the series from `_query_raw` and **discarded the error
  string**, so every caller saw an unreachable Prometheus as an empty result set. Measured
  2026-08-20 against a real closed TCP port (`PROMETHEUS_URL=http://127.0.0.1:9`): the agentic/GPU
  collector's `_default_scalar("…sandbox_escape_attempts…")` returned **0.0** — the value that
  means *no escape attempts were observed* — and `collect_and_detect()` returned **`[]`**, which
  the caller reads as "clear". Same result with `PROMETHEUS_URL` unset. An instrument that reports
  `0` when it cannot read is worse than one that reports nothing: zero is an observation, and this
  one was never made. `_default_scalar` now returns `None` on a query error and the collector emits
  a `metrics-unavailable` warning hit naming how many of the seven agent/GPU signals it could not
  read, so the detectors say *blind*, not *clear*. Note this reverses a contract that a test had
  written down as intended behaviour (`test_scalar_exception_safe`, "verify the default path
  swallows errors → 0 → no hit"); it is renamed and inverted, and flagged for review.
- The detector engine had the same hole one level up, plus a guard that could never fire.
  `evaluate_trends` logged `trend_query_error` from an `except` block — but `_query_raw` returns
  its errors and does not raise, so a Prometheus outage could never reach that handler; the
  documented warning had never been emitted by the failure it exists for. `evaluate_trends` now
  reads `query_prometheus_series` (series, error) and, on an error, records `trend_blind_since` /
  `last_trend_error` and logs `trend_query_unavailable` instead of evaluating a trend against an
  empty series.
- `GET /v1/findings` now carries `predictive` (`active` / `blind` / `off`), `predictive_detectors`
  and `predictive_error` alongside `sensorium`. The two are independent claims — a connected watch
  stream says nothing about whether Prometheus answered — and `kq findings` no longer prints the
  green *"No findings · N detectors watching"* line while predictive detection is blind.
- **Two of the project's own refusals came back from a read verb as cluster state — one of them as
  a clean bill of health.** `_run`, the shared tail of all four ACI read verbs, separates "here is
  the cluster" from "here is why I could not look" purely by reading `run_kubectl`'s string, and it
  did so with a hand-kept marker list (`"blocked protected"`, `"requires confirmation"`, `"HITL"`,
  `"not permitted"`) plus `lowered.startswith("error")`. Measured 2026-08-20 against the real
  `run_kubectl`: `[Error] kubectl is not installed or not found in PATH…` returned **ok=True** with
  the error text as the body the model reads as cluster state, and `_health_from` found the word
  "error" in it and reported the target as **FAILED** — a verdict about a workload derived from a
  missing binary. `startswith("error")` never fired, because the string starts with `[`. Worse,
  `[Unsupported] 'kubectl edit' requires an interactive terminal which is not available…` also
  returned ok=True and read as **CURRENT**, because the phrase *"is not available"* contains
  "available" — a refusal reported as healthy. The three `[Protected]` refusals were caught only
  because their wording happens to contain "not permitted": a coincidence of phrasing, not a check.
  `_run` now classifies the string once through the shared `kubectl_output` reader, so a refusal or
  an error is always `ok=False` with the text in `error` and never in `body`. Separately,
  `_health_from` matched its status keywords as substrings anywhere in the body, so a Deployment
  named `error-budget-exporter` read as FAILED and a `crashloop-detector` as crashlooping; health
  words are now matched as whole whitespace-separated fields (splitting on whitespace only — a
  hyphen is part of a Kubernetes name, and splitting on it is what turned `error-budget-exporter`
  into the word "error").

- **The capability sandbox ran unbounded commands when it did not recognise the role — with the
  approval gate already switched off.** `run_as` (v5 P3 two-axis sandbox) executes with
  `hitl_bypass=True`: the app-level HITL prompt is deliberately given up there, on the stated
  grounds that the impersonated ServiceAccount's RBAC is the real guard. That trade only holds
  while the impersonation is definitely applied. It was not — an unrecognised role produced no
  flags, `as_impersonated` documented itself as a "no-op", and `run_as` executed the command
  anyway, unimpersonated, returning an ordinary output string that said nothing about it. Measured
  2026-08-20: `run_as("delete deployment web -n prod", "typo")` sent exactly
  `delete deployment web -n prod` to the seam, and against the real `run_kubectl` that command runs
  through to execution under `hitl_bypass=True` while the same command with `hitl_bypass=False`
  stops at the approval interrupt — so both guards were off at once, each because the other was
  presumed present. The role vocabulary makes it reachable by accident rather than by malice: this
  module's roles are `read-only` / `namespace-write` / `never-cluster-admin`, while the API-key
  roles used everywhere else in the codebase are `readonly` / `operator` / `admin` / `superadmin`,
  so passing `"readonly"` — the spelling the rest of the project uses — silently disabled the
  sandbox. A second hole: the command could bring its **own** identity. Real kubectl (v1.36.3,
  `kubectl options`) documents `--as-group=[]` as *"Group to impersonate for the operation, this
  flag can be repeated"*, so a command carrying `--as-group=system:masters` defeats the
  never-cluster-admin property whatever ServiceAccount the appended `--as` flag names. `run_as` now
  raises `SandboxContractError` — running nothing — for an unknown role, for a command that sets
  any of `--as` / `--as-group` / `--as-uid` itself (matched as whole tokens, so a label value like
  `team=--as-group` is not one), and as a last check if the impersonation flag is somehow absent
  from what would be executed. The pure flag builders keep their behaviour: `impersonation_args`
  still returns `""` for an unknown role, which is honest — there are no flags — and `run_as` is
  the seam that refuses to act on it.

- **The pre-apply validation gate reported "would apply cleanly" for commands the API server never
  saw — and could switch its own server-side check off.** `validate_mutation` (v5 P3 chokepoint) is
  the third link of validate → apply → verify, and it read `run_kubectl`'s prose the same way the
  other two did: `ok = not admission and "error" not in output.lower()`. Measured 2026-08-20 with
  the **real `run_kubectl`**: all five safety-gate refusals — readonly key on a write, operator key
  on a high-risk verb, protected namespace, cluster-wide mutation, terminal-only verb — contain
  none of those words, so every one produced `DryRunResult(ok=True, admission_denied=False)`: a
  claim that the API server and its admission chain accepted a command that was never sent. An
  unreachable cluster (`The connection to the server localhost:8080 was refused …`) and
  `run_kubectl`'s own `(no output)` placeholder took the same path. The flag handling had the
  mirror-image hole: `_with_server_dry_run` left the command untouched whenever the **substring**
  `--dry-run` appeared, so `--dry-run=none` (which real kubectl v1.36.3 documents as the default —
  *"--dry-run='none': Must be \"none\", \"server\", or \"client\""* — i.e. not a dry run), a bare
  `--dry-run` (*"deprecated and can be replaced with --dry-run=client"*), and an explicit
  `--dry-run=client` all suppressed the server-side validation while the result still claimed to be
  one; `kubectl label deploy/web team=--dry-run` tripped it from inside a label value.
  `DryRunResult` gained `validated`: false means the API server never answered, so `ok` and
  `admission_denied` are statements about nothing. `_with_server_dry_run` now matches whole tokens
  and rewrites any dry-run spelling to `--dry-run=server`. And `plan_mutation` downgrades an `auto`
  decision to `approve` when the dry-run did not run — an unrun check is not a passed check, and
  auto-execution is earned against evidence the server would accept the command. The production
  HITL gate in `kubectl_tool.py` runs the same test against `args`, a `list[str]`, so there it is
  exact token membership and `--dry-run=none` correctly still requires human approval; the defect
  was confined to this module.

- **The health oracle could not tell "not ready" from "I could not look" — and the executor rolled
  back on both.** `deployment_ready` (v5 P3 TNR verification rung) reads the cluster through
  `run_kubectl`, which returns a string and discards the exit code. Anything that is not a
  `kubectl get` table therefore has no READY column, the parser found no row, and the oracle
  answered `met=False, "deployment 'web' not found in 'prod'"` — the same verdict it gives a
  genuinely unhealthy deployment. Measured 2026-08-20 with the **real `run_kubectl`**: a read of a
  protected namespace returns `[Protected] Access to namespace 'kube-system' is not permitted`, and
  a machine without the binary returns `[Error] kubectl is not installed or not found in PATH`;
  both became *"deployment 'web' not found in 'prod'"* — a health verdict about a namespace the
  oracle never looked at. `execute_transactional` reads `met=False` as a failed mitigation and runs
  the rollback command, so an **instrument outage became a live mutation against a cluster we had
  just been told we cannot read**. Real kubectl's `The connection to the server localhost:8080 was
  refused …` and `run_kubectl`'s own `(no output)` placeholder took the same path.
  `PostconditionResult` gained `evaluated`: false means the oracle has no observation at all, so
  `met` must not be read as a verdict. `execute_transactional` answers that with the new
  `VERIFY_INCONCLUSIVE` — escalate, **never roll back** — while a read that succeeded and simply
  does not contain the row stays a real observation (`evaluated=True, met=False`). The
  string-reading itself moved into `app/tools/aci/kubectl_output.py` so the apply side and the
  verify side cannot disagree about what a given `run_kubectl` result meant. Same scope caveat as
  the entry below: this executor has no production caller yet.

- **A refused mutation was read as a successful one, and the executor then rolled back a change
  that had never happened.** `execute_transactional` (v5 P3 TNR, shipped default-off and listed in
  the v5 P3 entry below as "transactional apply→verify→auto-rollback") promises a mitigation
  either commits or leaves the cluster as it was. It decided whether the apply had happened with
  `not any(e in output.lower() for e in ("error", "exit=1", "not found", "forbidden", "invalid"))`
  — a substring scan over prose, on a seam whose input is a human-readable string because
  `run_kubectl` returns text and discards the exit code. Measured 2026-08-20 by driving the **real
  `run_kubectl`**: every safety gate in the project answers a blocked mutation with a marker
  string, and **all five read as SUCCESS** — readonly key on a write and operator key on a
  high-risk verb (`[Permission Denied]`), infrastructure namespace and cluster-wide mutation
  (`[Protected]`), a verb needing a terminal (`[Unsupported]`). End to end, a refused
  `kubectl scale deploy/web --replicas=3` produced `status: rolled_back` and issued
  `kubectl scale deploy/web --replicas=1` **against the live cluster**, undoing something that was
  never done: the safety gate's refusal was converted into a mutation. The reverse direction is as
  wrong and more likely — `deployment.apps/error-budget-exporter configured` is a successful apply
  whose *resource name* contains "error", so it was reported `apply_failed`, the postcondition
  oracle never ran, and the change stayed live and unverified. Real kubectl text was also missed
  entirely: `The connection to the server localhost:8080 was refused …` contains none of those
  five keywords. Replaced with `classify_apply()`, which reads KubeIntellect's own refusal markers
  and kubectl's error openers as **line prefixes** rather than substrings anywhere, and returns
  three outcomes: `APPLY_REFUSED` (nothing ran, nothing to roll back), `APPLY_FAILED` (kubectl
  rejected it), or applied — where the postcondition oracle, not a keyword, is the authority.
  Scope, stated plainly: `execute_transactional` has **no production caller** today (tests only),
  so this is a guarantee that could not have been delivered, not a cluster that was harmed.

- **Rollback points reported themselves armed while holding something that cannot be applied.**
  Before every mutating `kubectl` command the tool layer captures the target's current YAML and
  records it as a `rollback_point`; the digest listed them under *"Rollback points armed"*, the
  server logged `rollback_point_armed`, and `docs/flight-recorder.md` said recovery is "manual
  but mechanical: pipe the captured state into `kubectl apply -f -`". What is actually stored is
  `redact_secrets(yaml, max_chars=4000)`, and both of those transformations can destroy the
  object. Measured 2026-08-20 against real `bitnami/kubectl:latest` output at both ends: for a
  **Secret**, the line `kind: Secret` contains a redaction keyword and is dropped, so kubectl
  answers `error: unable to decode "STDIN": Object 'Kind' is missing` — nothing to restore; for a
  **ConfigMap** whose values are token-shaped, every value becomes `<redacted-token>` and the
  result is still **valid** (`kubectl label --local` accepts it as `configmap/app-config`), so the
  documented recovery **succeeds and overwrites the live configuration with placeholders** — a
  restore that destroys exactly what it was meant to protect; and any object over the cap (this
  project's own chart `values.yaml` is 7.4 KB) is cut mid-line and no longer parses. Redaction is
  not the defect and is not negotiable — the alternative is credentials in Postgres — so the
  capture is now compared against what kubectl produced and the record says which of the two it
  is: `restorable` plus `capture_notes` naming what changed. A capture that cannot be applied is
  still recorded, as evidence of what the object looked like, but the log says *"NOT restorable,
  do not apply it"* instead of *armed*, the digest section became *"Pre-mutation state captures
  (N of M restorable)"* with a per-entry verdict, and the postmortem timeline marks it. Records
  written before the field existed are reported as *unknown*, never promoted to armed.

- **The tamper-evident audit log could lose records and still verify as intact.**
  `docs/flight-recorder.md` promised there is "no way to edit, insert, or delete a record
  without the chain failing verification afterwards", and two paragraphs later stated that
  during a recorder outage "events are dropped, not buffered to disk". Both could not be true.
  Measured 2026-08-20 by driving the real recorder against a real `postgres:16-alpine`: with the
  `decision_log` table removed mid-episode, the in-process chain head advanced anyway, so the
  same process resumed at a skipped `seq` and `verify_chain` reported the episode as **broken** —
  `kq replay` exit 3, *"records may have been tampered with"*, permanently on the record for a
  database blip that altered nothing. Worse in the other direction: if the process restarted
  after the outage, the head was re-read from the database, the sequence came back contiguous,
  and the result was six rows, `seq 0-5`, **`chain_valid=true`** — with three recorded events
  silently gone and the postmortem printing *"✅ Audit chain verified intact — every event below
  is tamper-evident."* A third path made loss permanent: a failed chain-head lookup was swallowed
  and cached as a genesis head, so every later batch for that episode collided with
  `UNIQUE (episode_id, seq)` and was dropped for good, while the log blamed "duplicate key" —
  a symptom of its own retry, not the outage. The write path stays fire-and-forget (a recorder
  outage must never break a user response), but loss is no longer invisible: a failed batch drops
  the cached chain head instead of advancing it, so the chain stays contiguous and a blip is not
  reported as tampering; the count and the real cause are carried forward and written **into the
  chain** as a `recorder_gap` record the moment writes recover, where it cannot be removed without
  breaking verification. `kq replay` and `kq export` gained exit code **5** — chain intact, episode
  incomplete — and `kq postmortem` prints a **RECORD INCOMPLETE** banner beside the ✅ verdict with
  the number lost and why. *Intact* and *complete* are two claims, and only the first was ever
  a property of a hash chain.

- **The zero-token detection layer reported itself "active" while nothing was being watched.**
  `GET /v1/findings` returned `{"sensorium": "active", "detectors": N}` whenever a `DetectorEngine`
  object had been constructed — a fact about object lifetime, not about perception. Nothing
  anywhere tracked whether a `kubectl --watch` stream was connected. Measured 2026-08-20 by
  starting the real watchers on a host without kubectl: both watch tasks hit `FileNotFoundError`
  and **`return`** — that loop exits permanently and never reconnects for the rest of the process
  lifetime — and the endpoint still answered `{"sensorium": "active", "detectors": 20,
  "findings": []}`, which `kq findings` renders as the green line *"No findings · 20 detectors
  watching"*. Nothing was watching. An RBAC denial reaches the same silence by a different route:
  kubectl exits non-zero, the loop retries forever at a 60-second backoff cap, and `stderr` was
  sent to `DEVNULL`, discarding the one piece of information that explains it — `pods is
  forbidden: User "system:serviceaccount:…" cannot watch`. This is the layer that is supposed to
  notice trouble without an LLM, so an empty findings list from a deaf sensorium is the most
  expensive kind of silence. Each watch stream now records its own health — connected, permanently
  stopped, consecutive failures, and kubectl's own stderr as the reason, captured through a
  concurrent drain so a full stderr pipe can never block the child. `sensorium` became a real
  state (`active` only while a stream is connected, otherwise `disabled`, `starting`,
  `reconnecting` or `stopped`), the streams are reported alongside it, and `kq findings` prints the
  green all-clear **only** when the sensorium is genuinely watching — otherwise it says an empty
  result does not mean the cluster is healthy and lists each stream with its reason. The active
  path, the disabled path and the findings table are unchanged and asserted as such.

- **The morning digest said "Quiet watch" when nothing had been recorded.** `kq digest` is the
  operator's check on what the agent did overnight, so an empty result is only reassuring if the
  sources were readable. Measured 2026-08-20, four materially different states produced the
  identical, confident line *"Quiet watch: no findings in the last 24h."*: a genuinely quiet night;
  a `decision_log` query that raised (caught as `except Exception: rows = []`); **SQLite mode**, a
  supported and documented configuration in which — per `docs/flight-recorder.md` — there is no
  `decision_log` table at all, so the digest structurally cannot have data; and
  `FLIGHT_RECORDER_ENABLED=false`, where nothing is ever written. Only a missing connection pool
  was reported honestly. `kq digest` rendered that sentence and exited `0` in every case, so a
  night with recording switched off was indistinguishable from a night on which nothing went
  wrong. The digest now carries `degraded` and `degraded_reasons`, empty exactly when the digest
  is a real observation of the window; it names the setting an operator would change
  (`FLIGHT_RECORDER_ENABLED`, `USE_SQLITE`, `WATCHTOWER_ENABLED`) rather than only the resulting
  error; the summary leads with `Digest INCOMPLETE … This is NOT a quiet watch`; and the rendered
  markdown opens with a warning block before any section. Degraded does not mean suppressed —
  whatever was readable is still reported, and a partially-readable digest keeps its sections and
  still says so. A genuinely quiet watch is unchanged and is asserted as such. `kq digest`
  deliberately still exits `0`: it is a successful report of a degraded state, and scripts should
  branch on `degraded` in the JSON form.

- **A predicate type the schema, the docs and the detector-authoring prompt all treat as working
  is never evaluated.** Playbook `detect:` blocks accept three predicate types. `watch_predicates`
  are matched by `DetectorEngine.process()`; `trend_predicates` are evaluated by the periodic tick
  (ADR-010). **Nothing has ever read `DetectBlock.promql`** — verified 2026-08-20 across every
  module in the server package. It was nonetheless treated as real everywhere else:
  `parse_detect_block` accepted it as sufficient to make a block valid, `_is_detect_block` counted
  it when deciding a database row was a recompilable detector, and `authoring.py` told the
  NL-authoring model *"promql: list of instant PromQL strings (firing = non-empty result)"* — so
  ADR-012 could mint a PromQL-only shadow candidate that validates, is staged for human promotion,
  accrues no precision because it cannot fire, and would still never fire once promoted.
  `Finding.source` likewise documents an unreachable `"promql"` value. Stated honestly: all 21
  `promql:` queries in the shipped playbooks sit alongside real `watch_predicates`, so no shipped
  detector is dead and nothing that fires today stops firing — what was false is the additional
  coverage those queries appear to claim and the validity of a PromQL-only detector. This is the
  same shape as the `kind:` trap already documented in the playbook reference: it parses, loads,
  counts toward the detector total, passes the schema check, and matches nothing, ever. PromQL now
  does not on its own make a block valid — a PromQL-only block is rejected at parse time with a
  warning naming the reason — and the authoring prompt and error message say plainly that the key
  is recorded but not evaluated. The queries themselves are kept and their count is pinned by a
  test, so removing them has to be deliberate. **Evaluation has not been implemented**: that is a
  new capability with its own failure semantics, not an audit fix, and a test now fails
  deliberately if anything starts reading the field, so the docs cannot drift back out of date.

- **A cluster read that failed was reported to the model as a cluster that was empty and
  healthy — and to memory as a fix that worked.** `context_fetcher` pre-fetches pods and Warning
  events before every turn; its runner returned `proc.stdout or proc.stderr` and never looked at
  the exit code, so kubectl's error text was handed to the pod-table parser as cluster data.
  Measured 2026-08-20 against the real binary (`bitnami/kubectl:latest`), the two failure shapes
  produced two different lies. A connection failure prints three lines, two of which have enough
  whitespace-separated columns to be counted as pod rows ⇒ `pod_count=2`, a quantity invented out
  of an error message. A single-line failure — `error: You must be logged in to the server
  (Unauthorized)` — was consumed as the header row ⇒ `pod_count=0, has_issues=False`. Three
  consumers acted on that. (1) The prompt: `_snapshot_sufficiency_block` asserts *"the cluster
  snapshot above was fetched Ns ago and contains 0 pods. Health flags: issues=false,
  warnings=false"* and then instructs the model to **prefer answering directly from the snapshot**
  for exactly the questions "how many pods", "is the cluster healthy", "what's running" — measured
  with the pods read failing and the events read succeeding-and-empty, an ordinary asymmetric
  failure. (2) R4 post-fix verification: `_verify_resolution` documents "None if verification …
  failed to run", but nothing raised, so a failed read scanned clean and it returned
  `(True, "resolved")`, recording an unverified fix as verified — and `promotion.py` selects those
  rows (`WHERE verified = TRUE`) to mint learned rules and detector candidates. A cluster read is
  most likely to fail immediately after a disruptive change, which is precisely when R4 runs.
  (3) Playbook triggers ran their regexes over the stderr text. Each read is now checked by exit
  code and carries an `ok` flag; a failed read sets a new `snapshot_read_failed` state field,
  renders as an explicit **UNAVAILABLE** section that still shows kubectl's reason but is never
  labelled pod state, is never parsed for a count, is not matched against playbook triggers, and
  makes the sufficiency block assert no count and no health flags while requiring a fresh fetch.
  R4 returns `(None, None)` on a failed read. The healthy path is unchanged and is asserted as
  such.

- **Cluster identity was unresolvable in the only mode the chart ships, so every deployment
  wrote into a scope every other cluster reads.** `cluster_id.py` exists, in its own words, so
  that "patterns from a Kind dev cluster would pollute prompts on prod EKS and vice versa" —
  memory, episodes, learned failure patterns and findings are all scoped by the id it returns.
  Two of its three strategies shelled out to `kubectl config`, which needs a kubeconfig **file**.
  An in-cluster deployment has none: the chart sets `KUBECONFIG_PATH: ""` so kubectl
  authenticates with the pod's ServiceAccount. Verified 2026-08-20 against the real binary
  (`bitnami/kubectl:latest`, no kubeconfig): `kubectl config current-context` exits 1 with empty
  stdout, and `config view --minify` exits 1 with "current-context must exist in order to
  minify". Both strategies therefore returned nothing and identity fell through to the literal
  `"unknown"`. The module's docstring called that "a sentinel that read paths can filter out",
  which is the opposite of what the read paths do: `memory_store` recalls with
  `cluster_id IN ($1, 'unknown')` — deliberately, so pre-column rows still match — which makes
  the sentinel a **cross-cluster wildcard**. Two clusters sharing a database both wrote it and
  both read each other's rows, which is exactly the contamination the module was written to
  prevent, arriving by default and only in production: on a laptop a kubeconfig is present and
  identity resolves, so it looked correct in development. `docs/reflexion.md` further claimed
  the sentinel rows "age out via retention" so the system "naturally converges to per-cluster
  patterns only" — untrue in-cluster, where fresh `'unknown'` rows are minted continuously.
  Identity is now resolvable: a new `CLUSTER_ID` setting takes precedence over every probe
  (Helm: `config.clusterId`), and where it is unset the `kube-system` namespace UID — the
  conventional cluster identifier — is tried before giving up. The fallback still exists and
  still returns the sentinel, because filtering those rows on read would discard the legitimate
  data of every single-cluster deployment; it now logs a warning naming both the fix and the
  consequence, and `cluster_id_is_resolved()` lets callers tell a real identity from the
  sentinel. Two stale doc claims that the fingerprint hashes a "namespace count" were corrected
  — no namespace count has ever been part of it.

- **The kill switch an operator can see was not the kill switch the agent obeyed.**
  `GET /v1/v5/status` reports `kill_switch_engaged` — annotated in the response model as
  "⇒ all autonomous writes denied" — and `kq v5-status` prints it in red. But
  `auto_write_permitted()` returned **allow** when `KI_V5_BLAST_RADIUS_BUDGET` was false,
  *before* consulting the kill switch, and that flag defaults to `False`. Measured 2026-08-20
  through the real watchtower path in the default configuration: with the kill switch engaged,
  `kill_switch_engaged()` returned `True` (what the API and CLI report) while
  `watchtower._should_auto_fix()` returned `True` — the agent kept auto-fixing. A declared
  `KI_V5_CHANGE_FREEZE` was ignored the same way. The failure mode is the worst available for a
  break-glass control: an operator stopping the agent mid-incident was told it had stopped, and
  so did not reach for a real brake. The runtime toggle exists precisely so a stop needs no
  redeploy, yet it was inert unless an unrelated experimental flag had been enabled by env var
  beforehand. Both brakes now bind regardless of `KI_V5_BLAST_RADIUS_BUDGET`, which is left with
  no consumer at all: `gate_write` never read it either, so the flag's only effect in the whole
  codebase was to disable a brake. It is recorded as unwired in `UNWIRED_EXPERIMENTAL_FLAGS`
  rather than given an invented purpose. Default behaviour is unchanged — a deployment
  with no brake engaged sees the same ladder as before. Two existing tests asserted the defect
  (`auto_write_permitted().allow is True` with the switch engaged, commented "gate inactive ⇒
  ladder unchanged"); they now assert the corrected contract, alongside a new suite that checks
  the reported state against the actual write decision over all sixteen combinations of the four
  inputs, so the signal and the behaviour cannot drift apart again.

- **Cluster-scoped objects escaped the autonomy safety model, which is built on namespaces.**
  A Node, PersistentVolume or ClusterRole has no namespace, so a Warning event about one
  (`NodeNotReady`, `Rebooted`, `KubeletHasDiskPressure` — among the most common warnings in any
  cluster) reaches the watchtower with `namespace=""`. That fell through to the configured
  default rather than being pinned. Because `fnmatch("", "*")` is true and `*` is the natural
  way to write "all my namespaces" in an allowlist whose docstring advertises glob support, an
  operator who set `AUTONOMY_A3_ALLOWLIST=SomePlaybook/*` silently made **Nodes auto-fixable** —
  where an unattended remediation (cordon, drain, delete) is the least recoverable action the
  system can take. Measured 2026-08-20 by feeding `_event_observation` a real-shaped
  `NodeNotReady` event: `level_for_namespace("")` returned the default and
  `a3_allowed("NodeNotReady", "")` returned `True`. An unattributable namespace is now capped at
  A1 — investigated and reported, never mutated — with `a3_allowed` refusing it independently of
  the cap; the cap is a ceiling, so a deployment pinned to A0 stays at A0. Observation is
  unaffected. The ladder now also normalises namespace names (strip + lowercase) the way
  `run_kubectl` does, so the two components cannot disagree about which namespace is protected.

- **Ten of twelve `/v1` routes answered without an API key.** Authentication was a per-endpoint
  convention — each handler called `get_user_role(request)` itself — so it was enforced exactly
  where somebody had remembered it. Measured 2026-08-20 with auth enabled: `/v1/digest`,
  `/v1/findings`, `/v1/episodes/{id}/replay` (the flight recorder — every command run and its
  output), `/v1/episodes/{id}/postmortem`, `/v1/events/replay/{session}`, `/v1/namespaces`,
  `/v1/v5/status`, and the **read** halves of `/v1/detectors` and `/v1/preferences` all returned
  data to a request carrying no `Authorization` header; only `/v1/chat/completions` and
  `/v1/auth/demo-keys` challenged. In `detectors.py` and `preferences.py` a `_require_writer`
  helper gated every mutation and no read — the same "a read is a safe default" assumption found
  in `run_helm` and the Loki/Prometheus tools the day before. Authentication is now a dependency
  on the API router, so every route inherits it and a route added later cannot forget it;
  `/healthz` and `/readyz` are mounted on a separate public router because they must answer an
  unauthenticated kubelet. The documented open mode (no keys configured ⇒ every caller is
  `admin`) is unchanged, and the per-verb role checks in the tools are untouched.

### Fixed
- **An unreachable cluster was reported as a cluster with no namespaces.** `GET /v1/namespaces`
  shells out to `kubectl get namespaces` and never checked the return code, so an unreachable API
  server, an expired credential, an RBAC denial or a bad `KUBECONFIG_PATH` produced empty stdout
  and was returned as `200 {"namespaces": []}`. The wrong answer then travelled: `kq` validates
  `/ns <name>` against this list, and its REPL is deliberately careful — it distinguishes present,
  absent and *undetermined*, and only rejects on a definite absence so that a backend outage
  cannot block an operator. A `200` with an empty list is a definite absence, so the care was
  defeated and the REPL answered **"Namespace 'prod' not found in the cluster"** during exactly
  the incident where the operator's credentials had expired — pointing them at a deleted namespace
  instead of at their kubeconfig. The same empty list silently emptied `kq`'s namespace
  tab-completion. Measured 2026-08-20 end to end. The endpoint now returns **503** with the first
  line of kubectl's stderr (`FileNotFoundError` and a timeout included), so an empty list means
  one thing only; the protected-namespace filter is re-asserted by test so the new error handling
  cannot drop it.

- `kq`'s `fetch_namespaces` mapped any `200` to `body.get("namespaces", [])`, so a response it
  could not interpret — a missing key, a `null`, a gateway's own JSON — also became "zero
  namespaces" rather than "unknown". Only a genuine list now counts as an answer; everything else
  is `None`, which the caller already handles by failing open.

- `kq`'s health check reported `"did not respond within 5 s"` whatever timeout was in force, even
  though it is configurable via `KUBE_Q_HEALTH_TIMEOUT`. It now names the real value.

- **The database migration could not fail.** Every path that applies `schema.sql` ran `psql -f`
  without `ON_ERROR_STOP=1`. psql's documented default is to print an error, continue to the next
  statement, and **exit 0** — so a migration that applied nothing still reported success. Measured
  2026-08-20 against `postgres:16-alpine`, the image the chart's Job uses, running the real schema
  as a role without `CREATE` on `public` (the ordinary shape of a managed instance): **70
  statements failed, 0 of 18 tables were created, psql exited 0**. Kubernetes reads that exit code,
  so the `job-db-init` Job was marked `Succeeded` and `helm upgrade` reported `deployed`. Nothing
  downstream contradicted it — `/readyz` deliberately does not probe Postgres, and memory/recorder
  writes are fire-and-forget by design, so the product degraded silently with only an unwatched
  warning line in the server log. All five call sites now pass `ON_ERROR_STOP=1
  --single-transaction`, making the migration all-or-nothing rather than half-applied: the Helm
  Job, `make db-init`, the documented Alibaba RDS command, the schema header comment, and the
  documented **restore** command in `docs/operations.md` — where the same default meant a disaster
  recovery could report success and restore nothing. The `pip`/CLI path (`kubeintellect db-init`)
  was already correct: it uses psycopg, which raises, and it exits 1. A new suite asserts the flags
  on every shipped path, and that the two assumptions behind `--single-transaction` still hold (no
  `CREATE INDEX CONCURRENTLY`; every statement idempotent, since the Job re-runs on each upgrade).

- **The Helm chart shipped an unguarded manual copy of the schema.** `configmap-schema.yaml`
  embeds 456 lines of SQL literally rather than reading `schema.sql`, and the Job applies the copy.
  They are byte-identical today — verified — but nothing enforced that, and a stale-but-valid copy
  would apply cleanly and report success. Now gated by a test that diffs the two.

- The test harness claimed to force auth off and did not: `conftest.py` cleared three of the
  four key lists, so a local `.env` carrying a superadmin key (or `DEMO_KEY_HMAC_SECRET`) left
  `settings.auth_enabled` true while the comment said otherwise. Invisible until the routes
  began enforcing it, at which point nine tests failed with 401. All five inputs are now cleared.

- **`--all-namespaces` names every namespace, so it named none the guard could check.** The
  protected-namespace check asks which namespace a command names; a command naming *all* of them
  names none in particular, and for eleven passes of hardening it simply did not fire on `-A`.
  Measured 2026-08-20 against the real tool: `kubectl get pods -n kube-system` was refused while
  `kubectl get pods -A` returned the identical rows plus `kubeintellect` and `monitoring`;
  `kubectl get events -A` and `kubectl get configmaps -A -o yaml` likewise. Worse on the write
  side — `kubectl delete pods -n kube-system` was refused while `kubectl delete pods
  --all-namespaces` reached the approval prompt and, once approved, would have deleted pods in
  `kube-system`, `monitoring` and `kubeintellect`, the namespace KubeIntellect itself runs in;
  it composed badly with the fail-open approval gate fixed the same day. Cluster-wide
  **mutations are now refused for every role including superadmin**, with a message pointing at
  `-n <namespace>`. Cluster-wide **reads keep working and are filtered** — table, `-o wide`,
  `-o json`, `-o yaml` and `describe` drop entries from blocked namespaces and state how many
  were withheld. `-o name` and `-o jsonpath` carry no namespace to filter on and are refused
  rather than passed through, the same fail-closed choice made for an unparseable payload.

### Fixed
- `mkdocs build --strict` exited **0** on a broken intra-page anchor, reporting a link as
  resolved when it was not — measured by deliberately breaking one. `validation.anchors` (and
  unrecognized/absolute links, omitted files) is now raised to `warn`, which `--strict` turns
  into an error; verified red-green. No pre-existing link in the site was broken.

- **The two observability tools reached the same cluster's data with no blocklist at all.**
  Four tools are registered for the agent. `run_kubectl` and `run_helm` reach the cluster
  through a command line and gate on `-n`; `query_loki` and `query_prometheus` reach the same
  data through a *query language*, where the namespace is a label matcher, and enforced nothing.
  Measured 2026-08-20 against the real tools: `{namespace="kube-system"}`,
  `{namespace="kubeintellect"} |= "key"`, `{namespace="monitoring"} |~ "token|password"`,
  `rate({namespace="cert-manager"} |= "error" [5m])` and `kube_secret_info{namespace=
  "kubeintellect"}` all executed and returned their data. Loki is the sharper end: `kubectl logs
  -n kube-system` is refused precisely because logs carry credentials in plaintext, and
  `query_loki` advertises itself as the better way to read logs. Both tools now gate twice — the
  query is refused if it positively selects a blocked namespace (negative matchers are not
  mistaken for selection), and every returned stream/series is dropped if its own `namespace`
  label is blocked, which also catches `{app="nginx"}` matching a pod in `kube-system`. The
  response states how many results were withheld. The detector engine's
  `query_prometheus_range_raw` is deliberately exempt: its PromQL comes from human-reviewed
  playbooks and is meant to watch `kube-system`. Residual documented in `docs/security.md`: a
  result with no `namespace` label passes, so an aggregation that discards the label can still
  return a scalar computed partly over a blocked namespace.

- **The agent had a second way to reach the cluster, and it enforced neither blocklist.**
  `run_helm` is read-only against the cluster — its verb check is an allowlist, so it can never
  mutate a release — but read-only against the *cluster* is not read-only against *what may be
  read*. It applied no namespace blocklist and no resource blocklist, so it answered in
  protected namespaces questions `run_kubectl` refuses for every role. Measured 2026-08-20
  against the real tool: `helm list -n kube-system`, `helm get values kubeintellect -n
  kubeintellect`, `helm get manifest web -n prod`, `helm get all prometheus -n monitoring` and
  `helm status cert-manager -n cert-manager` all executed. `helm get manifest` renders the
  release's own `kind: Secret` objects with their base64 `data:` intact — precisely what
  `kubectl get secret` is refused for unconditionally. Separately, `GET /v1/namespaces` runs its
  own `kubectl get namespaces` and returned the blocked namespaces in full, because the pass that
  added the namespace output filter added it to the tool and not to the route. `docs/security.md`
  stated both guarantees about the product while one of three code paths enforced them. All three
  now share one definition, read from `KUBECTL_BLOCKED_NAMESPACES` rather than copied.
  `helm list -A` is filtered in table, JSON and YAML, failing closed on an unparseable payload.

### Fixed
- `run_helm` read its subcommand as `tokens[1]`, so `helm -n prod list` was rejected as an
  unsupported subcommand. Behind the allowlist this was a usability bug rather than a bypass —
  the same parser defect that was a *bypass* in `run_kubectl`'s deny-list (fixed 2026-08-13).
  It now uses the shared flag-aware parser. Recorded in `docs/security.md` as the argument for
  preferring allowlists: an allowlist turns a parser bug into a complaint, a deny-list turns the
  same bug into a hole.
- `docs/security.md` layer 6 carried a dangling half-sentence left by an earlier edit.
- **A HITL denial that was not one of 13 exact phrases executed the command.** The paused graph
  was resumed with `Command(resume=not is_denial(user_message))`, so approval was the default and
  only a recognised *denial* prevented execution. `is_approval()` already existed and nothing
  called it for this decision. Measured 2026-08-20 by driving the real `stream_events` with a
  thread paused at an interrupt: `"No."` (with a full stop), `"NO!"`, `"no thanks"`,
  `"don't do that"`, `"cancel it"`, `"stop it"`, `"not yet"`, `"wait"`, `"hold on"`, `"why?"`,
  `"what will that do?"` and an **empty message** all resumed with `True` and ran the destructive
  command. `docs/security.md` has always documented the opposite — *"anything else → treated as
  denial"* — so the published contract was false in the fail-open direction, on the last gate
  between an LLM and a destructive cluster operation. The resume value is now
  `is_approval(msg) or is_auto_approve_request(msg)`; case, surrounding quotes and trailing
  `.`/`!` are normalised so `No.` and `YES!` read as intended; an unrecognised reply cancels and
  logs a warning naming it.

### Fixed
- **`kubectl rollout restart|undo|pause` armed no rollback point**, despite
  `docs/flight-recorder.md` promising one before *"every mutating `kubectl` command"*. The
  arming condition still used the enumerated `_HIGH_RISK | _MEDIUM_RISK` deny-list after the
  approval gate had moved to `_is_write_verb`, so the two consumers of "is this destructive"
  disagreed; any verb this build does not know also armed nothing. Both now use the same test.
  Two silent no-ops inside the capture are fixed as well: `rollout` puts a subcommand before its
  target, so the pre-state read was built as `kubectl get restart deployment/api -o yaml`, and
  `kubectl label pod api-1 tier=web` kept `tier=web` as a resource name — both rejected by
  kubectl and both swallowed by the deliberately best-effort wrapper, arming nothing while
  appearing to arm something.

### Security
- **The namespace listing filter worked in three of its six output formats.** A bare
  `kubectl get namespaces` is deliberately allowed *because* blocked namespaces are stripped from
  the result. Measured 2026-08-20 with a `readonly` key: the default table, `-o wide`, `-o name`
  and `-o jsonpath` filtered correctly, while **`-o json`, `-o yaml` and `-oname` returned the
  blocked namespaces in full**, and **`kubectl describe namespaces` was not filtered at all**.
  Three separate causes in one function: the `-o` reader did not accept pflag's attached
  shorthand (the same gap fixed for `-n`, now sharing one `_flag_value` parser so they cannot
  drift again); `json`/`yaml` returned the payload unchanged behind the comment *"too complex to
  strip reliably; blocked at execution anyway"*, whose second half was false — nothing blocks a
  bare listing at execution; and the filter passed a hardcoded `"get"` to `_extract_resource_type`,
  so for any other verb the resource came back `None` and the filter returned early, handing back
  the labels, annotations and quotas of every namespace. `-o json`/`-o yaml` are now parsed,
  filtered and re-serialised, and a payload that cannot be parsed is replaced rather than
  returned unfiltered. What leaked was namespace names and metadata, not credentials.

### Security
- **`kubectl apply -f <path|URL>` and `-k <URL>` ran with the manifest never reaching
  KubeIntellect.** Pass-55's fix taught the protected-access checks to read a manifest on stdin;
  these forms put it somewhere the process cannot reach at all. Measured 2026-08-20 for
  `operator` and `admin`, `kubectl apply -f /tmp/payload.yaml`,
  `kubectl apply -f https://example.com/m.yaml` and `kubectl apply -k https://github.com/…` all
  executed. Three properties failed together: the protected-resource and protected-namespace
  checks saw a command naming neither, so a Secret or a write into `kube-system` was invisible to
  both; the approval prompt carried `stdin: null` and a `human_summary` that was just the command
  line, so the approver had nothing to review — and for a URL the content did not exist yet at
  approval time, since kubectl fetches it afterwards; and that fetch is unreviewed outbound
  network access from the KubeIntellect pod. Any `-f`/`--filename`/`-k`/`--kustomize` whose value
  is not `-` is now refused with a message pointing at the supported stdin form, in every
  spelling pflag accepts (`-f x`, `-f=x`, `-fx`, `--filename=x`). `kubectl logs -f` is
  unaffected — there `-f` means `--follow`.

### Security
- **A manifest on stdin was invisible to both protected-access checks.** `kubectl apply -f -`
  names neither a resource nor a namespace on the command line — they are the manifest's `kind:`
  and `metadata.namespace:` — and every check parsed argv. Measured 2026-08-20 for `operator` and
  `admin`: applying a Pod whose `metadata.namespace` was `kube-system` reached an ordinary
  approval prompt, while `kubectl apply -f - -n kube-system` was refused outright; a
  `kind: Secret` manifest was likewise only prompted, while `kubectl create secret generic` was
  refused. This is the form KubeIntellect itself recommends — the `kubectl edit` rejection message
  points users at `kubectl apply -f -` with stdin. Both checks now read the manifest as well as
  argv, walking every document in a multi-document stream and the items of a `kind: List`, and
  the superadmin re-check uses the same manifest-aware helper so that role does not get the
  closed bypass handed back. Scope is deliberately the manifest's `kind` and
  `metadata.namespace` and nothing deeper: a Pod that mounts a Secret in its own namespace is
  what Pods are for and still applies.

### Security
- **`kubectl -nkube-system` reached a protected namespace that `kubectl -n kube-system` could
  not.** kubectl parses flags with pflag, which accepts a shorthand's value attached to it —
  `-n kube-system`, `-n=kube-system` and `-nkube-system` are the same command. `_extract_namespace`
  read only the spaced form and `--namespace=`, so for the other two it returned `None` and the
  protected-namespace check never ran its comparison: the guard did not decide the namespace was
  permitted, it never learned there was one. Measured 2026-08-20 through the real tool,
  `kubectl get pods -nkube-system` **ran** for `readonly`, `operator` and `admin` — all three of
  which are refused the identical command written with a space — and an admin's
  `kubectl delete pod x -nkube-system` was downgraded from an outright `[Protected]` refusal to an
  ordinary approval prompt. All five spellings are now equivalent, `superadmin` keeps its
  documented bypass, and an unprotected namespace still works in every form.

### Fixed
- **Three documentation surfaces understated the infrastructure-namespace block.**
  `docs/security.md` said *"read-only verbs always allowed"* in the HITL sequence and titled its
  matrix row *"Writes to infrastructure namespaces"*, and `docs/architecture.md` said *"infra
  namespace writes blocked"* for `admin`. The code blocks **all** access including reads
  (`kubectl get pods -n kube-system` is refused for every role but `superadmin`), which is the
  stronger and intended behaviour — a Secret is reachable through a Pod spec, an Event or a
  ConfigMap, not only through `kubectl get secret`. The docs now say so.

### Security
- **The command gate was a deny-list, so every kubectl verb it did not name counted as a read.**
  `DESTRUCTIVE_VERBS` enumerated 13 verbs; kubectl has many more. Measured through the real tool
  with a **read-only** API key, all of these executed with no approval prompt: `label`,
  `annotate`, `rollout restart`, `rollout undo`, `cp`, `debug`, `expose`, `autoscale`,
  `port-forward` and `attach`. `kubectl cp prod/api-1:/etc/creds /tmp/` copies files out of a
  container — mounted Secrets included — and `kubectl debug node/node-1` starts a privileged pod
  on the node, so the two worst cases were readable by the role explicitly defined as unable to
  read Secrets. The default is now inverted: `_READ_ONLY_VERBS` is an **allowlist** and anything
  absent is treated as a write, so a verb introduced by a future kubectl release arrives blocked
  rather than pre-approved. `rollout` is judged by its subcommand — `status` and `history` stay
  available to a read-only key, `restart` / `undo` / `pause` / `resume` do not. `cp` and `debug`
  are classified high-risk. The ACI bounds guard (`is_read_only`) delegates to the same function
  instead of keeping its own copy, which fixes the same `rollout restart` hole there.

### Security
- **kubectl's own alternative spellings walked through the credential block.** The blocklist
  compares literal strings, so `kubectl get sa` — the documented short name for
  ServiceAccounts — and the fully-qualified `kubectl get secrets.v1.` / `serviceaccounts.v1.`
  form returned the objects, as did `sa/default`. Resource tokens are now normalised (short
  name, `resource.version.group` suffix, `resource/instance`, case) before being matched, at
  both comparison sites including the superadmin re-check. Unrelated CRDs that merely start
  with the same letters (`secretstores`, `sealedsecrets`) are unaffected.
- **A protected namespace named as the command's target was only prompt-gated, not blocked.**
  `kubectl delete pod x -n kube-system` was refused outright, but `kubectl delete namespace
  kube-system` reached a human approval prompt instead — the same protected namespace, and the
  documentation says infrastructure namespaces are blocked including reads. The namespace guard
  now also reads a positional target. Listing namespaces still works (the output is filtered),
  and deleting an ordinary namespace is still a normal approval-gated operation.
- **Reordering a kubectl command bypassed every safety gate, including the read-only role.**
  The subcommand verb was parsed as the second token, so `kubectl -n prod delete deployment api`
  — as valid as the canonical order, and a form an LLM writes routinely — parsed its verb as
  `-n`, a token in no risk set, no role set and no rejected set. Every check in `run_kubectl`
  keys off that value, so all of them fell together. Measured through the real tool: a
  **read-only** API key could delete Deployments and PersistentVolumeClaims and drain nodes,
  destructive commands executed with **no approval prompt**, and six of eleven ordinary ways of
  writing a Secret read returned the Secret. The verb and resource parsers now skip global flags
  wherever they appear, and, as defence in depth, a destructive verb appearing anywhere in the
  command is gated even if the parse misses it — matched on whole tokens, so `-l app=delete`
  does not trip it. No configuration change is required. All 1093 pre-existing tests passed
  before and after: every one of them writes the canonical order, which is why nothing caught it.
- **Tuning the kubectl blocklist silently unblocked every Secret in the cluster.**
  `KUBECTL_BLOCKED_RESOURCES` (Helm: `config.blockedResources`) *replaces* its list rather than
  extending it, and `values.yaml` said *"Override to add tenant-specific or environment-specific
  namespaces"* — so an operator following the documentation and adding `configmap` removed
  `secret`. Verified through the real `run_kubectl`: reading every Secret in a namespace, listing
  ServiceAccounts, and reading **this release's own API keys** all went from blocked to allowed,
  with no warning anywhere, while the guard still answered with its promise that Secrets are
  *"shielded from inspection to protect cluster credentials"*. The four credential types
  (`secret`, `secrets`, `serviceaccount`, `serviceaccounts`) are now re-added unconditionally via
  `ALWAYS_BLOCKED_RESOURCES` and cannot be configured away; operator additions still apply.
  Namespaces deliberately keep **no** floor — letting the agent investigate `monitoring` is a
  legitimate choice — but the values file now states the replace-not-merge semantics instead of
  inviting the mistake. Requires no action on upgrade; a deployment that had narrowed the list
  regains credential protection automatically.

### Fixed
- **A server crash mid-answer made `kq -q` exit `0`.** When the chat stream raised, the failure
  path ended it with the *same* frames a successful answer ends with — a `finish_reason: "stop"`
  chunk then `[DONE]` — and put the reason in `content` as `[Error: …]`. Prose is not a signal:
  `run_single_query()` scores any non-empty text as an answer, so in a script or CI job a crashed
  turn was indistinguishable from a real result, and a **partially streamed diagnosis was
  presented as a complete one**. The stream now also emits a `ki_event` of type `error` with
  **`fatal: true`**, and `kq` discards the partial answer and exits non-zero. `fatal` is what
  separates this from the error events emitted when one tool fails and the agent recovers and
  answers anyway — those still count as answers. The `[Error: …]` content chunk is retained so
  OpenAI-compatible clients that ignore the side channel still see a reason rather than an
  unexplained empty completion, and the `error` event type is now documented in the API
  reference (it never was).
- **The Helm chart's rolling-update drain never worked, and four places documented it as if it
  did.** The chart, the deployment template, `app/core/readiness.py`, `app/api/v1/endpoints/health.py`
  and the public API reference all described the same sequence: `SIGTERM` flips `/readyz` to `503`,
  Kubernetes stops routing, then the pools close. Sending a real `SIGTERM` to a real server and
  probing it showed the transition is `200` → **connection refused**: uvicorn closes its listening
  socket first and runs the application's shutdown hook last, so the `503` is never observable and
  a request arriving on a not-yet-updated kube-proxy route is *refused* rather than served — worse
  than the problem the flag was added to fix. The chart now carries a **`preStop` sleep**
  (`drainSeconds`, default `5`), which Kubernetes runs *before* `SIGTERM` and which actually holds
  the socket open while the Endpoints removal propagates. `set_ready(False)` is kept as honest
  in-process state but no longer claimed as a traffic-control mechanism, and every one of those
  five descriptions was corrected. New `tests/test_chart_shutdown_contract.py` pins the hook and
  the arithmetic — both failure modes (no hook; grace period at or below the drain) are otherwise
  silent outside a cluster under load.
- **The other half of the coordinator's memory context was still silent.** The previous entry made
  episode recall report its own outage; `load_memory_context()` — the V4 pinned block carrying
  operator preferences, failure hints, session notes and past RCA — still returned `""` on any
  failure. `""` is exactly what a brand-new user with nothing stored produces, so a Postgres outage
  reached the model as a clean slate: no preferences to honour, no precedent to build on, and no
  signal anywhere. It now raises `MemoryStoreUnavailable`, and the node injects the same explicit
  **"Memory unavailable"** block rather than an empty string. `USE_SQLITE` returning `""` is a
  configured state, not an outage, and is unchanged.
- **`GET /v1/preferences` answered `200` with an empty list when the store was unreadable**, so
  `kq preference list` printed `No preferences remembered` during a database outage — inviting the
  operator to re-enter preferences that already existed. It now answers **503**, matching
  `GET /v1/detectors`. Unlike episode recall, no agent path depends on this read, so there is
  nothing to fail open for.

- **A memory outage reached the model as "this cluster has no history".** When both recall channels
  failed, `recall_episodes()` returned `[]` — the same value as a genuine zero-recall. Downstream,
  `render_recall_block([])` returns `""`, so the triage prompt simply **omitted** the "Similar past
  episodes" section, and the log line read `episodes=0` exactly as it does when nothing matched.
  Neither the model nor the operator could tell that recall had failed, on the one capability this
  product is differentiated by; a Postgres blip silently degraded every investigation to
  memoryless, and absence of recalled precedent reads as absence of precedent.

  Recall now raises `MemoryUnavailable`. The agent turn still survives — an investigation without
  memory beats no investigation — but the injected context carries an explicit **"Memory
  unavailable"** block telling the model that prior history could not be checked and must not be
  reported as absent, and the log line gains `degraded=true`. A genuine empty recall is unchanged
  and still injects nothing.

  Same change fixes a second, quieter failure: `regenerate_file_plane()` fed that `[]` straight into
  the L0 projection, **overwriting a good `CLUSTER.md` / `MEMORY.md` with an empty one** on any
  recall error. It catches the new exception and leaves the previous projection intact.

- **An unreadable detector store reported "no detectors" instead of an error.**
  `review.list_detectors()` returned `[]` both when no memory pool was configured and when the
  query raised, so `GET /v1/detectors` answered `200 {"detectors": []}` in either case and
  `kq detector list` printed `No detectors.` and exited `0`. An operator asking what watches their
  cluster was told *nothing does* when the truth was that the question had not been answered — and
  the only trace was a server-side log line they would never see.

  The store now raises `DetectorStoreUnavailable`, which the endpoint translates to **503**. An
  empty list means exactly one thing: the store was read and holds nothing. This follows the
  pattern `GET /v1/findings` already used, where `sensorium: disabled` is reported rather than
  disguised as an innocent empty result.

- **`kq detector new` exited `0` when the detector was rejected.** A description the compiler
  refuses comes back as a normal `200` carrying `staged: false` and the compile errors — a valid
  response, not an HTTP failure — so `raise_for_status()` passed and the command returned success.
  The human-readable output was honest (`Not staged.` plus each error); only the machine-readable
  status lied, which is the half a script reads. `kq detector new … && kq detector promote …`
  proceeded as though a detector existed.

  It now exits **`3`** when nothing was staged, and the exit table is documented in
  `kq detector --help` and the CLI reference. This matters most for NL-authored detectors
  specifically: the author writes plain English and cannot read the compiled predicate to check
  whether it survived.

- **`kq replay` reported an unverified hash chain as intact.** The flight recorder is hash-chained
  so tampering with stored records is detectable, and `kq replay` is the command that detects it.
  The server sends its verdict as a `replay_meta` SSE frame — but the SSE reader silently discards
  any frame it cannot parse (`except json.JSONDecodeError: pass`), and an older or proxied server
  may not send one at all. In that case the command printed **no verdict line whatsoever** and
  returned **`0`**, which the documented contract defines as *"chain intact"*. Absence of evidence
  was being reported as evidence of integrity, so a script gating on `kq replay … && promote` could
  not tell a missing check from a passed one.

  It now fails closed with a new exit code **`4` — chain NOT VERIFIED**, and prints an explicit
  warning that the rendered records are unverified. Exit `0` still means verified intact and `3`
  still means broken; both were re-asserted by test. Verified empirically against four streams
  (valid, tampered, truncated verdict frame, absent verdict frame).

- **The server reported experimental features as active that no code implemented.** 25 of the 60
  declared `KI_V5_*` / `CORTEX_V5_*` flags are read by nothing — they were written when the
  configuration surface ran ahead of the implementation. Because `active_experimental_flags()`
  reported *any* true boolean carrying an experimental prefix, setting one of them made
  `GET /healthz`, `GET /v1/v5/status`, `kq v5-status` and the startup log line all state that the
  slice was on. An operator could enable `KI_V5_RIGHTSIZING`, read back that rightsizing was
  active, and be wrong — on precisely the surface used to confirm a rollout.

  Those flags are now excluded from the active set and reported separately under
  **`set_but_unwired_flags`** (both endpoints, plus `[set but NOT WIRED, no effect: …]` in the log
  line), so a setting that does nothing is visible rather than either misreported or silently
  swallowed. The list lives in `app/core/version.py` and is checked against real `settings.<FLAG>`
  usage by `tests/test_v5_flag_wiring.py`, so it can only shrink: wiring a flag without removing
  its entry fails the suite, and so does adding a new unwired one. `docs/v5-experimental-flags.md`
  marks each affected row.

  `kq v5-status` shows the same information as a red `set_but_unwired_flags` row, rendered only
  when non-empty — otherwise excluding those flags from `active_flags` would have replaced a wrong
  answer with a missing one, and the operator would read `(none — v4 baseline)` after setting a
  switch. A newer `kq` against an older server tolerates the absent field.

  All 10 `MEMORY_*` booleans were verified wired; the gap was confined to the v5 track. No
  behaviour changes for anyone who had not set one of the 25 flags — the default install reports
  an empty list exactly as before.

### Fixed
- **The agent's loop bound existed where it could not do harm, and was missing where it could.**
  The read-only RCA subagents were capped at 50 recursion units (~16 tool calls), but the
  **coordinator** — which holds the write-capable toolset — and the outer graph both inherited
  LangGraph 1.x's default `recursion_limit` of **10007** (~3,300 ReAct steps). `GraphRecursionError`
  was caught nowhere in the codebase, so exhausting that budget destroyed the entire turn with an
  uncaught exception instead of returning what had already been found.

  Both loops now carry an explicit, configurable budget — `AGENT_GRAPH_RECURSION_LIMIT` (120) and
  `AGENT_COORDINATOR_RECURSION_LIMIT` (150) — and **exhaustion halts and escalates to the operator
  with the partial result**, on both the single-turn and the streaming path. The defaults are a
  runaway backstop set well above observed usage, not a tuned budget.

  Highest-exposure path: an auto-approve session (`hitl_bypass`), where writes execute unprompted.
  The always-confirm set still gated the largest blast radius — cascading deletes of
  `namespace`/`pv`/`crd`, and `set image`/`set resources` — so this was never an unbounded-destruction
  bug; it was an unbounded *loop* issuing medium-risk writes with no ceiling and no clean failure.

  9 new tests, red-green verified: with the fix reverted, 4 of them fail — the exhaustion test with
  exactly the uncaught `GraphRecursionError` that was the defect.

### Security
- **`nanoid` bumped 3.3.17 → 3.3.18 in `v4/packages/kube-q/web`** (GHSA high: custom generators
  can loop indefinitely when size is zero). Transitive via Next.js/postcss in the web PTY relay.
  This was the **only** open Dependabot alert affecting `v4/` — `npm audit` now reports **0
  vulnerabilities** for that tree.

  The other 137 open alerts are all in `v1/`–`v3/`, which are frozen by ADR-001/002 so the
  published paper's results stay reproducible. Per the existing `SECURITY.md` policy ("About
  dependency alerts in `v1/`–`v3/`"), those are an accepted trade-off: nothing from `v1/`–`v3/`
  is published to any registry and none of it is deployable. They have been dismissed as
  *tolerable risk* with that justification, so the security tab now reflects the written policy
  instead of contradicting it.

### Added
- **CI now gates the demo front-end** (`Web (lint + build)`). `v4/packages/kube-q/web` had no
  gate of any kind: every existing job is scoped to the Python tree, so nothing in CI had ever
  run `npm ci`, `eslint` or `next build`. The gap was not theoretical — the eslint 9 → 10 bump
  (#142) reported **all 15 checks green** while `npm run lint` failed outright with
  `contextOrFilename.getFilename is not a function`, because eslint 10 removed the legacy
  rule-context API that `eslint-plugin-react` (vendored inside `eslint-config-next@16.3.0`)
  still calls. The job runs `npm ci` rather than `npm install` so a lockfile that disagrees with
  the manifest fails instead of being silently re-resolved.

### Changed
- **ruff 0.16 backlog: 362 → 210 findings** (#75), clearing every modernization family —
  `UP045`/`UP006`/`UP035`/`UP037` (typing syntax), `I001`, `PIE810`, `RUF059`, `RUF012`,
  `FURB162`, `FURB188`, `RUF015`, `SIM102`/`SIM113`/`SIM114`, `C408`, `F401`, `F841`,
  `ISC004`, `PLR0124`. Both suites (1047 + 317), mypy and the pinned lint gate stay green.

  **One finding in that set was a trap, and it is now guarded.**
  `ruff --select UP045 --fix` rewrites the canary inside
  `tests/test_injected_config_invariant.py` from `Annotated[Optional[RunnableConfig],
  InjectedToolArg]` to `Annotated[RunnableConfig | None, InjectedToolArg]` — **and the suite
  still reports 8 passed.** Neither spelling is in langchain_core's match list, so the canary
  goes on reporting `None` either way; the autofix leaves a green test that no longer exercises
  the exact form AGENTS.md invariant #6 forbids. That line now carries a suppression and the
  reasoning, matching the existing one on `app/agent/nodes/coordinator.py`, where the same
  rewrite would silently stop the run config being injected and take `user_role` and
  `hitl_bypass` with it.

  Also fixed while in there: `ki_protocol/events.py` caught `(ValidationError, Exception)`,
  which is exactly `except Exception` because `ValidationError` is a subclass — the tuple only
  made the intent read narrower than it was. The breadth is deliberate for that decoder, so it
  is now stated plainly.

  The `ruff<0.16` pin stays for now. The remainder is **141 `BLE001`** (blind-except, mostly
  deliberate CLI and agent boundary handlers) and **30 `PLW1510`**; both are project-wide policy
  calls rather than cleanup, and are tracked in #75.

### Fixed
- **The published container image could not start.** `docker run` on any released image failed
  immediately with `No module named uvicorn`, and it had been that way since the image was
  introduced. The builder stage resolved dependencies on `uv:python3.12-bookworm-slim` while the
  runtime stage ran `python:3.13-slim`. The venv is copied wholesale between them, but a venv's
  packages live at the version-stamped `lib/python3.X/site-packages` and its `bin/python` is only
  a symlink to `/usr/local/bin/python` — which resolves to the *runtime* interpreter. So Python
  3.13 looked for `lib/python3.13/site-packages`, found only `lib/python3.12`, and started with
  **no site-packages on `sys.path` at all**.

  Nothing caught it because nothing ever ran the image. `docker-publish.yml` builds, pushes and
  writes a summary; a `docker build` that succeeds proves only that the layers assembled. This is
  the same shape as the `kubeintellect --version` regression that shipped to PyPI under a green
  install-smoke job.

  The builder is now `uv:python3.13-bookworm-slim`, matching the runtime. Two guards were added
  so it cannot recur silently: the Dockerfile asserts at build time that the copied venv matches
  the running interpreter and that `uvicorn`, `fastapi`, `pydantic_core` and `pydantic_settings`
  import (this travels with the Dockerfile, so the publish path is covered too), and a new CI job
  `Container image (build + serve)` starts the image against Postgres and requires a 200 from
  `/healthz`.

  Verified end to end: the fixed image returns
  `{"status":"ok","arm":"v4",...}` and logs `Application startup complete`, where the previous
  image fails to load its own entry point.

- **The documented one-shot query form had never worked, and a failed one exited 0** (#151,
  reported by [@ybayraktarb](https://github.com/ybayraktarb) while verifying the install path on
  k3s/k3d for #100). `kq "question"` reads the first positional as a *subcommand*, so it printed
  `Unknown command` and exited 2 — in 14 places including the root `README.md` that serves as the
  GitHub landing page, the social-preview generator, the issue templates and the v2/v3 READMEs.
  All corrected to `kq -q "..."`; the one remaining occurrence, in
  `v4/packages/kube-q/docs/cli-reference.md`, is deliberate — it documents that the bare form
  fails.

  The same pass fixed the exit code behind it: authentication failures, non-200s, invalid JSON and
  exhausted retries all printed a red message and **exited 0**, so a script or CI job could not
  tell an answer from an outage. `run_single_query` now returns success as a bool and `main()`
  exits 1. A mutation paused for HITL approval returns no text and is still a success.

- **`ADOPTERS.md` has its first entry** — k3s/k3d on macOS, contributed by
  [@ybayraktarb](https://github.com/ybayraktarb) (#151). The table was honestly empty rather than
  padded; it is now honestly not.

### Fixed
- **Every playbook silently failed to load on a non-UTF-8 locale** (#136, #138 — reported and
  fixed by [@uuzzrm](https://github.com/uuzzrm)). The playbook loader read its YAML with a bare
  `path.read_text()`, which decodes using the *platform default* encoding. On Windows
  (CP1252/CP936) or under the POSIX `C` locale the em-dashes the playbooks contain raise
  `UnicodeDecodeError` — and `_load_all()`'s per-file `except` swallowed it, so the server came
  up with **zero** playbooks and nothing in the logs that looked like a failure. Now read
  explicitly as UTF-8 in the v2, v3 and v4 loaders, each with a regression test that fails if the
  encoding argument is ever dropped again.

  ⚠️ CI runs the **v4 and kube-q suites only**, so the v2/v3 regression tests are not guarded by
  CI. They were run by hand for this merge (v2 25 passed, v3 12 passed).

- **A malformed triage reply silently discarded the user's request** (#22, #133 — contributed by
  [@uuzzrm](https://github.com/uuzzrm)). The triage tier answers in strict JSON, and a reply that
  did not parse was converted straight into `{"mode": "investigate", "plan": []}`. A user who
  asked a *chat* question got a full cluster investigation instead, and nothing recorded that the
  model's answer had been thrown away. The reply now goes back to the model with a corrective
  hint, up to **3 attempts** total, before falling back to the investigate default.

  Follow-up in the same seam: `_parse_triage_json_strict` now also rejects a `plan` that is not a
  list. `triage()` does `(parsed.get("plan") or [])[:6]`, so a *string* plan sliced into six
  single characters and emitted six one-character PlanSteps — pre-existing, and now sent back
  through the repair loop instead. The echoed malformed reply is capped at 2 000 chars so a
  pathological reply cannot be re-sent on every remaining attempt and exhaust the context window.

### Fixed
- **`v4/uv.lock` was stale — it still recorded `kubeintellect 2.2.0` after the 2.3.1 bump.**
  `uv lock --check` failed against the committed lockfile (rc=1, *"the lockfile needs to be
  updated"*), so `uv sync --locked` / `--frozen` — what a reproducible release build should use —
  would have failed for three days. Re-locked, and **CI now runs `uv lock --check`** so it cannot
  drift silently again.

### Added
- **The install smoke test now probes `--version`, not just `--help`.** `--help` is not sufficient
  on its own: argparse exits 0 for `-h` even when a required subcommand is missing, so a broken
  top-level parser passes a `--help` check. That is not hypothetical — `kubeintellect --version`
  shipped to PyPI printing a usage error and exiting 2, and this job was green the whole time.
  The project's own launch pre-flight had already documented the blind spot; CI had not adopted it.

### Fixed
- **`gitops.py`'s default command runner could hang a request indefinitely.** `_default_runner`
  shelled out to `git push` and `gh pr create` with **no timeout**. Both block rather than fail
  in the common failure modes — `git push` waits on a credential prompt that will never be
  answered (there is no tty), on a half-open connection, or on a stale `index.lock`; `gh` waits
  on auth. Unbounded, that hangs the calling request with no upper limit, which is the worst
  failure shape for an incident-response tool: it fails exactly when someone needs an answer.

  Now bounded at 60s, converting a hang into an ordinary non-zero result that `open_pr`'s
  existing graceful-degradation paths already handle — a push timeout reports push failure
  rather than falsely claiming the branch was pushed. **Latent, not live**: the module is a v5
  P3 slice that nothing currently calls, so no request path was affected; it is fixed now
  because it would have become live the moment the fix-PR flow is wired up.

  Found by AST-auditing every `subprocess` call site in `app/` for a `timeout` kwarg. The other
  results are correct as they stand: the ten request-path calls all specify timeouts,
  `sensorium/k8s_watcher.py` is a deliberately long-lived `kubectl --watch`, and the remaining
  hits are in the operator-interactive admin CLI where a hang is visible and interruptible.

### Fixed
- **Readiness and liveness were the same static probe, so rolling updates dropped requests.**
  `/healthz` is deliberately static — a liveness probe that touches a dependency turns one
  database blip into a cluster-wide restart loop — but the Helm chart pointed **both**
  `livenessProbe` and `readinessProbe` at it. A replica therefore kept answering "route traffic
  to me" right up until process exit. Kubernetes removes a terminating pod from Endpoints
  asynchronously, so during that window requests were still being routed to a replica that had
  already begun closing its pools.

  Adds `GET /readyz` (`app/core/readiness.py`): 200 while serving, **503 as soon as shutdown
  begins** — the lifespan flips it *before* tearing anything down. The chart now points
  `readinessProbe` at `/readyz` with `failureThreshold: 1`, and sets
  `terminationGracePeriodSeconds` (default 45) so the drain window has somewhere to happen.

  `/readyz` deliberately **does not probe Postgres**. That would look more thorough and be more
  dangerous: when the shared database blips, every replica goes unready at once and the Service
  is left with no endpoints, converting degradation into a total outage. Dependency health
  belongs in alerting, not in a probe that controls routing. Six tests lock this down, including
  an explicit assertion that `/readyz` never touches the database, and that liveness stays 200
  while draining (failing it would have Kubernetes kill the pod mid-drain).

### Fixed
- **The four ACI read verbs declared an injected run config they could never receive.**
  `app/tools/aci/read_verbs.py` annotated the parameter
  `Annotated[Optional[RunnableConfig], InjectedToolArg]`. `langchain_core` matches the injected
  run config **by identity** (`type_ is RunnableConfig`), so the widened form is not matched and
  those tools always received `config=None` — the exact failure mode AGENTS.md safety invariant
  #6 exists to prevent, and the one `ruff`'s `UP045` actively suggests.

  **No RBAC decision was affected**: those verbs are read-only and never read `user_role`, so
  nothing failed open in practice. It is recorded as a fix rather than a footnote because the
  parameter advertised itself as carrying `user_role`, so the first RBAC check added there
  would have silently failed open. Corrected to bare `RunnableConfig` at all four sites, with
  the reasoning inline at the call site.

### Added
- **`v4/tests/test_injected_config_invariant.py` — a gate for the annotation invariant.**
  It scans every `config: Annotated[…, InjectedToolArg]` parameter under `app/` and fails if any
  is not bare `RunnableConfig`, plus canary tests proving against the installed `langchain_core`
  that the bare form *is* injected and the widened form is *not* — so the invariant's premise is
  re-proven on every run rather than assumed.

  This closes a real hole. mypy cannot catch it (both forms type-check; the correct one needs a
  `# type: ignore`), ruff cannot (the `<0.16` pin exists because `UP045` suggests the broken
  form), and behavioural tests mostly cannot — a tool that never *reads* `config` passes every
  test while silently receiving `None`, which is precisely how this survived. Red-green proven:
  reintroducing the widened form on a single parameter fails the suite with the offending file
  and line. Server suite 1023 → **1031**.

### Added
- **Hugging Face Space** — [`mskazemi/kubeintellect`](https://huggingface.co/spaces/mskazemi/kubeintellect),
  a Gradio chat client over the public read-only demo API (`deploy/huggingface-space/`). It renders
  the `ki_event` side channel (status / plan / tool_call / tool_result) as collapsible activity
  blocks, so a visitor can see the actual `kubectl` calls behind each answer. A new discovery
  surface: the repo has never had a developer-community referrer, and HF is where the adjacent
  Kubernetes-agent audience already is.

  Two things worth knowing for anyone maintaining it. The demo key holds the `readonly` role, so a
  write request is **refused by RBAC** and never reaches the human-approval prompt — the Space says
  exactly that rather than implying it demonstrates the HITL flow. And a Gradio Space on the free
  tier is pinned to ZeroGPU hardware, which aborts at startup with *"No @spaces.GPU function
  detected"*; since this app is pure I/O it carries a guarded no-op probe purely to satisfy that
  check. Downgrading to free `cpu-basic` requires a PRO subscription.

### Fixed
- **`kubeintellect --version` printed an argparse usage error instead of a version.** The
  subcommand parser is declared `required=True` and no `--version` argument existed, so the flag
  fell through to "the following arguments are required: command" and exited 2. `kq --version` had
  worked all along, which is how this survived — the two CLIs were never checked together. It now
  prints `kubeintellect <version>` resolved from installed distribution metadata (falling back to
  `unknown` in a bare source tree), matching how `kube-q` already does it. `main()` also takes an
  optional `argv` so the behaviour is testable without touching `sys.argv`, and regression tests
  cover the flag, the version string, and that a bare invocation still exits 2.


## [2.3.1] – 2026-08-15

### Fixed
- **The krew manifest could not be rendered**, so `kubectl kq` was never submitted to
  `krew-index` on the v2.3.0 release. `addURIAndSha` emits its `sha256:` continuation line at a
  hard-coded four-space indent, so the template call has to sit at four spaces itself; ours was
  nested two deeper, which put `uri:` at six and `sha256:` at four and made the mapping
  inconsistent — krew-release-bot failed with *"error converting YAML to JSON: yaml: line 55:
  did not find expected '-' indicator"*. Reindented to the canonical form and checked by
  rendering the template with a stub helper, which reproduces the failure on the old file and
  parses on the new one. The v2.3.0 release assets themselves were fine.


## [2.3.0] – 2026-08-15

### Added
- **Two more playbooks — `PvcPending` and `LivenessProbeFailing`** (#94, #95, #127, #128) —
  contributed by [@hariomlohardev](https://github.com/hariomlohardev). The library is now **23
  playbooks / 20 compiled detectors / 3 LLM-only**.

  Both arrived with a working `triggers:` block and a `detect:` block that could not fire, which
  is the [#114](https://github.com/MSKazemi/kubeintellect/pull/114) failure again: `PvcPending`
  declared `kind: PersistentVolumeClaim`, but `kind:` selects the observation *channel* and
  `WatchPredicate.matches()` only knows `Pod`/`Event`/`Node` — every other value falls through to
  `return False`. It parsed, loaded, counted toward the detector total and passed the schema
  check while being a permanent no-op. Re-seated on `kind: Event` + `involved_kind:
  PersistentVolumeClaim`, and `StorageClassNotFound` dropped because no controller emits it
  (`FailedBinding` and `ProvisioningFailed` do).

  `LivenessProbeFailing` matched `reason: ^Unhealthy$` with no message co-condition. The kubelet
  emits `Unhealthy` for **both** probe kinds, so it fired on every readiness failure as well —
  duplicating `ReadinessProbeFailing` and reporting a restart loop that was not happening. Both
  playbooks now carry the message that names the probe, and `ReadinessProbeFailing` no longer
  claims `Liveness probe failed`: a failed readiness probe pulls the pod out of Service
  endpoints, a failed liveness probe makes the kubelet restart the container. Different symptom,
  different fix.

  Guarded in `tests/test_detectors.py`: `test_every_watch_predicate_uses_a_known_observation_kind`
  is a class guard over every shipped detector for the dead-`kind:` family, plus `TestPvcPending
  Detector` and `TestProbeDetectorsDoNotCrossFire` (both directions). All five fail against the
  originals — verified by reverting.
- **Worked examples for the remaining eight `kq` subcommands** (#86–#93, #119–#126) — contributed
  by [@hariomlohardev](https://github.com/hariomlohardev). `v4/docs/examples.md` covered 2 of 10
  subcommands; it now covers all 10, as sections 10–17. Every transcript is real output, checked
  against `kube-q` 1.5.0 byte-for-byte at merge time.

  Adopted with edits. The eight PRs each numbered their section `10` and each closed with the
  same copied sentence — *"a zero-token local operation … it never contacts the server"* — which
  is true of `kq config show` and `kq completion`, and false of `replay`, `postmortem`, `export`,
  `detector`, `preference` and `v5-status`, all of which call the server. In an incident tool a
  confident wrong statement is the failure mode that matters, so each section now says what its
  command actually does. Six of the eight `cli-reference.md` anchors did not exist (`#kq-replay`
  vs the real `#kq-replay-session-id`, etc.) and are fixed; `mkdocs build` is clean. The `kq
  config show` transcript had the contributor's home directory in it, now `/home/you`.

- **The Helm chart is published — `oci://ghcr.io/mskazemi/charts/kubeintellect`.** It existed
  in-tree but had never been pushed anywhere, so there was no supported way to install the
  server. `helm-publish.yml` lints and renders the chart on every PR that touches it and pushes
  it to GHCR on each `v*` tag, taking chart `version` and `appVersion` from the tag so they
  cannot drift from the release. The chart is listed on Artifact Hub at
  [artifacthub.io/packages/helm/kubeintellect/kubeintellect](https://artifacthub.io/packages/helm/kubeintellect/kubeintellect),
  and `artifacthub-repo.yml` is pushed as a sibling OCI artifact for the Verified Publisher
  label. A chart `README.md` documents the LLM providers, the RBAC/HITL model, and the fact that
  the chart writes `metadata.namespace` from `.Values.namespace` rather than the release
  namespace.
- **Standalone `kq` binaries on every release** — linux and darwin, amd64 and arm64, frozen with
  PyInstaller and attached to the GitHub Release with checksums. They need no Python on the
  target machine (verified under `env -i`). `pipx install kube-q` is unchanged and remains the
  path for anyone who already has Python; these archives exist so downstream package managers
  have something to consume.
- **`kubectl kq` submitted to krew** — `.krew.yaml` plus a workflow that opens the
  `kubernetes-sigs/krew-index` PR on each release, guarded by a step that refuses to run unless
  all four platform archives are actually on the release, so the bot can never checksum a file
  that is not there.
### Changed
- **The memory recall similarity floor is configurable** (#14, #116) — contributed by
  [@uuzzrm](https://github.com/uuzzrm). The `pg_trgm` noise floor was hard-coded as `0.02` twice,
  independently, in `memory/episodes.py` and `memory/summaries.py`, so tuning recall for a
  cluster with an unusual vocabulary meant editing two constants in two modules and hoping they
  stayed equal. Both now read `MEMORY_RECALL_SIMILARITY_FLOOR`, a validated setting
  (`ge=0.0, le=1.0`) whose default is the same `0.02`. No behaviour change out of the box. The
  hybrid RRF path still receives the threshold in SQL and still does **not** re-apply it
  post-fetch, which is what keeps a lexical-only match from being dropped (ADR-014).
- **Cleared three ruff-0.16 rule families from the backlog** (#79, #110) — contributed by
  [@hariomlohardev](https://github.com/hariomlohardev). `UP017` (`datetime.timezone.utc` →
  `datetime.UTC`, 5 sites), `UP041` (`asyncio.TimeoutError` → `TimeoutError`, 2 sites) and
  `RUF022` (sort `__all__`, 3 sites). All three are provably no-ops here: `datetime.UTC` is an
  alias for the same object since 3.11, `asyncio.TimeoutError` **is** the builtin `TimeoutError`
  since 3.11 (the same class, not a subclass, and both sites are `except` clauses), and nothing
  iterates `__all__` in an order-dependent way. Every package declares `requires-python = ">=3.12"`.

  The four `from datetime import datetime, timezone` lines were rewritten to import `UTC` rather
  than left importing a now-unused name, which is what keeps `F401` quiet under the pinned `ruff`
  without a second cleanup pass. The deliberate `# noqa: UP017` in `kube_q/cli/store.py` is
  untouched, and no annotation was widened — `UP045` stays out of scope because on this codebase
  it silently disables RBAC and the HITL gate (AGENTS.md invariant #6). One slice of #75; the pin
  itself stays until the rest clears.

- **The Homebrew formula moved to its own tap, `MSKazemi/homebrew-kube-q`.** The install docs had
  instructed `brew tap MSKazemi/kube-q` in four places for a tap that did not exist. It does now,
  and its CI runs `brew audit --strict --online` followed by `brew install --build-from-source`
  and actually runs the binary, on every push and weekly. That immediately surfaced two
  violations the `#113` fix had missed — build dependencies must precede runtime dependencies,
  and passing `0` to `shell_output` is redundant — neither of which anyone could have seen,
  because `brew` had never once been run against the formula. The in-tree copy under
  `v4/packages/kube-q/Formula/` is deleted so the two cannot diverge; the tap also polls PyPI
  daily and only commits a version bump after that same audit-and-install gate passes.
### Fixed
- **`.env.example` was unreachable from a fresh clone** (#115, #117) — fixed by
  [@hariomlohardev](https://github.com/hariomlohardev). About fifteen places
  (`CONTRIBUTING.md`, `v4/README.md`, `v4/Makefile`, five deploy docs,
  `langfuse-provision.sh`) tell you to `cp .env.example .env`, and the file was not in the repo.
  The reported cause — a commit deleting it — was not the live one: `.gitignore`'s `.env.*`
  matches `.env.example`, so it could not be re-added at all until that pattern was negated.
  Now `!.env.example` / `!**/.env.example`, with `v4/.env.example` tracked.

  Adopted with one change: the PR restored the 244-line template from before the file was lost,
  which no longer describes the product — it omits the `qwen` and `anthropic` providers (both
  supported, and both asserted by `test_doc_claims`) and still tells you to copy Langfuse keys
  out of the UI, which `make langfuse-provision` replaced. Merged the current 289-line template
  instead. Scanned: every credential field is empty or an obvious placeholder.
- **The Homebrew formula's `desc` was still too long for `brew audit --strict`** (#113, #118) —
  fixed by [@hariomlohardev](https://github.com/hariomlohardev), who also sorted the `resource`
  blocks alphabetically and added `scripts/verify-brew.sh` so #113's two never-run commands are
  reproducible.

  Adopted with a correction: brew measures `"<name>: <desc>"`, not the description alone, so the
  PR's 75-character `desc` was still 83 with the `kube-q: ` prefix and would still have been
  flagged — and the script asserted `desc <= 80`, the wrong threshold, so it reported PASS on it.
  Both now use the real rule; the description is 78 including the prefix. `brew audit` and `brew
  install` themselves remain unrun — still no Homebrew on any machine here — so #113 stays open
  for that, but the static half is now checkable by anyone with `bash`.
- **The Homebrew formula could not install, and misstated the licence** (#56, #111) — fixed by
  [@uuzzrm](https://github.com/uuzzrm). It declared `license "MIT"` on AGPL code, pointed
  `homepage` at `MSKazemi/kube_q` (the #74/#78 defect, which the issue had not caught), targeted
  the pre-relicense 1.0.0, and carried four `resource` blocks for a seven-dependency package —
  **two of them with 63-character sha256 values**, which are not valid digests at all rather than
  merely stale ones. Now 1.5.0, `AGPL-3.0-or-later`, the canonical homepage, and the complete
  18-resource dependency tree with `certifi` taken from Homebrew.

  Verified by downloading every artifact and hashing the bytes — 19/19 match — and by resolving
  `kube-q==1.5.0` independently for Python 3.12: the closure is exactly complete, nothing missing
  and nothing extra. **`brew audit --strict` and `brew install --build-from-source` have still
  not been run by anyone**; neither contributor nor maintainer has Homebrew available, so the
  `maturin`/`rust` build path for `pydantic-core` is reasoned rather than observed. Tracked in
  #113. The formula is not a tap, so nothing is installable from here either way — the licence
  misstatement was the live defect, and it is fixed.

  The issue itself was **partly wrong** and has been corrected in place: `kube-q` 1.0.0 does
  exist (uploaded 2026-04-10, the oldest release) and its recorded sha256 matched the formula, so
  the "points at a release that does not exist" premise was false. It survived a re-verification
  because that check printed `sorted(releases)[-3:]` — the last three entries, which structurally
  cannot show 1.0.0.
- **The `kq` test suite failed depending on the contributor's terminal** (#106, #109) — fixed by
  [@floze-the-genius](https://github.com/floze-the-genius). The suite inherited `COLUMNS`, `TERM`
  and `NO_COLOR` from the invoking shell, which changed Rich's table wrapping and the theme the
  module-level console is built with. A contributor on an 80-column terminal could watch tests
  fail before touching a line of code, on a suite that was green in CI. On `2c1f676`:
  `COLUMNS=40` → 5 failed, `COLUMNS=60` → 1 failed, `TERM=dumb` → 1 failed, `NO_COLOR=1` →
  1 failed.

  The fix pins the rendering environment in `pytest_configure` — **before** test modules are
  imported — and re-applies it per test with an autouse fixture that `monkeypatch` can still
  override, restoring the invoking environment in `pytest_unconfigure`. **No assertion was
  weakened**, which was the point: the tempting fix is to loosen assertions until they pass at
  any width, which makes the suite green and stops it testing anything.

  The `pytest_configure` half is load-bearing, and
  [@AshSgDe29071999](https://github.com/AshSgDe29071999) is why that is documented rather than
  assumed. They independently diagnosed the same root cause and submitted the fixture-only
  form (#107); running it as a control showed it clears four of the five environments and
  still fails under `NO_COLOR=1`, because an autouse fixture runs after collection, by which
  point the module-level console already exists with the stripped theme. Now 312/312 on all
  five single-variable runs, on all three applied together, and on a clean environment.
- **The published `kube-q` package pointed users, and their bug reports, at the wrong
  repository** (#74, #78) — every `[project.urls]` entry resolved to `MSKazemi/kube_q`, a
  pre-AGPL snapshot that is not where `kube-q` is developed. On a package serving ~160
  downloads a month, "Homepage", "Repository" and "Bug Tracker" all led away from the canonical
  repo, and the PyPI page carried no link to the docs, the changelog, or this repository. Fixed
  by [@shaurya703](https://github.com/shaurya703), who also gave `ki-protocol` the `authors`,
  `[project.urls]` and `classifiers` it had never had — its page was blank, including **no
  licence classifier at all** on AGPL code.

  All three distributions moved to the [PEP 639](https://peps.python.org/pep-0639/) form
  (`license = "AGPL-3.0-or-later"` + `license-files`). This removes a defect the issue had not
  spotted: `kube-q` used `license = { file = "LICENSE" }`, which dumped the **entire 34 KB AGPL
  text** into the `License:` metadata header. Every wheel now reports a machine-readable
  `License-Expression` under `Metadata-Version: 2.4`, and the redundant
  `License :: OSI Approved :: …` classifier is gone — PyPI rejects an upload carrying both.

  **@shaurya703 also caught that `license-files` resolves relative to each package directory**,
  and that neither `ki-protocol` nor `kubeintellect-server` had a `LICENSE` of its own — so
  those wheels had been shipping **without the licence text**, a real compliance gap in an
  AGPL project that was invisible from the source tree. Both now carry a byte-identical copy,
  verified in the built artifacts (`twine check` passes on all six).

  The live PyPI pages stay wrong until the next publish; this fixes the source of them.
- **The `kube-q` documentation still sent readers to the old repository** — 13 files across
  `v4/` linked `MSKazemi/kube_q`, including `v4/packages/kube-q/README.md`, which **is** the
  body of the PyPI page: it told readers to `git clone https://github.com/MSKazemi/kube_q`.
  So the metadata fix above would have corrected the sidebar links while the page underneath
  still pointed elsewhere. Found by [@shaurya703](https://github.com/shaurya703) while working
  on #78, from a single `mkdocs.yml` observation. The from-source instructions now clone the
  monorepo and `cd kubeintellect/v4/packages/kube-q` (verified end to end: clone → `pip install
  -e .` → `kq --version` reports 1.5.0 — which only resolves at all now that `ki-protocol` is
  published). The mkdocs social link to `ghcr.io/mskazemi/kube_q` was a **404** and now points
  at the image that exists. The Homebrew formula's stale homepage is deliberately untouched —
  it belongs to #56.
- **Imports are sorted across `v4/`** (#76, #77) — `ruff` 0.16 enables `I001` by default and
  reported 75 unsorted blocks across 60 files, part of what blocks lifting the `ruff<0.16` pin.
  Cleared by [@shaurya703](https://github.com/shaurya703), verified at the AST level rather
  than by eye: the imported-symbol set is identical in all 60 files and there is no non-import
  code change anywhere. The one manual edit is the interesting one — ruff's autofix wraps the
  long `langchain_anthropic` import into parenthesized form, which moves
  `# type: ignore[import-not-found]` off the `from` line and breaks the suppression (mypy 0 →
  1). The ignore now sits on the `from … import (` line, keeping both the sorted form and the
  suppression. `UP045` remains untouched by design; the remaining ~364 findings are tracked in
  #75.
- **The Greetings workflow had never posted a single greeting** — it passed the
  `first-interaction` action its inputs hyphenated (`issue-message`, `pr-message`,
  `repo-token`) while the action declares them underscored. The runner exposes unknown inputs
  under their own names rather than rejecting them, so the action threw
  `Input required and not supplied: issue_message` on every issue and every pull request since
  the workflow landed; `repo-token` only appeared to work because `repo_token` defaults to
  `${{ github.token }}`. Every first-time contributor got silence plus a red X — precisely the
  two things the workflow's own header says it exists to prevent, and what #73's author saw.
  The failure was easy to dismiss as bot noise because it also fired on every Dependabot PR,
  leaving them permanently `UNSTABLE`.
- **The demo UI is ESLint-clean again** (`v4/packages/kube-q/web`, #50, #73) — 2 errors and 2
  warnings, reported by [@AdvaitVarhade](https://github.com/AdvaitVarhade), who also traced the
  two `react-hooks/set-state-in-effect` errors to `app/page.tsx`; the issue had attributed them
  to `PtyTerminal.tsx`. `PtyTerminal` now builds its status notifier inside the connection
  effect and reaches the current callback through a ref, so the effect depends on `authToken`
  alone — a re-rendered parent no longer tears down a live PTY session — and the unused
  `useState` import is gone.

  The two errors in `page.tsx` are fixed by removing the effects rather than deferring them.
  `sessionStorage` is now read through `useSyncExternalStore`, which is SSR-safe and derives the
  token during render, so the `tokenReady` state and its mount effect are both gone; and the
  auth-failure prompt is raised in the `onStatusChange` callback that causes it instead of from
  an effect watching the state that callback just wrote. Deferring the same `setState` behind
  `setTimeout(…, 0)` would satisfy the linter while making the behaviour worse — the cascading
  render still happens, now after paint, so the terminal pane visibly flashes empty on every
  load. Verified in a browser as well as by `npm run lint` and `npm run build`: the terminal
  mounts, connects, and propagates status, with no hydration or `getSnapshot` warning under
  dev StrictMode.

  The ref that carries the callback is updated from an effect rather than during render, since
  React may discard a render and a ref written there would outlive it. The xterm-init failure
  path now builds its message as a text node instead of assigning `innerHTML`.

- **The Helm chart could never pull its image.** `values.yaml` defaulted `image.tag` to
  `dev-latest`, a tag that has never existed in the registry — GHCR has `latest` and `2.0.0`
  through `2.2.0` and nothing else — so a default `helm install` went straight to
  `ImagePullBackOff`. The tag now defaults to the chart's `appVersion`, and a CI step renders the
  chart and asserts the image matches, so it cannot regress silently. The same dead tag was
  copied into all seven per-cloud `values-*.yaml.example` files and is corrected there too.
- **The container image had never reached Docker Hub.** `docker.io/kazemi/kubeintellect` did not
  exist. Nothing was structurally broken: the `v2.2.0` tag run predated the credentials being
  added, and both later runs were dry runs, which log in to both registries (proving the
  credentials) while pushing nothing. The image is now published.
### Added
- **An `HPANotScaling` playbook** (#97, #114) — the 21st, contributed by
  [@Chris7717](https://github.com/Chris7717). It covers the autoscaler that does nothing and
  looks identical to one that is simply not needed, and it separates the two root causes that
  Kubernetes reports through the *same* `FailedGetResourceMetric` /
  `FailedComputeMetricsReplicas` events but that need different fixes: metrics-server missing or
  unreachable (cluster-wide) versus a container with no `resources.requests.cpu`
  (workload-specific). Both were reproduced against a kind cluster, so the
  `investigation_steps` and `expected_evidence` are transcribed from real `kubectl` output
  rather than reasoned. It compiles to a detector — 18 of the 21 playbooks now do.

  Adopted with two maintainer fixes to the `detect:` block, neither of which any existing gate
  could see. The `reason_regex` was `"^(FailedGetResourceMetric | FailedComputeMetricsReplicas)$"`:
  the spaces around the alternation bar bind to the branches, so the compiled predicate required
  an event reason *containing a space* and could therefore never match anything — the detector
  loaded, counted, and passed the schema check while being a permanent no-op. The stored PromQL
  named `kube_horizontalpoduatoscaler_status_condition` (transposed) and matched `status="False"`,
  where kube-state-metrics emits the status label lower-case; that arm is latent until #20 lands,
  but it was stored wrong.

  The gate gap mattered more than either typo: the PR's tests exercised `match_playbooks()` — the
  prompt-side `triggers:` path — and nothing anywhere asserted that a compiled predicate can
  actually *fire*. `test_detectors.py` now checks this playbook against both real event reasons,
  and a new class guard rejects any shipped `reason_regex` whose alternatives carry surrounding
  whitespace, since a Kubernetes event reason never contains a space. Both fail against the
  original file.

- **A `DeploymentRolloutStuck` playbook** (#98, #112) — the 20th, contributed by
  [@hariomlohardev](https://github.com/hariomlohardev). It covers the rollout that fails
  *without an outage*: the new ReplicaSet never becomes Available, the old one keeps serving
  traffic, and nothing looks broken from the outside until someone asks why the deploy never
  finished. It compiles to a detector (17 of the 20 playbooks now do), with a 600-second
  debounce so a slow-but-progressing rollout does not fire it.

  The part worth copying is that it **routes rather than duplicates**: `ProgressDeadlineExceeded`
  is almost always a symptom, so the investigation steps chain to `ReadinessProbeFailing`,
  `PendingInsufficientResources`, `PendingSchedulingConstraints` and `ImagePullBackOff` for the
  actual cause, and the fix template opens with "do **not** force-delete pods." The fourth
  `expected_evidence` entry names the condition under which the playbook does *not* apply,
  which is a discipline none of the previous nineteen had.

- **A `NetworkPolicyBlocking` playbook** (#99, #108) — the 19th, contributed by
  [@Priyanshu608](https://github.com/Priyanshu608). It covers the one failure in the set that
  emits **no evidence at all**: a NetworkPolicy denial is discarded in the CNI datapath, so the
  API server records no event and no Warning is ever produced. The Service is healthy, endpoints
  exist, DNS resolves — and the connection simply hangs. The playbook makes that *absence* the
  signal, which is what none of the other 18 could express.

  Two maintainer corrections landed on top. The submitted trigger also matched
  **`connection refused`**, which is evidence *against* a policy drop: a refusal means a TCP RST
  came back, so the packet was delivered. Matching it would have pointed the agent at a
  NetworkPolicy for what is almost always a wrong port or a missing endpoint —
  `ServiceUnreachable`'s territory. It now matches timeout signatures only, and
  `test_networkpolicy_blocking_ignores_connection_refused` locks that in. The `triggers:` block
  was also nested under `detect:`, where the loader never reads it
  ([`loader.py`](v4/packages/kubeintellect-server/app/agent/playbooks/loader.py) takes
  `triggers` from the top level), so the playbook loaded cleanly into the registry and then
  never fired — inert, with no error anywhere. `test_each_playbook_has_complete_schema` catches
  exactly this and did.

- **The test suites now run on Python 3.13 as well as 3.12** (`Tests (… · py3.13)`), closing
  a gap where the project was verified on an interpreter it does not ship. `v4/Dockerfile`'s
  runtime stage is `python:3.13-slim` and all three distributions declare
  `requires-python = ">=3.12"`, so every container and every `pip install` on a current
  machine already ran 3.13 — while CI pinned 3.12 in every job. Both suites pass unchanged
  (990 server + 312 `kq`), so this adds coverage rather than fixing a break; the point is that
  a future 3.13-only regression now fails a gate instead of reaching users. Added as a
  separate job rather than a `python-version` matrix axis on purpose: the axis would rename
  `Tests (server)`, and branch protection matches required checks by name. The
  `Programming Language :: Python :: 3.13` classifier on the server distribution is now
  earned rather than assumed.
- **A `Syntax warnings` CI gate (`make check-syntax`, `scripts/check-syntax-warnings.py`)** —
  compiles every tracked Python file outside the frozen v1–v3 trees with `SyntaxWarning`
  promoted to an error, on the newest supported interpreter. This closes the structural blind
  spot that let #63 reach an outside contributor: the pinned `ruff` does not report an invalid
  escape sequence in the CI-linted scope, `mypy` never compiles source, and pytest triggers the
  warning only on a cold `.pyc` cache — so a green suite was not evidence either way. The
  defect class is not cosmetic: #63's non-raw string was also corrupting the jsonpath examples
  in the coordinator prompt. Like the `File modes` gate it is deliberately dependency-free
  (stdlib only, no `uv sync`) so it stays correct however the `ruff` pin is eventually lifted,
  and it uses `compile()` rather than `compileall` so it leaves no `.pyc` files behind.
  `make setup` now runs all six gates instead of four.
- **A `File modes` CI gate (`make check-modes`, `scripts/check-file-modes.sh`)** enforcing one
  invariant outside the frozen v1–v3 generations: *a tracked file is executable if and only if
  it starts with a shebang*. This closes a structural blind spot rather than a one-off mess —
  `ruff` is pinned `<0.16` and `EXE002` only became a default rule in 0.16, so the lint
  gate could not see a stray `+x` bit at all, which is how 94 library modules silently acquired
  one. The guard is deliberately dependency-free (git + coreutils, no `uv sync`), so it stays
  correct whichever way the ruff upgrade lands, and it runs in seconds. It also covers the
  inverse defect (`EXE001`: a shebang'd script that is not executable, i.e. one you cannot run),
  which `ruff` only reports for Python. `make fix-modes` corrects violations in place, updating
  both the working tree and the index — a `chmod` alone would leave the committed mode unchanged.
- **One-command contributor setup: `make setup` (`scripts/dev-setup.sh`).** Installs `uv` if
  missing, installs the `v4` workspace, then runs the *exact* four gate commands CI runs
  (ruff, mypy, server suite, `kq` suite) and reports which passed. A contributor therefore
  learns their environment is correct *before* changing anything, and never debugs their setup
  and their change at the same time. It also names the pre-existing debt that is **not** their
  bug (`make lint`'s non-gate `ruff format --check`, the deliberate `ruff<0.16` pin). No
  cluster, Docker daemon, or LLM API key is required — the suites are mocked.
- **`.devcontainer/devcontainer.json`** — zero-install contribution via GitHub Codespaces or
  VS Code Dev Containers, pinned to the Python 3.12 that CI pins and running the same setup
  script on create.
- **`AGENTS.md`** ([agents.md](https://agents.md/) format) — machine-readable repository rules
  for AI coding agents: gate commands, the six safety invariants, testing expectations, and
  the pre-existing debt not to "fix". Human authority remains `CONTRIBUTING.md`.
- **A `Contributing` section on the README front page**, plus a live good-first-issue badge and
  a header nav link. The invitation previously existed only as one sentence under `Maintainer`
  near the bottom of a 221-line file.
- **`.github/workflows/greetings.yml`** — welcomes a contributor's first issue and first PR,
  and pre-empts the two things that most often make newcomers give up: silence, and a fork PR
  that appears stalled because GitHub runs no CI for first-time contributors until a maintainer
  approves the run.
- **`.github/workflows/labeler.yml` + `.github/labeler.yml`** — path-based `area/*` PR
  labelling mirroring the issue taxonomy in `TRIAGE.md`, so a contributor never needs to know
  the repo's internal structure to be routed correctly.
- **`.github/ISSUE_TEMPLATE/documentation.yml`** — a documentation issue template, making
  "the docs were unclear" a first-class report rather than a bug-report misfit.
- **A `Contributing` section in `llms.txt`**, so answer engines asked how to contribute to
  KubeIntellect have a grounded answer (Python-3.12-only prerequisite, `v4/` scope, the HITL
  invariant, and where the good first issues are).

- **`kq export <session-id>` — export a diagnosis report to JSON or YAML.** Serializes the
  same grounded postmortem `kq postmortem` renders (a view over the hash-chained decision
  log) for archiving, ticket attachments, or downstream tooling. `--format json|yaml`,
  `--output PATH`. Stdout stays machine-parseable (notes and warnings go to stderr), so
  `kq export <id> | jq` works. Exit codes follow the `kq replay` convention: `3` when the
  audit chain is broken, and `4` when the recorder holds no events for the session — in
  which case nothing is written, rather than emitting an empty-but-plausible report.
  Closes #58. Thanks to @AdvaitVarhade for proposing the capability and the initial
  implementation.

- **`Types (mypy)` is now a blocking CI gate**, and `make typecheck` runs it locally. The
  workspace type-checks with **zero errors** across all 171 source files, down from 30. The
  remaining `ruff format` debt is unaffected and still not a gate.
- `tests/test_workflow_config_injection.py` — a regression guard asserting that every graph
  node and tool declaring a `config` parameter actually receives the run config. See below
  for why that is not obvious.

### Fixed
- **Cleared the stray executable bit from 94 source files** under
  `v4/packages/kubeintellect-server/app/` and `v4/packages/ki-protocol/` — file-mode only
  (`100755 → 100644`), zero content changes, verified by the patch containing no `+`/`-` content
  lines. These are library modules with no shebang, so `chmod -x` is the correct fix rather than
  adding one. Clears `EXE002` in the CI-linted scope and takes the ruff 0.16 finding count from
  438 to 342, unblocking part of #64. Thanks to [@hariomlohardev](https://github.com/hariomlohardev)
  ([#70](https://github.com/MSKazemi/kubeintellect/pull/70)).
- **Extended the same mode-only sweep to the remaining 282 files** outside that lint path
  (the rest of `v4/`, `deploy/`, and three root files), and gave the four shebang'd scripts that
  were *not* executable their `+x` back. The frozen v1–v3 generations are deliberately untouched
  (ADR-001/002): they are closed to changes and not built by CI, so rewriting ~500 of their file
  modes would be churn against immutable history for no gate benefit.
- **The coordinator system prompt was silently corrupted.** `_COORDINATOR_SYSTEM` was a
  non-raw string containing jsonpath separators, so `{"\n"}` and `{"\t"}` were interpreted
  before the model saw them — a real newline split an example `kubectl get pods -o jsonpath=…`
  command mid-line, and a literal tab replaced another. Now a raw string. This also clears the
  `SyntaxWarning: invalid escape sequence '\`'` that Python is scheduled to turn into a
  `SyntaxError` (#63).
- **RCA synthesis crashed on providers that return content blocks.** `_synthesize` called
  `response.content.strip()`, which raises `AttributeError` when `content` is a list rather
  than a string; it now uses `response.text`, which flattens the blocks.
- **API keys are wrapped in `SecretStr`** before being handed to `ChatOpenAI` /
  `AzureChatOpenAI`, so a client repr or traceback cannot render the raw key.
- `init_graph()` raises a named error when `POSTGRES_DSN` is unset with `USE_SQLITE=false`,
  instead of failing inside the driver.
- **Four type errors in `app/cli.py`** (#55): `callable` used as a type annotation is now
  `Callable[[], None]`, and the two `subprocess.run` calls that rebound a
  `CompletedProcess[str]` variable declare `text=True`, so the type is consistent without
  changing runtime behaviour. Thanks to @hariomlohardev for the fix (#57).
- Dropped an f-prefix from a placeholder-free string in `v4/scripts/fix_pr_probe.py` (F541).

### Changed
- **The PyPI release is unblocked — all three distributions are published and current** (#66,
  closed). `ki-protocol` **1.0.0**, `kube-q` **1.5.0**, `kubeintellect` **2.2.0**, all reporting
  `AGPL-3.0-or-later` instead of the stale `MIT` metadata. Verified in clean venvs:
  `pip install kube-q` yields 1.5.0 with all ten subcommands (checked *without* `--help`, per
  the argparse trap), and `pip install kubeintellect` yields 2.2.0 with a working entry point.

  Two defects surfaced while unblocking it, neither of which the issue anticipated:
  **(1) `kube-q`'s trusted publisher authorized the wrong repository** — `MSKazemi/kube_q`
  rather than `MSKazemi/kubeintellect` — so the root `publish.yml` could never have published
  it, and confirming only the *workflow filename* would not have caught it.
  **(2) A trusted publisher registered without an environment does not match a workflow that
  runs with one.** `kubeintellect`'s publisher said `Environment: (Any)` while `publish.yml`
  runs `environment: pypi`, and every upload failed with `403 OIDC scoped token is not valid
  for project 'kubeintellect'` while the two publishers naming `pypi` explicitly succeeded in
  the same run. Registering a second publisher with the environment set explicitly fixed it.
- **README's PyPI warning block is gone** — it existed only while the published packages were
  behind, and both install paths now work as documented. The server quickstart also no longer
  claims `kubeintellect init` "creates a Kind cluster, deploys samples"; verified against the
  published 2.2.0 CLI, `init` writes `~/.kubeintellect/.env` and cluster creation is the
  separate `kind-setup` command.
- **`TRIAGE.md` no longer tells contributors that `mypy` is non-blocking debt.** It became a
  required CI check in `0c7b055`; a contributor following the old text would have pushed a PR
  expecting a `mypy` failure to be ignorable and had it blocked instead.
- `CONTRIBUTING.md` leads with the one-command path and states up front that **no cluster,
  Docker daemon, or LLM API key is needed** to run the suites — previously the requirements
  list implied all three were mandatory before you could contribute a typo fix.
- `types-PyYAML` added to the dev dependency group; the server's mypy baseline drops from
  29 to 27 errors (two pre-existing missing-stub errors resolved).
- **First `[tool.mypy]` configuration for the workspace** (#53) — `python_version = "3.12"`
  plus a per-module `ignore_missing_imports` override for `asyncpg`, which ships no `py.typed`
  marker. Clears the 5 remaining `[import-untyped]` errors without touching source. Thanks to
  @hariomlohardev (#65).
- `max_tokens=` → `max_completion_tokens=` on the OpenAI/Azure chat clients — the same
  pydantic field under its public alias, so the request payload is unchanged.
- Container runtime image moves from `python:3.12-slim` to `python:3.13-slim` (#59).
  Verified independently of CI, which does not build the image: the full dependency set
  resolves on CPython 3.13.14, both entry points start, and the 986-test server suite passes.
- `uvicorn[standard]` floor raised `>=0.32` → `>=0.52.1` to match the resolved version (#61).

## [2.2.0] – 2026-08-08

### Documentation
- **Canonical repo consolidated to `MSKazemi/kubeintellect`.** Repointed the README star badge
  (was rendering the org mirror's 0-star count), `CITATION.cff` `repository-code`, and the four
  `llms.txt` doc links from the `kubeintellect/kubeintellect` org mirror to the personal repo, which
  holds the organic stars. Ends the star-count fragmentation between the two identical public repos
  (org mirror to be redirected + archived).
- **v4 product docs — major quality pass.** Audited every page against the code and fixed
  drift (homepage playbook stat 10→18, `KUBE_Q_URL` default, `AZURE_OPENAI_API_VERSION`,
  demo-key TTL vars, a misplaced CLI flag table); documented the previously-undocumented
  `/v1/preferences` API and the `kq config` command group; added new **FAQ**,
  **Upgrade & feature-flag guide**, **Examples & cookbook**, and **Changelog** pages; deepened
  the quickstart with an end-to-end happy path; added front-matter descriptions to 13 pages;
  fixed all broken cross-page anchors; relocated the Qwen-hackathon submission collateral out
  of the product docs (to `v4/hackathon-submission/`) and clarified the Install-vs-Deploy nav
  split. `mkdocs build` is clean.
- **Investor pack + industry white paper refined (private, not in the public mirror).**
  Consistency/honesty pass on the fundraising docs (numeric cross-checks against the financial
  model, explicitly-labelled illustrative assumptions, real cited market sources — CNCF 2025,
  AIOps market size — with honest `[SOURCE NEEDED]` gaps, no fabricated traction) and the LaTeX
  white paper (MAPE-K/autonomic-computing citation, an evaluation results figure, benchmark-
  scoped detection claims, trimmed redundancy); all PDFs rebuild clean.

### Added
- **Snap packaging for the `kq` CLI.** New `snap/snapcraft.yaml` builds a `kubeintellect` snap
  (core24, amd64 + arm64) carrying the terminal client, with `snap/README.md` documenting the
  build, the confinement trade-offs, and the store-publishing steps. Deliberately **strict**
  confinement rather than the `classic` that `kubectl`/`helm`/`k9s` use: `kq` is an HTTP client
  whose filesystem needs are two known directories, so they are requested as explicit
  `personal-files` plugs — `dot-kube` (read `~/.kube`) and `dot-kube-q` (write `~/.kube-q`) —
  since the `home` interface excludes top-level dot-directories. `HOME` is remapped to
  `$SNAP_REAL_HOME` so `Path.home()` resolves outside the sandbox. Packaged as a single `python`
  part that vendors the client past its Next.js demo UI (sourcing `packages/kube-q` directly
  would drag ~600 MB of `node_modules` through the pull step) and installs it together with
  `ki-protocol` in one `pip` call, because `ki-protocol` is not on PyPI yet. Not published to
  the Snap Store yet — the name is unregistered and `personal-files` needs an approved snap
  declaration.
- **Snap CI.** `.github/workflows/snap.yml` builds both architectures on any PR touching
  `snap/`, `kube-q`, or `ki-protocol`, installs the built snap on the runner and smoke-tests
  `--version`, `--help`, and completion generation; publishing is a separate manually-dispatched
  job gated on a `SNAPCRAFT_STORE_CREDENTIALS` secret.
- **Community: adoption and prioritisation surfaces.** `ADOPTERS.md` (honestly empty rather
  than padded) plus a pinned adoption thread (#51), and a pinned roadmap voting index (#52)
  that makes 👍 reactions the explicit prioritisation signal for the "Next" list; the six
  roadmap issues carry a new `roadmap` label. New `adoption`, `needs-info`, and
  `area/packaging` labels.
- **Community: `SUPPORT.md` and `TRIAGE.md`.** `SUPPORT.md` routes questions to the right
  channel, says what to include, and states plainly that there is no SLA. `TRIAGE.md`
  documents the full issue lifecycle — the five triage questions, what every label means and
  what it promises, how to claim an issue, the two-week unassignment rule, and what each close
  reason means — written so that a non-maintainer can triage without repo permissions.
- **Container image publishing to GHCR and Docker Hub.** New `.github/workflows/docker-publish.yml`
  builds the v4 image once and publishes it to `ghcr.io/mskazemi/kubeintellect` and
  `docker.io/kazemi/kubeintellect`, tagged by semver plus `latest` and the commit sha, with OCI
  labels and `VERSION`/`GIT_SHA` build args. Triggered by a `v*` tag or manually, with a dry-run
  option. Docker Hub is skipped with a warning until `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN` are set.
  Private material cannot reach the image: CI checks out the public repository, the build context is
  `v4/`, and the Dockerfile copies only the three workspace package source trees — verified against a
  local build, whose image contains just `app/`, `ki_protocol/` and `kube_q/`.
- **Doc-claims drift guard (v4).** New `v4/scripts/check_doc_claims.py` reads the canonical
  numbers straight from code — 18 shipped playbooks (loader), 16 baseline compiled detectors
  (`load_detectors()`), the valid LLM-provider set (`config.py`), and the count of `KI_V5_*`
  flags — and asserts every numbered claim in the docs still matches, exiting non-zero on drift.
  Wired as `make docs-check` and enforced by `tests/test_doc_claims.py`. It caught a real drift:
  the `api-reference.md` `/v1/findings` example said `"detectors": 18` (the playbook count) and is
  now corrected to `16` (what the endpoint actually reports at baseline). Chosen as a *check* rather
  than a prose auto-generator so hand-written docs are guarded, not clobbered.
- **Documentation standardized across all five versions (v1–v5).** New
  `design/adr/002-standard-doc-surface.md` defines a canonical doc surface + mkdocs nav +
  metadata standard (the doc analog of ADR-001); every version's docs were brought to it
  as-built. v3 gained 7 missing canonical pages (cli-reference, api-reference, capabilities,
  troubleshooting, operations, glossary, memory); verified-against-code accuracy fixes landed
  across v1 (agent count 13), v2 (18 playbooks), v3 (2 providers, 4 role tiers, DeepAgents
  agent-behaviors), and v4 (18 failure detectors, `openai|azure|qwen|anthropic` providers).
  Root `README.md` now documents the full v1→v5 lineage; `v5/README.md` added (design tier).
- **Architecture reference extended to five generations.** `architecture-comparison/`
  (both the Markdown edition and the LaTeX/PDF, now 31 pp) gained code-grounded **V4**
  (platform) and **V5** (design-tier) chapters, a V4 architecture diagram, and updated
  lineage/comparison/module-map tables — one as-built v1→v5 technical reference.
- **Cortex mypy cleanup (quality gate).** Full-gate audit (1251 tests + ruff green) found and fixed 3 latent type issues in `app/cortex` (synthesize `messages: list[BaseMessage]`, remember `isinstance` narrowing, optional `langchain_anthropic` import-ignore); `mypy app/cortex` now clean, no behavior change.
- **v5 experimental-flags reference.** New `docs/v5-experimental-flags.md` catalogs all ~60 `KI_V5_*`/`CORTEX_V5_ENABLED` flags (flag / default / purpose, generated from `config.py`), linked from `configuration.md` — one place to see every additive default-off toggle.
- **`/v1/v5/status` API-reference entry + boot-time version log.** Documented the endpoint (fields + example) in `docs/api-reference.md`; the server now logs its full version identity + active v5 flags at startup (ADR-019).
- **`kq v5-status` CLI + CLI-reference entry.** Terminal view of the v5 trust plane (version, active flags, kill switch / change freeze / spend cap) via `GET /v1/v5/status`; documented in `docs/cli-reference.md`.
- **v5 trust-plane observability + arm-3 calibration harness.** New `GET /v5/status` surfaces version identity, active `KI_V5_*` flags, and the fail-closed brakes (kill switch / change freeze / spend cap) in one read-only call. Added `calibrate_offline_weight` (ADR-102 arm-3 calibration harness; simulation-validated, production cap value awaits real matched offline+live shadow data).
- **v5 fix-PR write class validated END-TO-END with a real PR.** The misconfig fix-PR flow (repair → fix_pr → push → PR) opened a genuine GitHub pull request (MSKazemi/kubeintellect-private#1) proposing `runAsNonRoot: false → true` — the P3 first write class exercised against a live remote, not just locally.
- **v5 data-source activation (additive, default-off).** Agentic-workload + GPU-health metric collector (queries Prometheus for agent tool-call rate/cost, sandbox-escape, ResourceClaim/ECC/GPU-OOM → runs the predicates; active/dormant-until-breach) and a Postgres fleet-signal store that pools per-cluster signals into fleet-wide pattern detection (LIVE-validated on real PG: 3 clusters → critical FleetAlert, tenant-isolated).
- **v5 integration-activation (additive, default-off).** Pre-capture wired into the live watchtower
  (imminent predicted finding → arm recorders before death); change-watchdog loop activated in the
  consolidation worker (changes-since-last-sweep → read-only fan-out investigation, timestamp-deduped)
  — the P4 anticipation loop now runs end-to-end (agent change → ledger → sweep → investigate-the-diff).
- **v5 loop-closing + fleet slices (additive, default-off).** P3: spend usage source (OTel token
  spend → deny-before-breach, closes the spend-budget loop) and blast-radius composite gate (budget
  + staged-propagation + failure-domain in one fail-closed verdict). P5: fleet-wide signal pooling
  (same signal on ≥N clusters/tenant → FleetAlert, tenant-isolated) and fleet-store RLS tenancy
  scaffolding (ADR-105 policy defined, disabled pending GUC-binding). Each behind a `KI_V5_*` flag.
- **v5 P3/P4 hardening slices (additive, default-off).** P3: failure-domain + change-schedule budget
  (per-zone unavailability caps ≤~⅓ + maintenance windows, REQ-sysadmin-18). P4: NL-detector →
  statistical ladder (ADR-012 shadow-precision gate, human review retained), agentic-workload SRE +
  GPU-health detectors (agent-runaway/sandbox-escape + GPU/ResourceClaim health, the incumbent-empty
  surface D17/SD-D), predictive pre-capture (arm recorders before an imminent predicted death,
  A-CH-20-16). Each behind a `KI_V5_*` flag.
- **v5 P3 Trust plane + P4 Anticipation + P5 Fleet (additive, all default-off; validated live where a
  cluster/DB/LLM applied).** P3: blast-radius/spend budget gate (kill switch, change freeze, spend
  cap), statistical promotion engine (ADR-102) + Postgres outcome store, mutating-verb chokepoint
  (rollback classification + write-authority), server-side dry-run, machine-checkable postcondition
  oracle, transactional apply→verify→auto-rollback (TNR), two-axis capability sandbox (SA
  impersonation), security-outcome gate, misconfig LLM auto-repair + fix-PR generator + GitOps PR
  opener, staged propagation (never instant-global), failure-domain + change-schedule budget. P4:
  heterogeneous model routing + air-gap read-only floor (ADR-103), per-change ephemeral watchdog +
  fan-out dispatch, evidence-grounded rightsizing, predictive-detection fusion. P5: cross-cluster
  fleet memory exchange with strict tenant isolation + Postgres-backed durable store (ADR-105). Every
  slice gated behind a `KI_V5_*` flag (default off ⇒ byte-identical V4). LIVE-validated on n1 Kind +
  Azure gpt-4o + Postgres: P3 write path (real mutation + rollback + RBAC), fix-PR (Azure + git),
  promotion loop (real PG), fleet isolation (real PG). ADRs 101/102/103/105 technical gates met
  (awaiting owner ratification); ADR-104 deferred.

### Fixed
- **`CONTRIBUTING.md` gave commands that do not exist.** The quality-gate section told
  contributors to run `uv run mypy src` (there is no `src/` in the workspace layout) and
  `uv run ruff check .` (CI lints only `packages/kubeintellect-server/app/` and
  `packages/ki-protocol/`); both are replaced with the exact commands from `ci.yml`, plus a note
  that `make lint`'s `ruff format --check` is not a CI gate. The dev-setup snippet also still
  claimed the archived org mirror was canonical. Added a worked first-PR walkthrough against a
  real open issue.
- **Container image licence label corrected (legal).** `v4/Dockerfile` labelled the image
  `org.opencontainers.image.licenses="MIT"` while the project is AGPL-3.0. Every published image
  would have misrepresented its terms. Now `AGPL-3.0-only`, with `url` and `documentation` labels
  added and the `source` URL cased to match the canonical repo. Caught while inspecting the built
  image before enabling public publishing.
- **v3 HITL fail-open closed (safety).** `v3/app/agent/hitl.py` approval/denial detection now
  matches a leading decisive token, not only exact whole-message phrases — a multi-word denial
  ("no don't do that") is no longer silently treated as approval by the `resume = not is_denial(...)`
  resume gate. Part of the 2026-07-10 v3 code-improvement batch (registry single-source-of-truth,
  bounded `deepagents`, memory/429/snapshot truncation logging, `SNAPSHOT_MAX_CHARS` config) —
  full detail in `v3/CHANGELOG.md`.

## [2.1.0] – 2026-07-05

### Added
- **v5 P0 foundations + P2 investigation-core (additive, all default-off; ADR-019 → v4.x, NOT a fork).**
  P0: K8s-ACI v0 read verbs, the ADR-101 harness subagent contract + read-only fan-out seam and body,
  OTel GenAI spans as hash-chained `decision_log` rows, Wilson-LCB promotion-stats (ADR-102), and the
  OpsMemBench deterministic core + live driver. P2: adversarial verification ladder, never-silent
  responsiveness heartbeat + latency budget, escalation-avoidance briefs, and runbooks-as-skills. Every
  slice is inert unless its `CORTEX_V5_ENABLED` + `KI_V5_*` flag is set (flags off ⇒ byte-identical V4).
  Validated live on a Kind cluster (35/35 probes).
- **Version identity surface (ADR-019).** `GET /healthz` now returns the three version axes — `arm`
  (the `KI_VERSION` generation), `version` (the package SemVer, which is what distinguishes v4 / v4.1 /
  v4.2), and the active `experimental_flags` — so a running instance is fully identifiable. New
  `app/core/version.py` (`version_info` / `version_line`). Server SemVer **2.0.2 → 2.1.0**.

### Added
- **Qwen Cloud support (Qwen Cloud Hackathon — MemoryAgent track).** `LLM_PROVIDER=qwen`
  is a first-class provider that auto-targets Alibaba DashScope's OpenAI-compatible
  endpoint (`https://dashscope-intl.aliyuncs.com/compatible-mode/v1`) with `qwen-max`
  (coordinator/synthesis) + `qwen-plus` (parallel RCA subagents); `DASHSCOPE_API_KEY` /
  `QWEN_API_KEY` are honest aliases for the key. `OPENAI_BASE_URL` is now honored by the
  OpenAI client factory (covers Cortex too). `scripts/verify_qwen.py` checks live chat +
  tool-calling. Cost table extended with qwen-max/plus/turbo pricing.
- **Operator-preference memory (MemoryAgent).** `app/memory/preferences.py` upgrades the
  thin `user_prefs` table into a learned layer: explicit (confidence 1.0, immortal) +
  behaviour-**inferred** preferences (e.g. `default_namespace` from RCA history) with
  confidence, decay, and **forgetting** (`preference_purge()`), learned/forgotten by the
  consolidation worker and injected into the prompt. New `kq preference set/list/forget`
  CLI + `GET/PUT/DELETE /v1/preferences` API.
- **Memory V5 upgrade — hybrid recall + bi-temporal knowledge graph (behind default-off
  flags; MemoryAgent).** Grounded in a state-of-the-art study (`design/memory-v5/`: 619
  systems / 1023 resources / ADRs 013–018). Two additive, flag-gated slices ship inside V4:
  (1) **`MEMORY_HYBRID_RETRIEVAL`** — episode recall fuses the `pg_trgm` channel with a
  full-text `ts_rank` channel via Reciprocal Rank Fusion (RRF, k=60) in one query, with a
  functional FTS GIN index (`idx_episodes_fts`, no table rewrite) and graceful fallback to
  the trigram baseline. (2) **`MEMORY_BITEMPORAL_ENABLED`** — the temporal KG gains a
  transaction-time axis (`ingested_at`/`retracted_at`): event-time `valid_from`, point-in-time
  `as_of(valid_t, tx_t)` queries, retract-not-delete supersede (audit-preserving), and a
  `mean_ingest_lag_seconds` freshness signal. (3) **`MEMORY_KG_PPR`** — multi-hop
  blast-radius over the KG: a bounded ≤3-hop induced subgraph via a recursive CTE, then
  Personalized PageRank computed **in-process** (dependency-free power iteration), so
  `kg.ppr_blast_radius(seeds)` ranks the entities most related to an incident. (4)
  **`MEMORY_WRITE_RECONCILE`** — Mem0-style write reconciliation: `kg.reconcile_edge()`
  decides ADD/UPDATE/**RETRACT**/NOOP against existing memory behind a query-independent
  salience gate (dedup + supersede); RETRACT sets `retracted_at` (never hard-deletes) and
  defaults to ADD when confidence is low. (5) **`MEMORY_PROMOTION`** — the learning loop: the
  consolidation worker promotes verified, recurring episodes into a new `semantic_rules` table
  (IF-context → THEN-guidance); a rule that recurs enough goes `active` (injected into the
  prompt) and is eligible to seed a detector *candidate* (reusing the existing human-review
  flow). (6) **`MEMORY_IMPORTANCE`** — importance/surprise-weighted retention (ADR-017): each
  episode write is scored for `importance` (incident severity — regression > partial > resolved,
  boosted by verified + confidence) and `surprise` (a KG-novelty proxy), stored on new
  `episodes.importance`/`surprise` columns; recall then ranks **recency × importance ×
  relevance** (importance modulates *ranking only*, never retention/audit), and a surprise gate
  drops redundant *low-value* auto-writes (unverified + report-only near-duplicates) while always
  keeping verified/actioned episodes. (7) **`MEMORY_PROSPECTIVE`** — first-class prospective
  memory (ADR-017): a new `prospective_memory` table lets the watchtower record a "re-check
  condition C at/after T" after an autonomous fix ("did the fix hold?"); the consolidation
  scheduler claims due re-checks (atomic `FOR UPDATE SKIP LOCKED`), fires each through the
  autonomy ladder (A0 namespaces never fire), and records the outcome. (8)
  **`MEMORY_SECURITY_HARDENING`** — security-hardened write path (ADR-018): defends the top
  threat the study surfaced, **MINJA-style query-only memory injection**, with a
  write-admission guard of **diverse, non-LLM-primary** validators (design review F5: a quorum
  of the same LLM fails together under MINJA) — provenance/**trust** scoring (sensor = ground
  truth, user-chat = low-trust), a heuristic persistent-instruction injection-signature check,
  a per-requester sliding-window rate limiter, and a caller-supplied contradiction check vs
  high-confidence sensor facts; a quarantined write is dropped, and a `[0,1]` provenance `trust`
  is stamped on every episode. Adds **tamper-evidence** (R8.2): an append-only, per-cluster
  SHA-256 **hash chain** over memory-mutating events (admitted writes and quarantined poison
  attempts) in a new `memory_audit` table, computed with the same primitive as the flight
  recorder (ADR-005) and verifiable via `verify_memory_chain` — a silent edit, delete, or reorder
  of learned memory is detectable. Ships with an always-available **right-to-be-forgotten**
  (`forget_subject`) and the pgbouncer-safe **`SET LOCAL ki.cluster_id`** tenant-context helper
  for the Row-Level-Security scaffolding (RLS policies documented in `schema.sql`, enabled with
  the paired GUC discipline). (9) **`MEMORY_SUMMARY_TREE`** — a RAPTOR/GraphRAG-style **summary
  hierarchy** (spec R7): the consolidation worker builds one deterministic theme summary per
  `(cluster, playbook|namespace)` signature into a new `memory_summaries` table, so the agent can
  answer theme-level questions ("what keeps failing in payments?") without scanning every episode;
  matching theme summaries are injected into the triage prompt alongside episode recall (via
  `memory_loader`), so a query gets cross-incident context, not just the top-k episodes.
  **Regeneration is tied to KG change-rate** — a theme is rebuilt only when new episodes arrived or
  the cluster's KG edge count moved (a conditional `ON CONFLICT … WHERE` upsert), never on a fixed
  clock; abstractive LLM roll-up is deferred (deterministic aggregates today). All default off;
  memory-never-breaks-a-request discipline preserved (the guard fails *open*). All migrations
  additive/idempotent (PostgreSQL 16-compatible).
- **OpsMemBench — an ops-memory benchmark (Memory V5 P9).** A new benchmark harness
  (`evaluation/opsmembench/`) measuring five memory-dependent operator abilities no existing
  benchmark covers — **M1** cross-incident recall, **M2** temporal/time-travel reasoning, **M3**
  knowledge updating/contradiction, **M4** learned detection, **M5** abstention/no-false-memory —
  over versioned, deterministic Kubernetes incident timelines whose gold answers derive from the
  injection script (no LLM judge for the core metrics; leakage-free). Ships pure metric functions
  (recall@k, MRR, set-F1, abstention/false-recall, latency percentiles), a `ScoreCard` grader with
  an `ablation_table` (the paper's core "V5 minus one decision at a time" result), a sample timeline
  (`oomkill-recurrence`), and a CLI (`selftest` / `grade` / `ablation`). Runnable today offline via
  `make opsmembench`; the live cluster driver (drive the agent through fault injection → record
  predictions → `grade`) is the documented completion step. Fills the gap between chat-memory
  benchmarks (LongMemEval, LOCOMO) and memoryless ops-RCA benchmarks (OpenRCA, RCAEval, PetShop).
  A second, harder timeline (`fleet-multi-theme`, distractor-heavy — stresses M1 precision and M5
  abstention) and a one-command ablation demo (`make opsmembench-demo`: No-memory / V4-flat /
  V5-full) reproduce the paper's core table offline, showing memory ability rise monotonically as
  design decisions are added.
- **Multi-cloud deployment — runs on any provider.** First-class Helm overlays,
  `make` targets, and runbooks for **AWS EKS** (`values-aws.yaml.example`,
  `make aws-deploy-kubeintellect`, `docs/deploy/aws.md`, ALB ingress + RDS) and
  **GCP GKE** (`values-gcp.yaml.example`, `make gcp-deploy-kubeintellect`,
  `docs/deploy/gcp.md`, GCE ingress + Cloud SQL), alongside the existing Azure AKS and
  Alibaba ACK/ECS paths. A shared `_cloud-deploy` recipe keeps the LLM provider
  decoupled from the cloud (azure | openai | qwen | anthropic on any of them), and
  `docs/deploy/cloud.md` gains a full provider matrix (ingress class / managed DB /
  storage class per cloud).
- **Memory-ablation experiment harness** (`evaluation/memory_ablation.py`): quantifies
  the "smarter after every incident" claim by running each recurring incident across two
  fresh sessions with the memory hierarchy toggled on vs off, and reports the Encounter-2
  delta in investigation steps / latency / recall rate. Runnable via
  `python -m evaluation.memory_ablation run --arm {on,off}` + `compare`.
- **Full-showcase feature flags are now chart-configurable + a one-file overlay.**
  The chart ConfigMap now exposes `PREDICTIVE_DETECTION_ENABLED`,
  `NL_DETECTOR_AUTHORING_ENABLED`, and `POSTMORTEM_LLM_NARRATIVE` (alongside the
  existing Cortex/Watchtower/Autonomy toggles), and `values-showcase.yaml.example`
  flips every V4 layer on in one file — layer it on any cloud overlay for the full
  MemoryAgent demo.
- **Anthropic/Claude wired into the Helm chart** (`secrets.anthropicApiKey` +
  `ANTHROPIC_API_KEY`/`ANTHROPIC_LARGE_MODEL`/`ANTHROPIC_SMALL_MODEL`), so all four
  LLM providers (azure/openai/qwen/anthropic) are deployable, not just runnable locally.
- **Alibaba Cloud deployment path.** Two overlays: `values-alibaba.yaml.example`
  (managed ACK/ACR/ApsaraDB RDS/ESSD, production-grade) and
  `values-ecs-k3s.yaml.example` (the cost-friendly single-ECS + single-node k3s path:
  in-cluster Postgres on `local-path`, `ClusterIP`, no RDS/SLB, trimmed to a 2C4G box).
  `make alibaba-deploy-kubeintellect` + `scripts/alibaba_ecs_k3s_bootstrap.sh` (one-shot
  k3s+helm install) + `docs/deploy/alibaba.md` runbook with coupon/no-out-of-pocket guardrails.
- **Hackathon submission docs:** `docs/memoryagent-design.md`, `docs/demo-script.md`,
  `docs/architecture-diagram.md`, `docs/qwen-cloud-integration.md`, `SUBMISSION.md`.
- **Three new V4 operator capabilities** (all flag-gated, fail-open, zero-token at
  runtime; ADR-010/011/012):
  - **Anticipatory / predictive detection** (`PREDICTIVE_DETECTION_ENABLED`): trend
    predicates project a range-PromQL metric toward its threshold via a hand-rolled
    least-squares slope and fire a `predicted` finding *before* a slow-burn failure
    (e.g. OOM) manifests. Predicted findings are capped at autonomy `A1` — they
    investigate but never auto-fix.
  - **Grounded incident postmortems** (`GET /v1/episodes/{id}/postmortem`,
    `kq postmortem`): a read-only narrative over the hash-chained flight recorder
    where every line cites its event sequence number and the audit chain is verified;
    an optional LLM narrative is constrained to the recorded events.
  - **Natural-language detector authoring** (`NL_DETECTOR_AUTHORING_ENABLED`,
    `POST /v1/detectors`, `kq detector`): compile a plain-English failure into a
    detector that runs in **shadow** (observes only, never reaches the watchtower)
    until a human promotes it.
- **Unified root infrastructure.** Cluster + observability are now a single
  source at the repository root: a root `Makefile`, `deploy/` (kind, Helm
  Langfuse chart, Grafana, docker-compose monitoring), and `scripts/`
  (`kind/create-kind-cluster.sh`, `langfuse-provision.sh`), shared by all
  versions against one `testbed-v2` cluster and the `monitoring` namespace.
- **Langfuse auto-provisioning** (`make langfuse-provision`): generates a project
  key pair once and injects it into `.env` (and fans it into `v2/v3/v4/.env`),
  then `make langfuse-install` deploys with those keys — eliminating the previous
  manual key sync and the `sk-lf-change-me` placeholder.
- **Per-version cost attribution** via a `KI_VERSION` config field and
  `version:vN` Langfuse trace tags emitted from all instrumented versions.
- **Paper manuscript** (`paper/`): FGCS `elsarticle` source for "From Assistant
  to Autonomous Operator," with the related-work survey, architecture/pillar
  sections, and the multi-version evaluation tables/figures.

### Changed
- **Relicensed v4 from MIT to AGPL-3.0-or-later (dual-licensed).** The open-source
  license is now GNU AGPL-3.0-or-later; a separate commercial license is available
  (`LICENSING.md`). Updated `v4/LICENSE`, `kube-q/LICENSE`, the three `pyproject.toml`
  declarations + OSI classifiers, README badge, and added `CITATION.cff` +
  `THIRD_PARTY_NOTICES.md`.
- **Per-version (`v2/`, `v3/`, `v4/`) Makefiles are now app-only** — shared infra
  targets and duplicated `deploy/`/`scripts/` directories moved to the root.
- **Stronger, independent evaluation judge.** The LLM-as-judge is decoupled from
  the system-under-test's coordinator and points at a separate Azure deployment
  (`EVAL_JUDGE_AZURE_*`), with reasoning-model request support.

### Fixed
- **Langfuse Redis auth**: the Langfuse Redis now runs with `--requirepass` and a
  matching client secret, clearing the recurring `langfuse-worker` `ERR AUTH`
  warnings.
- **Observability ingress**: routed `langfuse.local` / `prometheus.local` /
  `loki.local` correctly (ingress controller pinned to the node that maps host
  port 80), restoring host→cluster reachability for trace/metric/log collection.

### Notes
- The evaluation harness, run artifacts, and `.env` files remain local-only
  (git-ignored); they are not part of the published tree.
