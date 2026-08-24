# Changelog

All notable changes to kube-q will be documented here.

## [Unreleased]

### Fixed — `kq replay` printed `✓ chain intact` for a chain the server could not check
- the server's `replay_meta` frame now carries `chain_verified` beside `chain_valid`. When it is
  `false` the server could not read the episode's chain anchor, so `chain_valid` means only
  *"nothing contradicted these records"* — and nothing could have. `kq replay` prints
  `✗ chain NOT VERIFIED` and exits **4**, the code it already used when the meta frame itself was
  missing; the two are the same fact, that the records above are unverified.
- an absent `chain_verified` reads as `true`, so a server older than 2026-08-24 behaves exactly
  as it did.

### Fixed — documented exit codes that the commands did not return
- `kq replay` documented `0/1/3/4/5` and returned `0/1/2/3/4/5`. The undocumented `2` is the
  usage error, and it was missing from both surfaces that state the contract — the reference
  table and the `Exit codes:` block in the module's own usage text, which is printed *when you
  make a usage error*. A script written against the table cannot branch on a code the table
  does not have; it takes the wrong arm and reports success.
- `kq completion`, `kq config`, `kq digest`, `kq findings`, `kq preference` and `kq v5-status`
  had no exit-code table at all, though each returns `1` (the request failed) or `2` (usage) as
  well as `0`. Their tables now also record where `0` is deliberately *not* a health verdict:
  `kq findings` and `kq digest` exit `0` on a degraded sensorium or an incomplete digest, and
  say so in the row, because the command cannot tell you the cluster is fine.
- these are now checked. `make docs-check` derives each command's real return set from the AST
  — following the helpers that `kq postmortem` and `kq config` return through — and fails on a
  code that is returned but undocumented, documented but unreachable, or on a table that is
  missing entirely.

### Fixed — `kq detector promote` reported a dead detector as a failed request
- the server answers **409** for exactly one thing: promoting would make `/v1/detectors` report
  `status: active` about a detector whose predicate can never match an observation — the failure
  mode this project shipped once, where a stray space in an alternation made a detector a
  permanent no-op through a green suite. The CLI mapped it to exit `1`, "the request failed", so
  it was indistinguishable from the store being down and a script's retry loop would retry a dead
  detector forever. It now exits `3`, the code this command already owns for "understood, refused
  on the merits, nothing changed", and prints the server's reason plus "retrying will not help".
- `404` no longer prints "Detector command failed" — it names the detector that is missing.
- `transport.server_detail()` is now the one reader of a server's `detail`. There were two, and
  only `explain()` knew FastAPI sends validation errors as a *list* of dicts, so `replay_cmd`
  rendered a 422 as `[{'type': 'missing', …}]`.

### Fixed — `kq detector shadow` printed a bare count that was not always a measurement
- the line `<name>: N shadow firing(s)` is what a reviewer promotes or rejects a candidate on,
  and it read identically whether the detector had run quietly, had never been loaded by the
  process answering, or the fixed-size firing ring had overflowed and dropped the older half.
  The server now reports `watching` and `buffer.saturated`; the CLI annotates the count with
  "not a measurement of precision — …" when either applies, and still prints the number.
- a `503` from the endpoint no longer renders as a firing count at all.

### Fixed — a `break` in the caller erased the record of a lossy stream
- `stream()` counted every unparseable SSE frame into an `SseStats` and called
  `_warn_if_lossy(stats)` on the line *after* the loop. A `break` closes a generator at the
  `yield`, so that line ran on a full drain and on nothing else — and `case FinalEvent(): break`
  is exactly the pattern `docs/sdk.md` and the class docstring tell people to write. Measured
  with one bad frame ahead of the final event: draining logged the loss, breaking logged nothing,
  on both the sync and the async client. Now in a `finally`.
- `SseStats` says "a caller that must be fail-closed inspects this and refuses", which was true
  of the CLI and never of the SDK — `stats` was a local nobody could reach. It is now
  `client.last_stream_stats`, assigned before the first yield so an early `break` can still read
  it, `None` until a stream has run so "not yet" is not reported as "lost nothing". Documented
  with a fail-closed example in `docs/sdk.md`.

### Fixed — the two health checks named different causes for the same failure
- `AsyncKubeQClient.health` hand-rolled the classification that `check_health` already owned, and
  had drifted from it. A hostname that does not resolve was reported as **"Connection refused —
  nothing is listening at …"**: nothing was ever contacted, so no port can be blamed, and the
  reader was sent to check the service instead of the hostname. The sync half had said
  "DNS resolution failed for '…' — check the hostname or /etc/hosts" all along.
- its timeout message also dropped the duration in force, the exact wording the sync side had
  been corrected to include; and although both docstrings promise a *fast* connectivity check,
  the async one ran on `self.timeout` — the **query** timeout, 120 s by default against 5 s.
- the classification now lives once, in `transport.health_status_reason` /
  `transport.health_failure_reason`, with `HEALTH_TIMEOUT` and `HEALTH_PATH` shared by both
  clients. Tests assert the two surfaces are byte-identical across seven outcomes.

### Fixed — `AsyncKubeQClient.stream` reported an unreachable server as an empty answer
- after exhausting its retries the async generator fell off the end of the loop, and a `return`
  from an async generator is a **clean end of stream**: the caller's `async for` completed with
  zero events and no exception. The sync `KubeQClient.stream` raised the last `TransportError`,
  and `docs/sdk.md` promised both clients "give up and raise". Measured against a refused
  connection: sync raised `ConnectError`, async yielded 0 events silently. The async half now
  carries `last_exc` and re-raises, like its sibling.
- `docs/sdk.md` "Retry behaviour" also named a schedule the code has never used
  (`[1s, 3s, 5s]` vs the real `(2, 5, 10)`) and claimed 4xx/5xx are "raised immediately" for both
  entry points — true of `stream()`, but `query()` logs a warning and returns `{"text": "", …}`.
  Rewritten as a table of what each entry point actually does, and pinned by tests.

### Fixed — `kq replay` reported total truncation as a missing episode
- `GET /v1/episodes/{id}/replay` answers **409** when the chain anchor proves the episode had
  records and none survive, separating it from a 404 on purpose — its own comment calls
  laundering that into an absence "the one wrong answer this endpoint must not give". The CLI
  mapped it to exit **1**, the same code as a mistyped episode id or a DNS failure, so
  `kq replay X; [ $? -eq 3 ]` could not fire on the one case where every record is gone. It now
  exits **3** and names it as a broken chain.
- second half of the same defect: the request is **streamed**, so the response body was never
  read and `explain()`'s `response.json()` raised — silently falling back to httpx's
  `Client error '409 Conflict' for url …` line and discarding the server's explanation on every
  error status. The body is now read before `raise_for_status()`, as the 503 branch already did.

### Fixed — `kq findings` printed a green all-clear over observations it had been told were dropped
- the sensorium's watch queue sheds the **oldest** observation when it overflows (deliberately —
  blocking would stall the watch and trigger a reconnect storm plus a full relist), so the loss is
  silent where it happens and `queue.shed_total` in `GET /v1/findings` is the only record of it.
  Nothing read that field: not this command, not the docs. Measured 2026-08-24 with
  `shed_total: 4271`, a healthy stream and no findings — the output was
  `No findings · 16 detectors watching`, in green.
- the command already refused the all-clear for the two *named* blindnesses (stream not connected,
  Prometheus unreachable); shedding was the sibling in between. It now prints **Perception is
  lossy** with the dropped count and the queue high-water, above the findings table as well as
  instead of it — shedding makes an empty list not an all-clear and a non-empty one not a complete
  list. A server that sends no `queue` is not accused of shedding.

### Fixed — `kq postmortem` printed the tamper warning and exited 0
- `kq replay`, `kq export` and `kq postmortem` all render the same flight-recorder
  audit-chain verdict, and the first two map it to exit **3** (chain broken), **4** (nothing
  read, so not verified) and **5** (unaltered but incomplete). `kq postmortem` returned **0**
  in all of them, so `kq postmortem X > report.md && attach-to-ticket` could not tell a
  tamper-evident report from a broken one. It now follows the documented convention.
- the root cause was on the wire, not in the CLI: `GET /v1/episodes/{id}/postmortem?format=markdown`
  returned `{"markdown": ...}` and nothing else, so the verdict arrived as one of four English
  banners and the command had no datum to branch on. The response now carries `chain_valid`,
  `chain_verified`, `events_lost`, `gaps` and `enrichment_failed` alongside the prose —
  additive, so a caller reading only `markdown` is unaffected, and `format=json` is untouched.
- a server too old to send those fields is reported as exit **4**, not 0: an absent verdict is
  not a passing one.

### Fixed — `kq config show` reported success over a config it had just called invalid
- the command printed `⚠ Invalid values detected:` and returned **0**, so the human-readable
  answer and the machine-readable one disagreed — and `kq config show || exit 1` in an install
  script or CI pre-flight only reads the second. It now exits **2**, the code the CLI already
  uses when `load_config(strict=True)` finds the same errors; a valid config still exits 0.
- both `config show` and `config set` rendered `err.splitlines()[0]`. `validate_config` builds
  each message in three parts on purpose — the offending value, an example of a valid one, and
  the env var to edit — and the last two sit on lines 2 and 3. The user saw
  `Invalid URL: 'not-a-url' — must start with http://` and never
  `Fix: set KUBE_Q_URL in ~/.kube-q/.env`. Both now print the whole message, matching what
  `load_config(strict=True)` has always done.

### Fixed — `kq v5-status` could not show that an "active" flag was doing nothing
- the server now reports `degraded_experimental_flags` (a flag the code *does* read, sitting in a
  subsystem that is not running) and the `memory` state behind it. Both were dropped on the floor
  by the CLI, which would have printed `active_flags: MEMORY_HIERARCHY_ENABLED` and no sign that
  the hierarchy was unreachable. The command now renders a red `degraded_experimental_flags` row
  carrying the reason, and a `memory` row that always shows the hierarchy's state and the count of
  sensorium observations dropped — an empty degraded list is only evidence when the hierarchy is
  visibly up.
- the flags stay listed under `active_flags` on purpose: that list is rollout identity, not
  liveness. A server predating either field renders exactly as before.
- `docs/cli-reference.md` documents the third red row under `kq v5-status`.

### Fixed — `kq findings` printed one word for four unrelated sensorium absences
- the server classifies a missing detector engine four ways and sends the sentence in
  `sensorium_reason` (switched off, no compiled detectors, a **failed start**, a
  **leader-election standby**). The command ignored the field and printed
  *"Sensorium is disabled on this server."* for all four. Driving each of the four payloads
  through `findings_cmd.run` produced one distinct line, not four — and for two of them that
  line is a false statement: a sensorium that **crashed** was never disabled, and a standby
  replica is behaving correctly while the lock-holding replica does the perceiving, so
  "disabled on this server" sends the operator to look at the wrong replica's silence.
- it now renders the server's sentence verbatim. Against a server too old to send the field it
  says it does not know — *"this server does not report why, so an empty result here does NOT
  mean the cluster is healthy"* — rather than naming the likeliest cause. Exit stays `0` in all
  four cases, and every other `sensorium` state (`starting`, `reconnecting`, `stopped`,
  `active`) is untouched.
- `docs/cli-reference.md` gains the four-situation table under `kq findings`.

### Fixed — `replay` and `export` restated a recorder outage as a missing episode
- the server now answers `503` when the decision log cannot be read at all and keeps `404` for an
  episode that genuinely has no rows. `kq replay` recognised only the `404` wording, so any
  non-200 it handled printed *"No recorded episode 'X'."* — an absence claim about an episode
  nobody was able to look up. It now prints the server's own reason and says explicitly that this
  is **not** a statement that the episode has no records.
- `kq export` reported *"No recorded events … nothing exported"* (exit `4`) whenever the
  postmortem's timeline was empty, which is exactly what an unreadable recorder produces. It now
  reads `recorder_available` and exits `1` with the reason instead; a server that predates the
  field behaves as before.

### Fixed — an unreadable history store was rendered as an empty one
- `store.py` swallows every `sqlite3.Error` by design (a broken local cache must not crash the
  REPL), but each read then returns `[]` — which is also what a fresh install returns. Measured
  against a `~/.kube-q/history.db` holding one line of text, `kq --list` printed *"No sessions
  found."* and exited 0, with nothing on stderr: the `_logger.warning` meant to say otherwise goes
  to a logger with no stderr handler outside `--debug`.
- the store now records why the **last** operation failed — `_db()` clears it on every open, so a
  stale error can never be printed beside a healthy empty result — and `renderer.print_store_failure()`
  is consulted before any empty line is rendered: `kq --list`, `kq --search`, and the REPL's
  `/list`, `/search`, `/branches` and resume picker. A genuinely empty store still prints the
  plain line.
- `docs/session-history.md` gains *When the store cannot be read* (message, causes, fix), and its
  schema-version row is corrected from v3 to the v4 the code has been writing since the
  `kube_context` migration.

### Fixed — the kubeconfig fallback could not read the file kubectl writes
- `list_contexts()` tries `kubectl config get-contexts` and falls back to a minimal scan of
  `~/.kube/config`. The fallback matched only `- name:`, the ordering where the YAML sequence dash
  lands on the name — but kubectl writes `- context:` with `name:` on the following line.
  Measured against a kubectl-written file holding two contexts, `kubectl` printed both and the
  fallback returned nothing. That branch runs precisely when kubectl is *not installed*, i.e.
  when it is the only source there is, so a `kq`-only client machine got no `/context`
  completions and was told "No kubectl contexts found (is kubectl installed and is
  `~/.kube/config` valid?)" — a question whose two hypotheses the user checks and finds innocent.
  The scan now keys off the position of an entry's keys rather than which key carries the dash,
  handles both orderings and entries indented under `contexts:`, still stops at the next
  top-level key (so a `users:` entry is never offered as a context), and ignores the `name:`
  inside a nested `extensions:` entry (`context_info`).

### Fixed — `/plugins` told you to install plugins you had already installed
- The command whose entire job is to answer *"what plugins do I have?"* read only the registry.
  With a plugins directory holding nothing but files that had all failed to import it printed
  *"No plugins loaded. Drop Python files into `~/.kube-q/plugins/`…"* — advice to do the thing the
  user had just done, with the reason for the failure only in `~/.kube-q/kube-q.log`. It now
  reports failures whether or not anything loaded, and the rendering moved out of the REPL loop
  into `_render_plugins()` so it can be tested.

### Added — the plugin system is documented
- `~/.kube-q/plugins/` is a real extension point with a decorator API, a context object and a
  `KUBE_Q_PLUGIN_DIR` override, and until now `grep -rn plugin v4/docs/*.md` returned nothing —
  the module docstring was its only specification. `docs/cli-reference.md` now covers the
  directory, load order, the `register()` contract, everything on `PluginContext`, the "this is
  code you are choosing to run" warning, and what happens when a plugin fails.

### Fixed — a plugin that failed to import was indistinguishable from one never installed
- `load_plugins` documented its errors as "logged (and printed as a dim warning)", but the
  `kube_q` logger only gets a stderr handler under `--debug`, so the warning went to
  `~/.kube-q/kube-q.log` and nowhere else. Measured with `setup_logging()` configured exactly as
  `kq` does at start-up, a plugins directory containing a broken file produced **empty stdout and
  empty stderr**: the user drops a file into `~/.kube-q/plugins/`, the banner does not mention it,
  their command comes back "Unknown command", and nothing on screen says why. Failures are now
  collected in `plugins.load_failures()` and the REPL prints each one with its exception type and
  message.
- A module that raised *after* calling `register` left its commands in the registry. `register`
  runs at import time, so `/plugins` listed, tab-completion offered and `/`-dispatch would run a
  command from a module whose remaining lines — including whatever state the handler depends on —
  had never executed, while the start-up banner's "Plugins loaded:" line said it had not loaded. A failed module's
  registrations are now rolled back (including a handler it overwrote), and it is removed from
  `sys.modules` rather than left half-initialised and importable. A file Python cannot even build
  an import spec for is reported instead of skipped in silence.

### Fixed — a profile and a project-local `.env` were silent no-ops
- `load_config` documents four layers (`~/.kube-q/.env` → active profile → `./.env` → shell) but
  `_load_dotenv_file` wrote each file into `os.environ` and skipped keys already there, so the
  **first** file read won and the precedence was inverted. Every key `~/.kube-q/.env` already set
  — `url`, `api_key`, `context`, i.e. everything `kq config set` writes — could not be overridden
  by a profile or a directory-local `.env`, so `kq config profile prod` changed nothing for a
  configured user. Layering is now explicit: `_read_dotenv_file` parses, `load_config` merges
  lowest-first, and a shell export still outranks every file. File values still land in
  `os.environ` (`repl` reads it directly), but the loader now records what it put there, so a
  second `load_config()` in the same process no longer mistakes its own leftovers for shell
  exports — and a key deleted from every file stops being returned.
- `kq config show`'s **Source** column derived the source by comparing `os.environ` against
  `~/.kube-q/.env`. That comparison cannot see a profile or a local `.env`, so it reported both as
  `shell env`, while `docs/cli-reference.md` promises the column names them. It now reads the
  layer that actually won, and prints `profile (prod)` / `file (./.env)` / `file (…)` / `shell env`.

### Fixed — a lost SSE frame no longer looks like a frame the server never sent
- Both SSE parsers — `core/transport.iter_sse` and its hand-written async twin
  `core/client._aiter_sse` — answered an undecodable frame with `pass`. Nothing raised, nothing
  logged, nothing counted, so a truncated answer and a complete one were observationally
  identical, and a stream cut *mid-frame* left no trace at all: with no blank-line terminator
  the loop never saw a frame to skip. That matters most where a frame carries a verdict rather
  than prose — `kq replay`'s `replay_meta` holds `chain_valid`, so a dropped one turned *"the
  audit chain is broken"* into *"the audit chain was never mentioned"*. Both parsers now take an
  optional `SseStats` recording `dropped_frames`, the first bad payload, and `truncated_tail`,
  **without changing a single frame they yield**; the interactive chat stream passes one and
  prints *"⚠ This answer may be incomplete — 2 unreadable frames"* rather than aborting the turn,
  because a partial answer is worth more to an operator than a raised exception.

### Fixed — the server explained why, and `kq` printed a link to MDN
- The API answers errors with a reason, not just a status: `GET /v1/detectors` returns **503**
  with *"no memory pool — the detector store is not configured"* rather than an empty 200, so
  *unqueryable* cannot be mistaken for *unmonitored*, and promoting a detector that can never
  fire returns **409** naming the reason. All eight commands called `raise_for_status()` and
  printed the resulting exception, whose message is the status line plus a link to
  `developer.mozilla.org` — none of the server's `detail` ever reached the terminal. A shared
  `transport.explain()` now renders it (FastAPI validation lists included) with the status code
  alongside, and falls back to the exception whenever there is no usable body, so a missing
  explanation can never become a missing error.

### Fixed — the SDK exported an approval-gate event it could never emit
- `kube_q.core.client` publishes `HitlRequestEvent` in its public API, which promises that a
  caller can detect the approval gate without reading prose. The server merges the gate fields
  onto the choice of an ordinary chunk rather than sending a `ki_event`, and the SDK's parser
  inspected only `ki_event` and `delta.content` — so the event was unreachable and a caller
  streaming through the SDK received a `TokenEvent` full of markdown and no machine-readable
  signal that the agent was waiting for a human. The parser now returns every event a chunk
  carries (the prose first, then the gate), both the sync and async stream loops yield all of
  them, and `HitlRequestEvent.data.approval_id` carries the id needed to resume. The module's
  own usage example shows the case, since an event nobody knows to match is still unreachable
  in practice.

### Fixed — the approval gate never reached the CLI on its default path
- The server marks an approval gate with `hitl_required` (and the `action_id` needed to resume
  it) on the gate chunk, whose `finish_reason` is `null`, and sends `finish_reason: "stop"` on a
  separate terminal chunk that carries no gate fields. The streaming reader consulted
  `hitl_required` only *inside* `if finish == "stop"`, so the two conditions were never true on
  the same frame: `kq` streamed the "Approval Required" text to the screen while reporting
  `hitl_pending = False` and `action_id = None`. The only other route to a pending gate was a
  fallback watching for a 🛑 that nothing in the repo emits — and which blames
  the server ("should be upgraded to send hitl_required") for a client-side condition. The
  streaming path now trusts the field wherever it appears, as the non-streaming path already
  did. The regression test feeds the *real* server frames to the *real* reader, because every
  previous test of this side channel hand-built the chunk it then asserted on.

### Added — exit code `5`: the chain is intact but the record has holes in it
- `kq replay` and `kq export` exited `0` with a green *"✓ chain intact"* over an episode the
  server had recorded as incomplete. A hash chain proves no stored record was altered; it cannot
  prove that a record which was never stored is missing — and the flight recorder is
  fire-and-forget, so an outage drops events. The server now writes each loss into the chain as a
  `recorder_gap` record carrying the count and the cause. Both commands surface it: exit **5**,
  the total lost, the number of gaps, and each reason. `0` now means intact *and* complete, `3`
  (tampering) still wins over `5`, and an older server that does not send the field is treated as
  complete rather than warned about without evidence.

### Fixed — `kq replay` showed a blank summary for whole classes of record
- The `summary` column was built from a list of seven top-level field names. A `finding`
  payload shares none of them, and a `findings:<cluster>` episode contains nothing but
  findings — so `kq replay findings:default` printed a table of blank summaries for detector
  firings that had certainly fired. `plan` rows (all content in `steps`) and `ki_otel_span`
  rows (all content in `attributes`) were blank for the same reason, and spans also showed a
  `?` type because the replay stream yielded the payload without the row's recorded kind. The
  server had a complete, kind-aware summariser all along, in the postmortem builder; there was
  no reason for a second one here. Both now call `ki_protocol.record.summarise_record`, so a
  newly recorded kind is described once or not at all — and a test that reads the recorded
  kinds *and* both readers fails when one is unhandled.

### Fixed — the SDK's typed events arrived with their payloads stripped
- `KubeQClient` / `AsyncKubeQClient` parse each frame into a typed event through
  `ki_protocol.events.parse_event`. That module and `ki_protocol.wire`, which the server emits
  from, are two halves of one contract that nothing joined: no test in either suite imported
  both, so each half was only checked against its own fixtures. Measured by serialising every
  emission model and parsing it, five of the eight arrived stripped — a `tool_result` lost its
  `output`, a `tool_call` its `command`, an `error` its message, and a `hitl_request` all four of
  its fields, because the two halves spell the same things differently. `plan` frames, emitted
  since V2 and on every Cortex turn, were discarded outright: the client union never carried the
  type. `parse_event` now maps the wire's names onto the client's (never overwriting a field the
  payload already set), and `PlanEvent` / `PlanData` are part of the union and exported from
  `kube_q.core.events`. **The REPL was never affected** — it reads the raw frames — so this
  changes what the SDK sees, not what `kq` prints.

### Fixed — `kq -q` now reports failure through its exit status
- **A one-shot query that failed used to exit 0.** Every failure path — 401
  authentication, any non-200, invalid JSON from the server, retries exhausted —
  printed a red message and then exited **successfully**, so a script, CI job or
  `watch` loop could not tell an answer from an outage. `kq -q` now exits **1**
  when the server did not answer, and still exits 0 for a real answer or for a
  mutation correctly paused for human approval (which returns no assistant text).
- **Test isolation: `KUBE_Q_*` variables no longer leak between tests.**
  `config._load_dotenv_file` copies `.env` entries straight into `os.environ`, which
  `monkeypatch` cannot undo, so `test_config.py` left `KUBE_Q_URL`, `KUBE_Q_MODEL`
  and an invalid `KUBE_Q_TIMEOUT=-10` set for the rest of the session. Nothing
  downstream read them, so the suite stayed green while the environment was poisoned
  mid-run. An autouse fixture in `tests/conftest.py` now restores them after every test.

### Added — Shell completion
- **`kq completion [bash|zsh|fish]`** prints a shell completion script. Enable with
  `source <(kq completion bash)` (or the zsh/fish equivalent). It completes
  subcommands, their second-level verbs (`config show`, `detector new`, …), notable
  per-command flags (`findings --limit`, `digest --hours`), and the global flags —
  all generated from the same registry that powers `kq --help`, so completion never
  drifts from the real command set. A drift test asserts every completed global flag
  still appears in `kq --help`.

### Added — Command discoverability
- **`kq --help` now lists the subcommands.** The eight `kq <command>` verbs
  (`config`, `findings`, `digest`, `replay`, `postmortem`, `detector`, `preference`,
  `v5-status`) were dispatched manually before argparse and so never appeared in
  `--help` — a new user couldn't discover them. Help now shows an aligned
  `Commands:` section, generated from a single registry.
- **`kq help` / `kq commands`** — print the command listing (with one-line
  descriptions and a pointer to the REPL / `-q` one-shot mode).
- **Friendly unknown-command handling** — `kq fndings` now prints
  `Unknown command: fndings` with a `Did you mean: findings?` suggestion (difflib)
  and a pointer to `kq help`, instead of dumping the raw argparse usage wall. Exits `2`.

### Changed — Architecture
- New `cli/subcommands.py` is the **single source of truth** for `kq` subcommands
  (name → lazy-imported `run()` + description). `main()` dispatches, builds the
  `--help` command list, and powers `kq help` from it — adding a subcommand is now
  one registry line, no `main()` edits. Replaces eight repetitive `if sys.argv[1] == …`
  dispatch blocks.

### Changed — Interface polish (inline model preserved)
- **Real-time investigation plan** — the plan panel now lives inside the streaming
  `Live` group and updates its step icons (`✓ ▸ · —`) as the server emits successive
  `plan` events, instead of being buffered and printed only once at the end
- **Per-turn status footer** — after each answer a compact `ctx · ns · N tok · $cost`
  line summarises the active context, namespace, and session usage
- **Approval panel** — pending HITL write-actions render as a distinct bordered panel
  (with the proposed command when the server provides it) rather than inline text
- **Sectioned help** — `/help` shows a compact overview and topic list; `/help <topic>`
  (e.g. `/help sessions`, `/help config`) drills into one area; aliases like
  `/help ns` → namespace are accepted
- **`NO_COLOR` support** — colours now flow through a single semantic theme that
  degrades to no styling when `NO_COLOR` is set (https://no-color.org/)

### Changed — Architecture
- New `cli/theme.py` centralises the colour palette as a Rich `Theme` (semantic roles
  instead of scattered `cyan`/`dim` literals)
- New `cli/help_text.py` holds help content as structured topic data (was a single
  ~180-line f-string in `renderer.py`)
- `repl.py` reduced from 1191 → ~830 lines by extracting `cli/prompt.py` (completer,
  prompt-session factory, slash-command catalogue) and `cli/sessions_ui.py` (session
  picker, resume, transcript-history rendering); the public surface
  (`run_repl`, `ReplConfig`, `_make_prompt_session`, `_HISTORY_FILE`, `_print_logo`)
  stays importable from `kube_q.cli.repl`

## [1.4.0] — 2026-04-14

### Added — Web UI (Docker)
- **Browser terminal** — full kube-q REPL accessible in any browser via xterm.js + WebSocket → node-pty → `kq`; no client-side logic duplicated, all commands and streaming stay in the Python process
- **`web/server.mjs`** — production single-port server: Next.js HTTP + PTY WebSocket (`/pty-ws`) on one port; spawns a `kq` process per connection
- **`web/pty-server.mjs`** — dev standalone PTY WebSocket server on port 3001; run alongside `next dev` via `npm run dev`
- **`Dockerfile`** — multi-stage build: Node builder compiles Next.js, runtime stage installs `kube-q` from PyPI + copies built app; env vars injected at runtime (`KUBE_Q_URL`, `KUBE_Q_API_KEY`)
- **iframe / basePath support** — `NEXT_PUBLIC_BASE_PATH` env var relocates the Next.js app to a sub-path; `Content-Security-Policy: frame-ancestors *` and `X-Frame-Options: ALLOWALL` headers allow embedding in any parent page
- **Download conversation button** — toolbar "⬇ Download" button exports the xterm scrollback buffer as a `.md` file directly to the browser
- **Custom branding** — `KUBE_Q_LOGO` sets a custom ASCII banner logo; `KUBE_Q_TAGLINE` sets a custom copyright / tagline line; both configurable via `.env` or environment variable

### Added — Session Search & Branching
- **`kq --search <query>` / `/search <query>`**: FTS5 full-text search across all session history with highlighted match snippets; supports FTS5 boolean syntax (`pods AND NOT staging`); old databases are backfilled during schema migration
- **`/branch`**: fork the current conversation at the current message count into a new independent session — original is preserved; `/branches` lists all forks; `/title <text>` renames a session
- **SQLite schema v3**: `messages_fts` FTS5 virtual table with insert/delete triggers, `parent_session_id` and `branch_point` columns on `sessions`; branches are ordinary sessions so search finds them automatically

### Added — Token & Cost Tracking
- **Token footer**: every response now shows `(1.2s · 460 tokens)` when the server emits a `usage` block in the SSE stream or JSON response; servers that omit `usage` behave exactly as before — no errors, no noise
- **`/tokens` / `/cost`**: new in-REPL commands print a Rich panel with per-session prompt/completion/total counts, request count, and estimated dollar cost; override rates with `KUBE_Q_COST_PER_1K_PROMPT` and `KUBE_Q_COST_PER_1K_COMPLETION` env vars for custom backends
- **`kq --list` tokens column**: session listing now shows total token count per session; SQLite schema auto-migrates transparently from v1 databases (adds `token_log` table and `total_*_tokens` columns via `PRAGMA user_version`)

### Added
- Configurable display names — `--user-name` / `--agent-name` CLI flags, `KUBE_Q_USER_NAME` / `KUBE_Q_AGENT_NAME` env vars, and `user_name` / `agent_name` config file keys; used in the prompt and saved conversation files
- Friendly HTTP 401 error handling — shows a clear actionable message instead of raw JSON when the server has auth enabled and the key is missing or invalid; affects streaming, non-streaming, and health-check paths
- `.env` file support — all settings configurable via `KUBE_Q_*` environment variables; kube-q loads `~/.kube-q/.env` and `./.env` automatically (no extra tooling required)
- Removed YAML config file — `.env` files cover all the same settings with one less format and one less dependency (`pyyaml` removed)
- Renamed config directory to `~/.kube-q/`; history, user-id, and log files all consolidated there

## [1.0.0] — 2026-04-10

### Added
- Interactive REPL with streaming SSE responses
- Human-in-the-Loop (HITL) approve/deny flow
- Persistent conversation history and user ID
- Namespace context switching (`/ns`)
- Conversation save to markdown (`/save`)
- Built-in demo scenarios (deploy, debug, hitl, security, scale)
- Single-query mode (`--query`)
- Health check with retry on startup
- Multi-line copy-paste support via `prompt_toolkit`
- Rich syntax highlighting for YAML/JSON code blocks
- File attachment support via `@filename` in messages
- `--api-key` / `KUBE_Q_API_KEY` authentication
- `--ca-cert` for corporate proxies / self-signed certificates
- `--output plain` flag for pipe-friendly output
- Live token streaming
- Typo suggestions for unknown slash commands
