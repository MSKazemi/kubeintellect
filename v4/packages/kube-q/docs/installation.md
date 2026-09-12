# Installation

Requires **Python 3.12+**.

---

## pip (recommended)

```bash
pip install kube-q
```

Installs the `kq` command globally. Use a virtual environment or `pipx` to keep it isolated:

```bash
pipx install kube-q
```

---

## Snap (Linux)

```bash
sudo snap install kubeintellect
sudo snap connect kubeintellect:dot-kube    # read ~/.kube (kubeconfig)
sudo snap connect kubeintellect:dot-kube-q  # read/write ~/.kube-q (config, sessions)
sudo snap alias kubeintellect.kq kq         # optional: the short command name
```

Bundles its own Python, so it does not need one on the host. The two `snap connect`
steps are required and are not auto-connected: the snap runs under **strict**
confinement, and snapd's `home` interface deliberately excludes top-level
dot-directories — which is where both `~/.kube` and `~/.kube-q` live. Verify with
`snap connections kubeintellect`.

---

## Homebrew

```bash
brew tap MSKazemi/kube-q
brew install kube-q
```

---

## From source

```bash
git clone https://github.com/MSKazemi/kubeintellect.git
cd kubeintellect/v4/packages/kube-q
pip install -e .
```

Install with dev dependencies:

```bash
pip install -e ".[dev]"
```

---

## Docker (web terminal)

The Docker image bundles the full `kq` CLI and a browser-based terminal (Next.js + xterm.js). No local Python or Node.js install needed.

```bash
docker pull ghcr.io/mskazemi/kube_q:latest

docker run -p 3000:3000 \
  -e KUBE_Q_URL=https://kube-q.example.com \
  -e KUBE_Q_API_KEY=your-key \
  ghcr.io/mskazemi/kube_q:latest
```

Open `http://localhost:3000` to get a full `kq` terminal in the browser.

See [Web UI](web-ui.md) for the complete Docker setup guide.

---

## Verify

```bash
kq --version
```

---

## Dependencies

kube-q installs only lightweight, pure-Python dependencies:

| Package | Purpose |
|---|---|
| `httpx` | HTTP client (streaming SSE, TLS, proxy support) |
| `rich` | Terminal rendering (markdown, syntax highlighting, tables) |
| `prompt_toolkit` | REPL input (Tab completion, history, multi-line) |
| `pygments` | Syntax highlighting for code blocks |
| `pydantic` | Event model validation |
