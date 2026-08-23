# Changelog

All notable changes to kube-q will be documented here.

## [Unreleased]

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
