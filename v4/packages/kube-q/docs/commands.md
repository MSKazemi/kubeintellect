# In-REPL Commands

All commands start with `/`. Press `Tab` after `/` to see completions with descriptions. Close matches trigger a typo hint (e.g. `/hlep` → "did you mean /help?").

---

## Connection & config

Configure the backend URL, API key, and other settings without leaving `kq`. Settings are written to `~/.kube-q/.env` and take effect **immediately** in the current session.

| Command | Description |
|---|---|
| `/config` | Print every config key, its value, and where it came from |
| `/config set KEY=VALUE` | Write a key to `~/.kube-q/.env` (validated, takes effect immediately) |
| `/config reset KEY` | Remove a single key from `~/.kube-q/.env` |
| `/config reset` | Wipe `~/.kube-q/.env` entirely |

`KEY` accepts either the full env-var name (`KUBE_Q_URL`) or the short alias (`url`).

**Examples**

```
/config set url=https://kube-q.example.com
/config set api_key=your-key-here
/config set model=kubeintellect-v2

/config reset api_key
/config
```

---

## Conversation

| Command | Description |
|---|---|
| `/new` | Start a fresh conversation — clears history, generates a new session ID |
| `/id` | Show the current conversation ID |
| `/state` | Show full session state: ID, user, message count, tokens, namespace, HITL flag |
| `/title <text>` | Rename the current session |
| `/save` | Save conversation to a timestamped Markdown file |
| `/save <file>` | Save to a specific file — Tab completes paths |
| `/clear` | Clear the terminal screen |
| `/help` | Show the help overview and topic list |
| `/help <topic>` | Drill into one area — e.g. `/help sessions`, `/help config`, `/help hitl` |
| `/version` | Print the installed kube-q version |
| `/quit` / `/exit` / `/q` | Exit kube-q |

**Examples**

```
/title Production incident — OOM in payment service
/save ~/reports/incident-2026-04.md
/state
/new
```

---

## Namespace

| Command | Description |
|---|---|
| `/ns <name>` | Set active namespace — prepended to every query automatically |
| `/ns` | Clear the active namespace |

The active namespace is shown in the prompt. Tab-completes from the cluster (cached after first use).

**Examples**

```
/ns production
/ns kube-system
/ns
```

---

## Kubernetes context

| Command | Description |
|---|---|
| `/context <name>` | Set active kubectl context — prepended to every query as `[context: X]` |
| `/context` | Clear the active context |

Context names Tab-complete from your kubeconfig. Set at launch with `--context <name>` or `KUBE_Q_CONTEXT=<name>`.

**Examples**

```
/context prod-cluster
/context staging-gke
/context
```

---

## Session history

| Command | Description |
|---|---|
| `/sessions` | Interactive picker of the 20 most recent sessions — **↑/↓** to navigate, **Enter** to resume, **Esc** to cancel |
| `/resume` | Alias for `/sessions` |
| `/list` | Print recent sessions as a plain table (same data, no interactive picker) |
| `/history` | Replay all messages in the current session |
| `/history <N>` | Show the last **N** messages |
| `/history <X-Y>` | Show messages **X** through **Y** (1-indexed, inclusive) |
| `/history #<N>` | Show just message **#N** |
| `/forget` | Delete the current session from local history (server-side data untouched) |

Resuming a session via `/sessions` also restores the saved kube context. Each replayed message is prefixed with `[#N]` so you can reference specific messages.

**Examples**

```
/sessions
/list
/history 5
/history 2-8
/history #3
/forget
```

---

## Search & branching

| Command | Description |
|---|---|
| `/search <query>` | Full-text search across all past sessions with highlighted snippets |
| `/branch` | Fork this conversation at the current point into a new independent session |
| `/branches` | List all forks (and siblings) of this session |

FTS5 boolean syntax is supported:

**Examples**

```
/search "crash loop" AND production
/search pods AND NOT staging
/search "oom killed" OR "memory limit"
/search deployment AND rollback

/branch
/branches
```

---

## Profiles & plugins

| Command | Description |
|---|---|
| `/profile` | List profiles in `~/.kube-q/profiles/` and show which is active |
| `/profile new <name>` | Create a new profile `.env` from the template |
| `/profile show <name>` | Print a profile's contents |
| `/profile delete <name>` | Delete a profile file |
| `/profile <name>` | Show the restart command to activate a profile |
| `/plugins` | List slash commands registered by plugins in `~/.kube-q/plugins/` |

Profiles are `.env` fragments — one per cluster or environment. Switching profiles requires a restart. See [Configuration](configuration.md#profiles-per-cluster) for details.

**Examples**

```
/profile
/profile new staging
/profile show staging
/profile delete old-profile

# To switch, restart with:
kq --profile staging
```

---

## Token usage

| Command | Description |
|---|---|
| `/tokens` | Show token counts and estimated cost for this session |
| `/cost` | Alias for `/tokens` |

Override cost rates via `KUBE_Q_COST_PER_1K_PROMPT` and `KUBE_Q_COST_PER_1K_COMPLETION` env vars. See [Token Tracking](token-tracking.md) for details.

**Examples**

```
/tokens
/cost
```

---

## Human-in-the-Loop

| Command | Description |
|---|---|
| `/approve` | Approve a pending action — the AI executes it |
| `/deny` | Deny a pending action — nothing is applied |

The prompt changes to `HITL>` while an action is pending. See [Human-in-the-Loop](hitl.md) for details.

**Examples**

```
HITL> /approve
HITL> /deny
```

---

## Keyboard shortcuts

| Key | Action |
|---|---|
| `Enter` | Send message |
| `Alt+Enter` or `Esc` → `Enter` | Insert newline (multi-line input) |
| `Tab` | Auto-complete slash commands and paths |
| `↑` / `↓` | Scroll through input history |
| `Ctrl+A` | Jump to start of line |
| `Ctrl+E` | Jump to end of line |
| `Ctrl+W` | Delete previous word |
| `Ctrl+U` | Clear entire input buffer |
| `Ctrl+C` | Cancel current input (keeps history) |
| `Ctrl+D` | Exit the session |

---

## File attachments

Type `@path/to/file` anywhere in your message to attach a file. Its contents are embedded as a code block and sent with the message.

```
What's wrong here? @pod.yaml
Compare these two: @deploy-prod.yaml @deploy-staging.yaml
Check my config: @~/configs/service.json
```

Supported types: YAML, JSON, Python, Shell, Go, Terraform, text, logs, and more. Limit: 100 KB per file.

---

## Launch flags (quick reference)

These are CLI flags passed when starting `kq`, not REPL commands. See [Configuration](configuration.md) for the full list.

```bash
# Connect to a specific backend
kq --url https://kube-q.example.com --api-key sk-your-key

# One-shot query, no REPL
kq --query "why are payment pods crashlooping?"

# Resume a previous session
kq --session-id abc123

# Search past sessions
kq --search "oom killed"

# Use a profile
kq --profile staging

# Skip the startup health check
kq --no-health-check

# Plain text output (no markdown rendering)
kq --output plain

# Show raw HTTP traffic
kq --debug
```
