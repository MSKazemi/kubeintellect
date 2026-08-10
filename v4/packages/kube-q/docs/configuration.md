# Configuration

kube-q loads configuration from `.env` files and environment variables. CLI flags override everything.

## Priority order

```
CLI flag  >  shell env var  >  ./.env  >  ~/.kube-q/.env  >  built-in default
```

---

## .env file locations

| Location | Priority | Use case |
|---|---|---|
| `~/.kube-q/.env` | lower | Persistent user-level defaults across all clusters |
| `./.env` (current directory) | higher | Per-project or per-cluster overrides |

Shell-exported variables always win over `.env` files. You can mix both — common settings in `~/.kube-q/.env`, cluster-specific overrides in `./.env`.

---

## All variables

```bash
# ── Connection ────────────────────────────────────────────────────────────────
KUBE_Q_URL=https://api.kubeintellect.com  # kube-q API base URL
KUBE_Q_API_KEY=                         # Bearer token (required when server auth is enabled)
KUBE_Q_MODEL=kubeintellect-v2           # Model name sent in requests

# ── Timeouts (seconds) ────────────────────────────────────────────────────────
KUBE_Q_TIMEOUT=120                      # Per-query HTTP timeout
KUBE_Q_HEALTH_TIMEOUT=5                 # Health-check HTTP timeout
KUBE_Q_NAMESPACE_TIMEOUT=3             # Namespace fetch timeout
KUBE_Q_STARTUP_RETRY_TIMEOUT=300       # How long to retry on startup before giving up
KUBE_Q_STARTUP_RETRY_INTERVAL=5        # Seconds between startup retries

# ── Output ────────────────────────────────────────────────────────────────────
KUBE_Q_STREAM=true                      # Enable/disable streaming responses
KUBE_Q_OUTPUT=rich                      # rich | plain
KUBE_Q_LOG_LEVEL=INFO                   # DEBUG | INFO | WARNING | ERROR
KUBE_Q_SKIP_HEALTH_CHECK=false          # Skip startup health-check loop

# ── Display names ─────────────────────────────────────────────────────────────
KUBE_Q_USER_NAME=You                    # Your display name in the prompt
KUBE_Q_AGENT_NAME=kube-q               # Assistant name in saved conversations

# ── Custom branding ───────────────────────────────────────────────────────────
KUBE_Q_LOGO=KubeIntellect               # Custom ASCII banner logo (\\n for newlines)
KUBE_Q_TAGLINE=© 2025 Acme Corp        # Custom tagline / copyright line

# ── Token cost overrides ──────────────────────────────────────────────────────
KUBE_Q_COST_PER_1K_PROMPT=0.003        # Override prompt token cost rate
KUBE_Q_COST_PER_1K_COMPLETION=0.006    # Override completion token cost rate

# ── Backend selection ─────────────────────────────────────────────────────────
KUBE_Q_BACKEND=kube-q                  # kube-q | openai | azure
KUBE_Q_OPENAI_API_KEY=                 # used when backend=openai
KUBE_Q_OPENAI_ENDPOINT=https://api.openai.com
KUBE_Q_OPENAI_MODEL=gpt-4o-mini
KUBE_Q_AZURE_OPENAI_API_KEY=           # used when backend=azure
KUBE_Q_AZURE_OPENAI_ENDPOINT=https://my-resource.openai.azure.com
KUBE_Q_AZURE_OPENAI_DEPLOYMENT=        # deployment name, NOT model name
KUBE_Q_AZURE_OPENAI_API_VERSION=2024-06-01

# ── Multi-cluster ─────────────────────────────────────────────────────────────
KUBE_Q_CONTEXT=                        # initial kubectl context (also set live via /context)
KUBE_Q_PROFILE=                        # load ~/.kube-q/profiles/<name>.env on top of defaults
KUBE_Q_PLUGIN_DIR=                     # override plugin directory (default ~/.kube-q/plugins/)
```

---

## Quick setup

### Minimal (user-level)

```bash
mkdir -p ~/.kube-q
cat > ~/.kube-q/.env <<'EOF'
KUBE_Q_URL=https://kube-q.example.com
KUBE_Q_API_KEY=your-api-key
EOF
```

### Per-cluster override

Create `.env` in your cluster's working directory:

```bash
# .env
KUBE_Q_URL=https://kube-q.prod.example.com
KUBE_Q_API_KEY=prod-key
KUBE_Q_USER_NAME=alice
KUBE_Q_MODEL=kubeintellect-v2
```

Run `kq` from that directory — it picks up the overrides automatically.

### Multiple clusters

```
~/clusters/
  prod/.env        → KUBE_Q_URL=https://kube-q.prod.example.com
  staging/.env     → KUBE_Q_URL=https://kube-q.staging.example.com
  dev/.env         → KUBE_Q_URL=http://localhost:8000
```

---

## Custom branding

Operators can replace the startup banner and tagline:

```bash
# .env
KUBE_Q_LOGO=KubeIntellect\nPowered by AI
KUBE_Q_TAGLINE=© 2025 Acme Corp — Internal Use Only
```

Use `\n` for line breaks in the logo. Set `KUBE_Q_LOGO=` (empty) to suppress the logo entirely.

---

## Files written by kube-q

| Path | Description |
|---|---|
| `~/.kube-q/.env` | User config (you create this) |
| `~/.kube-q/profiles/<name>.env` | Named profile fragments — see [Profiles per cluster](#profiles-per-cluster) |
| `~/.kube-q/plugins/*.py` | User-authored slash commands — see [Plugins](#plugins) |
| `~/.kube-q/user-id` | Auto-generated persistent user ID (`0600` permissions) |
| `~/.kube-q/history.db` | SQLite session history database |
| `~/.kube-q/kube-q.log` | Rotating log (5 MB × 3 files, written in `--debug` mode) |

---

## Backend selection

kube-q can talk to three backends with a single CLI — pick one per launch:

| Backend | What it hits | Auth header |
|---|---|---|
| `kube-q` *(default)* | Your kube-q API server, `/v1/chat/completions` | `Authorization: Bearer` (optional) |
| `openai` | `https://api.openai.com/v1/chat/completions` | `Authorization: Bearer` |
| `azure` | `https://<resource>.openai.azure.com/openai/deployments/<deployment>/chat/completions?api-version=...` | `api-key: <key>` |

```bash
# Direct OpenAI
kq --backend openai --openai-api-key sk-...

# Azure OpenAI
kq --backend azure \
   --azure-openai-api-key    ... \
   --azure-openai-endpoint   https://my-resource.openai.azure.com \
   --azure-openai-deployment gpt-4o
```

Validation catches missing keys up front:

- `backend=openai` requires `KUBE_Q_OPENAI_API_KEY`
- `backend=azure` requires `KUBE_Q_AZURE_OPENAI_API_KEY`, `_ENDPOINT`, and `_DEPLOYMENT`

The backend is fixed for the lifetime of one REPL. `/state` shows the active backend label; the plain-HTTP warning is suppressed for `openai` / `azure`; and `/healthz` is skipped for non-`kube-q` backends (which don't expose one).

---

## Profiles per cluster

Profiles are `.env` fragments that bundle a full environment (backend + keys + kubectl context + custom branding) into one file. They live in `~/.kube-q/profiles/<name>.env` and are loaded between `~/.kube-q/.env` and `./.env` when selected.

```bash
# Create a profile from a commented template
kq config profile new prod

# Edit ~/.kube-q/profiles/prod.env, e.g.:
#   KUBE_Q_URL=https://kube-q.prod.example.com
#   KUBE_Q_API_KEY=prod-key
#   KUBE_Q_CONTEXT=prod-cluster
#   KUBE_Q_USER_NAME=alice

# Launch with the profile
kq --profile prod
KUBE_Q_PROFILE=prod kq           # equivalent

# Manage profiles
kq config profile list
kq config profile show prod
kq config profile delete staging
```

Precedence (high → low, highest wins):

```
CLI flag  >  shell env  >  ./.env  >  ~/.kube-q/profiles/<name>.env  >  ~/.kube-q/.env  >  default
```

`/profile` inside the REPL lists profiles and marks the active one; `/profile <name>` prints the restart command (switching profiles mid-session is not supported).

---

## Plugins

Drop a `.py` file into `~/.kube-q/plugins/` (or the directory named by `KUBE_Q_PLUGIN_DIR`) and call `register()` to add your own slash commands:

```python
# ~/.kube-q/plugins/hello.py
from kube_q.plugins import register

@register("/hello", help="Say hello and show config")
def hello(ctx):
    ctx.print(f"hi {ctx.cfg.user_name} — backend={ctx.cfg.backend_label}")
    ctx.print(f"args passed: {ctx.args!r}")
```

The `ctx` object exposes:

- `args` — everything the user typed after the command (string)
- `state` — the live `SessionState` (conversation id, messages, namespace, current context, HITL flag)
- `cfg` — the current `ReplConfig` (display names, backend info, URL, …)
- `print(text)` — convenience for writing to the Rich console
- `console` — the raw Rich console for advanced rendering

Plugins run in-process with full Python access — **only install plugins you trust**. Import failures are logged and the REPL continues to start. `/plugins` lists what's loaded; plugin commands dispatch before the unknown-command typo-catcher, so they always win.
