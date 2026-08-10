# kube-q

**Chat with your Kubernetes cluster from the terminal.**

`kube-q` is an interactive CLI (`kq`) that connects to an AI-powered backend and lets you query, debug, and manage your cluster in plain English — with streaming responses, persistent session history, full-text search, conversation branching, token cost tracking, human-in-the-loop approval flows, and rich terminal rendering.

[![Docs](https://img.shields.io/badge/docs-mskazemi.github.io%2Fkube__q-teal)](https://mskazemi.github.io/kube_q/)
[![PyPI](https://img.shields.io/pypi/v/kube-q)](https://pypi.org/project/kube-q/)

---

## Features

- **Interactive REPL** — persistent conversation history, slash commands, Tab completion
- **Streaming responses** — tokens render in real-time via Server-Sent Events
- **Session persistence & resume** — every conversation is saved to a local SQLite database; resume any past session with `kq --session-id <id>` from the shell or pick one interactively in the REPL with `/sessions` (arrow-key picker, Enter to resume, Esc to cancel) — the stored transcript is replayed on resume so you see the whole conversation before continuing, and the session's kube context is restored so you're back on the same cluster
- **Full-text search** — `kq --search "pod crash"` or `/search` inside the REPL; FTS5-powered with highlighted match snippets and boolean syntax
- **Conversation branching** — `/branch` forks the current conversation at any point; the original is untouched; `/branches` lists all forks
- **Token & cost tracking** — every response shows tokens used; `/tokens` shows session totals and estimated dollar cost; rates configurable per model
- **Human-in-the-Loop (HITL)** — review and approve or deny destructive actions before they run
- **Namespace context** — set an active namespace with `/ns <name>`; it's injected into every message automatically
- **File attachments** — embed YAML, JSON, logs, and more with `@filename` anywhere in a message
- **Conversation save** — dump the full session to a Markdown file with `/save`
- **Single-query mode** — pipe-friendly with `kq --query "…"` and `--output plain`
- **TLS & auth** — `--api-key` / `KUBE_Q_API_KEY` env var, custom CA cert via `--ca-cert`
- **Rich output** — syntax-highlighted code blocks, elapsed response time, typo suggestions for slash commands
- **Python SDK** — use `KubeQClient` directly in your own scripts and tools
- **Multi-backend** — one CLI drives the kube-q server, direct OpenAI, or Azure OpenAI — selectable per launch with `--backend` or `KUBE_Q_BACKEND`
- **Multi-cluster** — switch kubectl context live with `/context <name>` (tab-completes from your kubeconfig); bundle cluster + backend + keys into named profiles under `~/.kube-q/profiles/` and launch with `--profile <name>`
- **Plugins** — drop a Python file in `~/.kube-q/plugins/` to register your own slash commands; loaded at REPL startup
- **Web UI with live reconnect** — browser terminal shows connection status, auto-reconnects with exponential backoff, and optional `PTY_AUTH_TOKEN` gate

---

## Installation

```bash
pip install kube-q
```

Or via Homebrew:

```bash
brew tap MSKazemi/kube-q
brew install kube-q
```

Or install from source:

```bash
git clone https://github.com/MSKazemi/kubeintellect.git
cd kubeintellect/v4/packages/kube-q
pip install -e .
```

Requires Python 3.12+.

---

## Quick start

```bash
# Start the interactive REPL (connects to https://api.kubeintellect.com by default)
kq

# Save your URL and API key once — takes effect immediately, persists across sessions
kq
/config set url=https://kube-q.example.com
/config set api_key=your-key-here

# Or pass them as flags for a one-off launch
kq --url https://kube-q.example.com --api-key your-key-here

# Single query and exit
kq --query "show me all pods in the default namespace"

# Pipe-friendly plain text output
kq --query "list failing deployments" --output plain

# List recent sessions
kq --list

# Search across all past conversations
kq --search "pod crash"

# Resume a previous session (shell) — replays the stored transcript
kq --session-id <id>

# Or pick one interactively inside the REPL (↑/↓, Enter to resume, Esc to cancel)
/sessions
```

---

## In-REPL commands

### Conversation

| Command | Description |
| --- | --- |
| `/new` | Start a new conversation (clears history, generates new ID) |
| `/id` | Show the current conversation ID |
| `/state` | Show full session state — ID, user, messages, tokens, namespace, HITL flag |
| `/save [file]` | Save conversation to a Markdown file |
| `/clear` | Clear the terminal screen |
| `/help` | Show full in-REPL help |
| `/quit` / `/exit` / `/q` | Exit kube-q |

### Namespace

| Command | Description |
| --- | --- |
| `/ns <name>` | Set active namespace — prepended to every query automatically |
| `/ns` | Clear the active namespace |

### Kubernetes context

| Command | Description |
| --- | --- |
| `/context <name>` | Set active kubectl context — prepended to every query (Tab-completes from your kubeconfig) |
| `/context` | Clear the active context |

### Profiles & plugins

| Command | Description |
| --- | --- |
| `/profile` | List profiles in `~/.kube-q/profiles/` and show which one is active |
| `/profile <name>` | Show the restart command for the named profile (profile switching requires a restart) |
| `/plugins` | List slash commands registered by plugins in `~/.kube-q/plugins/` |

### Session history

| Command | Description |
| --- | --- |
| `/sessions` | Interactive picker of recent sessions — ↑/↓ to navigate, Enter to resume (stored transcript is replayed, kube context restored), Esc to cancel |
| `/resume` | Alias for `/sessions` |
| `/history` | Replay messages in the **current** session — `/history` (all), `/history N` (last N), `/history X-Y` (range), `/history #N` (single) |
| `/forget` | Delete the current session from local history (server data untouched) |

### History & branching

| Command | Description |
| --- | --- |
| `/search <query>` | Full-text search across all past sessions with highlighted snippets |
| `/branch` | Fork this conversation at the current point into a new independent session |
| `/branches` | List all forks of (and siblings of) this session |
| `/title <text>` | Rename the current session |

FTS5 boolean syntax is supported: `/search pods AND NOT staging`

### Connection & config

| Command | Description |
| --- | --- |
| `/config` | Print every config key, its value, and where it came from |
| `/config set KEY=VALUE` | Write a key to `~/.kube-q/.env` — validated and takes effect immediately |
| `/config reset KEY` | Remove a single key from `~/.kube-q/.env` |
| `/config reset` | Wipe `~/.kube-q/.env` entirely |

`KEY` accepts the full env-var name (`KUBE_Q_URL`) or the short alias (`url`).

```
/config set url=https://api.kubeintellect.com
/config set api_key=your-key-here
/config set model=kubeintellect-v2
/config reset api_key
/config
```

### Token usage

| Command | Description |
| --- | --- |
| `/tokens` | Show token counts and estimated cost for this session |
| `/cost` | Alias for `/tokens` |

### Human-in-the-Loop

| Command | Description |
| --- | --- |
| `/approve` | Approve a pending HITL action — the AI executes it |
| `/deny` | Deny a pending HITL action — nothing is applied |

**Keyboard shortcuts:**

| Key | Action |
| --- | --- |
| `Enter` | Send message |
| `Alt+Enter` or `Esc` → `Enter` | Insert newline (multi-line input) |
| `Tab` | Auto-complete slash commands |
| `↑` / `↓` | Scroll through input history |
| `Ctrl+C` | Cancel current input |
| `Ctrl+D` | Exit the session |

---

## File attachments

Embed a file's contents directly in your message using `@`:

```text
what is wrong with this deployment? @deployment.yaml
compare these two configs: @old.yaml @new.yaml
what is wrong here? @pod.yaml @service.yaml
```

Supports: `yaml`, `json`, `py`, `sh`, `go`, `tf`, `toml`, `js`, `ts`, `rs`, `java`, `xml`, `html`, `md`, `txt`, `log`, and more. Limit: 100 KB per file. Quote paths with spaces: `@"my file.yaml"`.

---

## CLI reference

```text
kq [options]
```

### Flags

| Flag | Default | Description |
| --- | --- | --- |
| `--url URL` | `https://api.kubeintellect.com` | kube-q API base URL (env: `KUBE_Q_URL`) |
| `--query` / `-q TEXT` | — | Run a single query and exit |
| `--no-stream` | off | Disable streaming — wait for full response |
| `--session-id ID` | — | Resume a previous session by ID — replays the stored transcript on launch (use `/sessions` inside the REPL for an arrow-key picker) |
| `--list` | — | List recent sessions and exit |
| `--search QUERY` | — | Full-text search across session history and exit |
| `--user-id ID` | auto | Persistent user ID (saved to `~/.kube-q/user-id`) |
| `--api-key KEY` | — | Bearer token for auth-enabled servers (env: `KUBE_Q_API_KEY`) |
| `--ca-cert PATH` | — | Custom CA certificate bundle for TLS |
| `--output {rich,plain}` | `rich` | `rich` for markdown rendering, `plain` for raw text |
| `--model NAME` | `kubeintellect-v2` | Model name sent in requests (env: `KUBE_Q_MODEL`) |
| `--user-name NAME` | `You` | Your display name in the prompt (env: `KUBE_Q_USER_NAME`) |
| `--agent-name NAME` | `kube-q` | Assistant name in saved conversations (env: `KUBE_Q_AGENT_NAME`) |
| `--no-banner` | off | Suppress logo (useful for screen recordings) |
| `--debug` | off | Log raw HTTP requests/responses to stderr and `~/.kube-q/kube-q.log` |
| `--version` | — | Print version and exit |
| `--backend {kube-q,openai,azure}` | `kube-q` | Pick the LLM backend (env: `KUBE_Q_BACKEND`) |
| `--openai-api-key KEY` | — | API key for the direct OpenAI backend (env: `KUBE_Q_OPENAI_API_KEY`) |
| `--openai-endpoint URL` | `https://api.openai.com` | Override OpenAI endpoint (env: `KUBE_Q_OPENAI_ENDPOINT`) |
| `--azure-openai-api-key KEY` | — | Azure OpenAI API key (env: `KUBE_Q_AZURE_OPENAI_API_KEY`) |
| `--azure-openai-endpoint URL` | — | Azure OpenAI resource URL (env: `KUBE_Q_AZURE_OPENAI_ENDPOINT`) |
| `--azure-openai-deployment NAME` | — | Azure OpenAI deployment name (env: `KUBE_Q_AZURE_OPENAI_DEPLOYMENT`) |
| `--profile NAME` | — | Load `~/.kube-q/profiles/<NAME>.env` on top of defaults (env: `KUBE_Q_PROFILE`) |
| `--context NAME` | — | Set active kubectl context at launch (env: `KUBE_Q_CONTEXT`) |

---

## Session history

kube-q saves every conversation to a local SQLite database at `~/.kube-q/history.db`. Nothing is sent to or read from the server — this is a local-only mirror.

```bash
# See recent sessions
kq --list

# Resume from where you left off
kq --session-id <id>

# Search across everything you've ever discussed
kq --search "deployment rollback"
kq --search "pods AND crash"
```

Inside the REPL, `/sessions`, `/history`, `/forget`, `/search`, `/branch`, `/branches`, and `/title` give you full control over history.

**Branching** forks a conversation at the current message count. The original session is never modified — you get a new independent session you can take in a different direction. Branches show up in `kq --list` as regular sessions.

---

## Token & cost tracking

After every response kube-q shows the token count in the footer:

```
kube-q  (1.2s · 460 tokens)
```

Use `/tokens` or `/cost` for a session summary:

```
┌─ Token Usage ─────────────────────────┐
│ This session:                         │
│   Prompt:     1,240 tokens            │
│   Completion: 3,890 tokens            │
│   Total:      5,130 tokens            │
│   Requests:   8                       │
│   Est. cost:  $0.0312                 │
│                                       │
│ Last response:                        │
│   120 in → 340 out ($0.0024)          │
└───────────────────────────────────────┘
```

Cost estimates are labeled "Est." — not exact. Built-in rates for `kubeintellect-v2`, `gpt-4o`, `gpt-4o-mini`, and `claude-sonnet-4-6`. Override for custom backends:

```bash
KUBE_Q_COST_PER_1K_PROMPT=0.002
KUBE_Q_COST_PER_1K_COMPLETION=0.008
```

If the server doesn't emit a `usage` block, the footer omits the token count — no errors, no noise.

---

## Configuration

kube-q loads configuration from `.env` files and environment variables. Priority order (highest wins):

```
CLI flag  >  shell env var  >  ./.env  >  ~/.kube-q/.env  >  default
```

### .env files

| Location | Priority | Use case |
| --- | --- | --- |
| `~/.kube-q/.env` | lower | Persistent user-level defaults |
| `./.env` (current directory) | higher | Project-local or per-cluster overrides |

Shell-exported variables always win over `.env` files.

### All supported variables

```bash
KUBE_Q_URL=https://api.kubeintellect.com
KUBE_Q_API_KEY=your-key-here
KUBE_Q_MODEL=kubeintellect-v2
KUBE_Q_TIMEOUT=120
KUBE_Q_HEALTH_TIMEOUT=5
KUBE_Q_NAMESPACE_TIMEOUT=3
KUBE_Q_STARTUP_RETRY_TIMEOUT=300
KUBE_Q_STARTUP_RETRY_INTERVAL=5
KUBE_Q_STREAM=true
KUBE_Q_OUTPUT=rich                  # rich | plain
KUBE_Q_LOG_LEVEL=INFO               # DEBUG | INFO | WARNING | ERROR
KUBE_Q_USER_NAME=You
KUBE_Q_AGENT_NAME=kube-q
KUBE_Q_COST_PER_1K_PROMPT=0.003    # override cost rate for /tokens
KUBE_Q_COST_PER_1K_COMPLETION=0.006

# ── Backend selection ─────────────────────────────────────────────────────────
KUBE_Q_BACKEND=kube-q              # kube-q | openai | azure
KUBE_Q_OPENAI_API_KEY=sk-...       # used when backend=openai
KUBE_Q_OPENAI_ENDPOINT=https://api.openai.com
KUBE_Q_OPENAI_MODEL=gpt-4o-mini
KUBE_Q_AZURE_OPENAI_API_KEY=...    # used when backend=azure
KUBE_Q_AZURE_OPENAI_ENDPOINT=https://my-resource.openai.azure.com
KUBE_Q_AZURE_OPENAI_DEPLOYMENT=my-gpt-4o
KUBE_Q_AZURE_OPENAI_API_VERSION=2024-06-01

# ── Multi-cluster ─────────────────────────────────────────────────────────────
KUBE_Q_CONTEXT=prod-cluster        # initial kubectl context (also set live via /context)
KUBE_Q_PROFILE=prod                # load ~/.kube-q/profiles/prod.env on top of defaults
KUBE_Q_PLUGIN_DIR=~/.kube-q/plugins  # override plugin directory
```

### Example — per-cluster setup

```bash
# .env in your cluster's working directory
KUBE_Q_URL=https://kube-q.prod.example.com
KUBE_Q_API_KEY=prod-secret-key
KUBE_Q_USER_NAME=alice
```

Run `kq` from that directory and it picks up the settings automatically.

### Quick one-time setup (pip users)

```bash
mkdir -p ~/.kube-q
cat >> ~/.kube-q/.env <<'EOF'
KUBE_Q_URL=https://kube-q.example.com
KUBE_Q_API_KEY=your-key-here
EOF
```

---

## Multi-backend & multi-cluster

One `kq` binary drives three LLM backends and any number of Kubernetes clusters. Backends are chosen at launch; kubectl context switches live in the REPL; profiles bundle both together.

### Backend selection

```bash
# kube-q server (default) — no extra config
kq

# Direct OpenAI — bypass the kube-q server entirely
kq --backend openai --openai-api-key sk-...

# Azure OpenAI — deployment-specific URL, api-key header
kq --backend azure \
   --azure-openai-api-key    ... \
   --azure-openai-endpoint   https://my-resource.openai.azure.com \
   --azure-openai-deployment gpt-4o
```

The backend is fixed for the lifetime of one REPL — switch by restarting with a different flag or profile. `/state` and the header panel show the active backend.

### Kubernetes context (live, no restart)

```text
/context prod-cluster      # Tab-completes from kubectl config get-contexts
/context                   # clear — no context prepended
```

The active context is prepended to every user message as `[context: kube_context=X]` so the backend knows which cluster to target. Set it at launch with `--context prod-cluster` or `KUBE_Q_CONTEXT=prod-cluster`.

### Profiles (bundle backend + keys + context per environment)

Profiles live in `~/.kube-q/profiles/<name>.env` and are loaded between `~/.kube-q/.env` and `./.env` when selected.

```bash
# Create a profile from a template
kq config profile new prod
# edit ~/.kube-q/profiles/prod.env — set KUBE_Q_BACKEND, KUBE_Q_CONTEXT, API keys, etc.

kq config profile list          # list profiles
kq config profile show prod     # dump a profile's contents
kq config profile delete staging

kq --profile prod               # launch with that profile
KUBE_Q_PROFILE=prod kq          # same, via env var

/profile                        # inside REPL: list profiles, show which is active
```

Profile switching requires a restart (the REPL shows the exact command for you).

### Plugins (custom slash commands)

Any `.py` file in `~/.kube-q/plugins/` (or `$KUBE_Q_PLUGIN_DIR`) is auto-imported at REPL startup:

```python
# ~/.kube-q/plugins/hello.py
from kube_q.plugins import register

@register("/hello", help="Say hello")
def hello(ctx):
    ctx.print(f"hi {ctx.cfg.user_name} — args: {ctx.args!r}")
```

`ctx` exposes `args`, `state` (the live `SessionState`), `cfg` (the current `ReplConfig`), `print(text)`, and the Rich `console`. Use `/plugins` to list what's loaded. Plugins dispatch before the typo-catcher, so they always win.

---

## Authentication

When the server has API key authentication enabled, requests without a valid key are rejected with HTTP 401. kube-q shows a clear message:

```
Authentication required. Set KUBE_Q_API_KEY or pass --api-key with a valid key.
Ask your administrator for an API key.
```

When auth is disabled on the server, no key is needed.

---

## Human-in-the-Loop (HITL)

When the AI backend requests approval before executing a potentially destructive action, kube-q pauses:

```
╭─ Action requires approval ──────────────────╮
│ Action requires approval.                   │
│ Type /approve to proceed or /deny to cancel.│
╰─────────────────────────────────────────────╯
HITL> /approve
```

The prompt changes to `HITL>` while an action is pending. Type `/approve` to execute it or `/deny` to cancel.

---

## Python SDK

`kube_q.core` exposes a typed SDK you can use directly in scripts, notebooks, or other tools — no CLI required.

```python
from kube_q.core.client import KubeQClient
from kube_q.core.events import TokenEvent, FinalEvent

client = KubeQClient(url="http://localhost:8000", api_key="...")

# Non-streaming query
result = client.query("why are my pods failing?")
print(result["text"])

# Streaming — typed event objects
for event in client.stream("list all deployments in default namespace"):
    match event:
        case TokenEvent(data=d):
            print(d.content, end="", flush=True)
        case FinalEvent():
            break
```

All backend events are modelled as a typed Pydantic discriminated union in `kube_q.core.events`:

| Event type | Data fields |
| --- | --- |
| `token` | `content`, `role` |
| `status` | `phase`, `message` |
| `tool_call` | `tool_name`, `args`, `call_id`, `dry_run` |
| `tool_result` | `call_id`, `ok`, `summary`, `truncated` |
| `hitl_request` | `action`, `risk`, `diff`, `approval_id` |
| `usage` | `prompt_tokens`, `completion_tokens`, `total_tokens`, `model` |
| `final` | `content`, `usage`, `elapsed_ms` |
| `error` | `code`, `message`, `retryable` |

---

## Web frontend

The `web/` directory contains a Next.js web UI for kube-q.

### Browser chat

Three-pane desktop layout (resizable panels):
- **Chat panel** — streaming markdown responses with `react-markdown` + syntax highlighting
- **Reasoning timeline** — live status, tool calls, and tool results as they happen
- **Terminal panel** — xterm.js view of tool execution output

Tabbed mobile layout, dark mode, and bearer-token auth gate included.

### PTY terminal (full CLI in the browser)

The `/pty` route spawns `kq` in a pseudo-terminal via WebSocket. It's a pure byte relay — the Python CLI handles all logic; xterm.js renders it.

```bash
cd web
npm install
npm run dev:pty     # starts Next.js + pty-server on separate ports
```

Open `http://localhost:3000/pty` to get a full terminal running your local `kq` binary in the browser.

A coloured status dot in the toolbar shows the live connection state (`connected`, `reconnecting…`, `error`). If the WebSocket drops unexpectedly the terminal auto-reconnects with exponential backoff (up to 8 attempts, 1s → 15s).

To require a token before clients can spawn `kq`, set `PTY_AUTH_TOKEN` on the server and share it with users — they click the 🔑 Token button to enter it (stored in `sessionStorage`). Connections with a missing or wrong token are rejected with WebSocket close code 1008.

---

## Data & privacy

- Session history is stored **locally only** at `~/.kube-q/history.db` (SQLite). Nothing is sent to the kube-q server.
- Conversations may contain sensitive cluster data. Use `/save` with care — saved files go wherever you point them.
- The user ID (`~/.kube-q/user-id`) is stored with `0600` permissions.
- Logs are written to `~/.kube-q/kube-q.log` (rotating, 5 MB × 3 files).

---

## License

Dual-licensed under **AGPL-3.0-or-later** (see [LICENSE](LICENSE)) or a **commercial
license** — contact mohsen.seyedkazemi@gmail.com. See the top-level `LICENSING.md`.
