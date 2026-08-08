---
tags:
  - reference
---

# Frequently Asked Questions

---

## General

### What is kube-q?

kube-q is an AI-powered terminal client for Kubernetes. It lets you query, debug, and manage any Kubernetes cluster by typing in plain English — no kubectl syntax required. It connects to a kube-q backend server that has access to your cluster.

### What does kube-q do, exactly?

You ask a question in the terminal. kube-q sends it to a kube-q backend, which uses an AI model to run `kubectl` commands, read pod logs, inspect events, and assemble an expert-level answer. The response streams back to your terminal in real time.

### Is kube-q a standalone tool or does it need a server?

kube-q is a **client**. It requires a running **kube-q backend server** that has Kubernetes API access. The client (`kq`) connects to that server over HTTP/SSE. The server is a separate component — see [Deployment & Hosting](deployment.md) for options.

### Does kube-q ever touch my cluster directly?

No. The `kq` client only talks to the kube-q HTTP/SSE API. All cluster operations happen on the backend server. The client never has `kubeconfig` or direct cluster access.

### What AI models does kube-q support?

That depends on your backend. The client sends a `model` parameter (default: `kubeintellect-v2`) — the backend decides which model to use. You can override the model name with `--model` or `KUBE_Q_MODEL`.

---

## Installation

### What Python version is required?

Python **3.12 or later**. kube-q uses `match`/`case` structural pattern matching, `tomllib`, and other 3.12+ features.

### Can I install kube-q without polluting my system Python?

Yes — use [pipx](https://pipx.pypa.io):

```bash
pipx install kube-q
```

This installs `kq` in an isolated virtualenv and puts it on your `PATH` automatically.

### How do I upgrade to the latest version?

=== "pip"
    ```bash
    pip install --upgrade kube-q
    ```

=== "pipx"
    ```bash
    pipx upgrade kube-q
    ```

=== "Homebrew"
    ```bash
    brew upgrade kube-q
    ```

### Is there a Docker image I can use instead?

Yes — the Docker image bundles the full `kq` CLI plus a browser-based terminal:

```bash
docker run -p 3000:3000 \
  -e KUBE_Q_URL=https://kube-q.example.com \
  -e KUBE_Q_API_KEY=your-key \
  ghcr.io/mskazemi/kube_q:latest
```

Open `http://localhost:3000` for a browser REPL. See [Web UI](web-ui.md).

---

## Configuration

### Where does kube-q store its data?

| Path | What's stored |
|------|---------------|
| `~/.kube-q/.env` | Your config (you create this) |
| `~/.kube-q/user-id` | Auto-generated persistent user ID |
| `~/.kube-q/history.db` | SQLite session history |
| `~/.kube-q/kube-q.log` | Debug log (only in `--debug` mode) |

### How do I connect to multiple clusters?

Create a `.env` file per cluster directory:

```
~/clusters/
  prod/.env        → KUBE_Q_URL=https://kube-q.prod.example.com
  staging/.env     → KUBE_Q_URL=https://kube-q.staging.example.com
```

Run `kq` from each directory and it picks up the local `.env` automatically.

### Can I use kube-q without an API key?

Yes, if your backend doesn't require authentication. Simply omit `KUBE_Q_API_KEY` and the `--api-key` flag.

### How do I set a default namespace?

Either:

1. Use `/ns <name>` inside the REPL to set it for that session.
2. Or set it in your `.env` — there's no static `KUBE_Q_NAMESPACE` variable, but you can use a startup alias: `alias kq='kq --query "/ns production"'` is not ideal; instead just use `/ns` at the start of each session.

---

## Usage

### Does kube-q remember past conversations?

Yes. Every conversation is saved to a local SQLite database (`~/.kube-q/history.db`). Nothing is stored on the server.

- List sessions (table, exits): `kq --list`
- Resume interactively: type `/sessions` (or `/resume`) in the REPL — arrow-key picker, Enter to resume in place, Esc to cancel; the stored transcript is replayed so you see the conversation before continuing
- Resume by ID from the shell: `kq --session-id <id>` — also replays the stored transcript on launch
- Search history: `kq --search "pod crash"` or `/search pod crash`

### Can I use kube-q in scripts and CI?

Yes — use `--query` mode:

```bash
kq --query "list all failing pods" --output plain
```

`--output plain` disables Rich markdown rendering for clean stdout that pipes well. In `--query` mode, kube-q prints the response and exits immediately.

### What file types can I attach with `@`?

`yaml`, `json`, `py`, `sh`, `go`, `tf`, `toml`, `js`, `ts`, `rs`, `java`, `xml`, `html`, `md`, `txt`, `log`, and others. The limit is **100 KB per file**.

### Is the conversation context sent to the server on every message?

Yes. kube-q maintains a client-side message history and sends the full conversation on each request. This is how the AI understands follow-up questions. History is stored locally.

### Can I fork a conversation without losing the original?

Yes — use `/branch`. It creates a new independent session at the current message count. The original is never modified. List forks with `/branches`.

---

## Security & Privacy

### Are my cluster credentials stored anywhere by kube-q?

No. The `kq` client stores nothing about your cluster. All cluster access happens on the backend server. The client only stores session message history, token counts, and user preferences in `~/.kube-q/`.

### Can kube-q execute destructive commands without my approval?

Only if the Human-in-the-Loop feature is disabled on the backend. When HITL is enabled, any destructive action (delete, scale, patch) is paused and shown to you — nothing runs until you type `/approve`.

### Is traffic between the client and backend encrypted?

Use `https://` in `KUBE_Q_URL` to enable TLS. For custom CA certificates (corporate proxies, self-signed certs), use `--ca-cert /path/to/ca.pem`.

---

## Troubleshooting

### kube-q can't connect to my backend

Check your `KUBE_Q_URL` and that the backend is reachable:

```bash
curl -s $KUBE_Q_URL/health
```

See the full [Troubleshooting Guide](troubleshooting.md).

### The response cuts off mid-stream

This is usually a server-side timeout. Try:

```bash
KUBE_Q_TIMEOUT=300 kq
```

### I'm getting a 401 Unauthorized error

Your `KUBE_Q_API_KEY` is missing, wrong, or expired. See the [Troubleshooting Guide](troubleshooting.md#authentication-errors).
