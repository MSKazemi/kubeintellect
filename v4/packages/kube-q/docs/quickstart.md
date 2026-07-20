---
tags:
  - getting-started
---

# Quick Start

Get kube-q running and talk to your first cluster in under 5 minutes.

---

## Prerequisites

Before you begin, make sure you have:

- [x] Python **3.12** or later
- [x] A running **kube-q backend** (the server your cluster is connected to)
- [x] The backend URL and API key (if auth is enabled)

!!! info "Don't have a backend yet?"
    kube-q is a client. It connects to a kube-q server that has access to your Kubernetes cluster. If you're setting up your own server, see [Deployment & Hosting](deployment.md).

---

## Step 1 — Install

=== ":material-package-variant: pipx (recommended)"

    [pipx](https://pipx.pypa.io) installs `kq` in an isolated virtualenv and puts it on your `PATH` — no pollution of your system Python. Works on Linux, macOS, and Windows.

    ```bash
    pip install pipx       # skip if you already have pipx
    pipx ensurepath        # adds pipx and installed tools to PATH (all platforms)
    ```

    Then **restart your shell** (or open a new terminal), and:

    ```bash
    pipx install kube-q
    kq --version
    ```

    !!! tip "macOS alternative"
        On macOS, prefer `brew install pipx && pipx ensurepath` — Homebrew keeps pipx itself updated.

=== ":material-language-python: pip"

    ```bash
    pip install kube-q
    kq --version
    ```

=== ":material-apple: Homebrew"

    ```bash
    brew tap MSKazemi/kube-q
    brew install kube-q
    kq --version
    ```

=== ":material-source-branch: From source"

    ```bash
    git clone https://github.com/MSKazemi/kube_q
    cd kube_q
    pip install -e .
    kq --version
    ```

You should see something like:

```
kube-q 1.4.0
```

---

## Step 2 — Configure your backend URL

Save your connection settings once so you never need to type them on every run.

**Option A — from inside the REPL** (easiest, settings take effect immediately):

```
/config set url=https://kube-q.example.com
/config set api_key=your-api-key-here
```

**Option B — before launching** (edit the file directly):

```bash
mkdir -p ~/.kube-q
cat > ~/.kube-q/.env << 'EOF'
KUBE_Q_URL=https://kube-q.example.com
KUBE_Q_API_KEY=your-api-key-here
EOF
```

!!! tip "Multiple clusters?"
    Use per-directory `.env` files — see [Configuration → Multiple clusters](configuration.md#multiple-clusters).

---

## Step 3 — Start the REPL

```bash
kq
```

On first run, kube-q checks your backend is reachable, then drops you into the interactive REPL:

```
 _          _                                 
| | ___   _| |__   ___       __ _
| |/ / | | | '_ \ / _ \     / _` |
|   <| |_| | |_) |  __/    | (_| |
|_|\_\\__,_|_.__/ \___|     \__, |
                              |___/
kube-q v1.4.0  |  session d4e91c  |  Connected to kube-q.example.com

You>
```

---

## Step 4 — Ask your first question

Just type naturally. No special syntax.

```
You> show me all pods that are not running
```

```
You> what namespaces exist in this cluster?
```

```
You> why is the nginx deployment in staging failing?
```

kube-q streams the response in real time as tokens arrive from the backend.

---

## Step 5 — Try the key commands

### Set a namespace context

Instead of mentioning the namespace in every message, set it once:

```
You> /ns production
```

Now all your questions are scoped to `production` automatically.

### Attach a file

Reference a YAML manifest or log file by prefixing with `@`:

```
You> what is wrong with this deployment? @deployment.yaml
```

### Search past sessions

```
You> /search pod crash loop
```

Or from the terminal:

```bash
kq --search "oom killed"
```

### See token usage

```
You> /tokens
```

### Save the conversation

```
You> /save
```

Saves a Markdown file of the full session to your current directory.

---

## Step 6 — Exit

```
You> /quit
```

Or press ++ctrl+d++.

---

## What's next?

<div class="grid cards" markdown>

-   :material-cog:{ .lg .middle } **Configuration**

    `.env` files, environment variables, multi-cluster setups.

    [:octicons-arrow-right-24: Configuration](configuration.md)

-   :material-console:{ .lg .middle } **All slash commands**

    Complete reference for every `/command` available in the REPL.

    [:octicons-arrow-right-24: In-REPL Commands](commands.md)

-   :material-shield-check:{ .lg .middle } **Human-in-the-Loop**

    How kube-q handles destructive actions and asks for approval.

    [:octicons-arrow-right-24: HITL Guide](hitl.md)

-   :material-monitor:{ .lg .middle } **Browser Terminal**

    Run kube-q in any browser — no local install needed.

    [:octicons-arrow-right-24: Web UI](web-ui.md)

</div>
