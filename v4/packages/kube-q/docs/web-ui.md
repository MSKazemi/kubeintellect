# Web UI

kube-q includes a browser-based terminal that runs the full `kq` REPL in any browser. No client-side logic is duplicated — xterm.js renders bytes relayed from a real `kq` process running in a pseudo-terminal (PTY) on the server. All slash commands, streaming, HITL, session history, and file attachments work exactly as in the terminal.

```
Browser (xterm.js) ←→ WebSocket (/pty-ws) ←→ node-pty ←→ kq process
```

---

## Docker (recommended)

The Docker image bundles everything: the Python `kq` CLI, the Next.js frontend, and the PTY WebSocket server.

### Run

```bash
docker run -p 3000:3000 \
  -e KUBE_Q_URL=https://kube-q.example.com \
  -e KUBE_Q_API_KEY=your-key \
  ghcr.io/mskazemi/kube_q:latest
```

Open `http://localhost:3000`. Each browser tab gets its own independent `kq` process.

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `KUBE_Q_URL` | Yes | kube-q API base URL |
| `KUBE_Q_API_KEY` | When auth is enabled | API key for the backend |
| `KUBE_Q_MODEL` | No | Model name (default: `kubeintellect-v2`) |
| `KUBE_Q_USER_NAME` | No | Display name shown in the prompt |
| `PORT` | No | HTTP port (default: `3000`) |
| `KUBE_Q_SKIP_HEALTH_CHECK` | No | Set `true` to skip startup health check |
| `PTY_AUTH_TOKEN` | No | Require clients to supply this token on the WebSocket; when unset, auth is disabled |

### Build your own image

```bash
git clone https://github.com/MSKazemi/kube_q
cd kube_q
docker build -t kube-q-web .
docker run -p 3000:3000 -e KUBE_Q_URL=... kube-q-web
```

The Dockerfile is a multi-stage build: Node builder compiles Next.js, runtime stage installs `kube-q` from PyPI and copies the built app. No `.env` is baked into the image — inject everything at runtime.

---

## iframe embedding

The web terminal can be embedded in any parent page. The `NEXT_PUBLIC_BASE_PATH` variable relocates the app to a sub-path:

```bash
docker run -p 3000:3000 \
  -e KUBE_Q_URL=https://kube-q.example.com \
  -e KUBE_Q_API_KEY=your-key \
  -e NEXT_PUBLIC_BASE_PATH=/kq \
  ghcr.io/mskazemi/kube_q:latest
```

The app is then available at `http://host:3000/kq` and can be embedded:

```html
<iframe src="http://host:3000/kq" width="100%" height="600px"></iframe>
```

`Content-Security-Policy: frame-ancestors *` and `X-Frame-Options: ALLOWALL` are set on all responses, so embedding from any origin works by default.

---

## Development setup

Run Next.js and the PTY server locally (Node.js 20+ required, and `kq` must be installed):

```bash
cd web
npm install
npm run dev        # starts Next.js on :3000 and PTY server on :3001
```

Open `http://localhost:3000`.

To point the PTY at a different `kube-q` binary:

```bash
PTY_CMD=/path/to/kq npm run dev
```

### Production build (no Docker)

```bash
cd web
npm run build
npm run start      # Next.js + PTY WebSocket on the same port
```

`server.mjs` upgrades `/pty-ws` connections and handles them with node-pty. No second port needed.

---

## Download conversation

The toolbar has a **⬇ Download** button that exports the xterm.js scrollback buffer as a Markdown file directly to your browser — no server round-trip needed.

---

## Connection status & auto-reconnect

A coloured status dot in the toolbar reflects the live WebSocket state:

| Status | Meaning |
|---|---|
| `connecting…` | Opening the WebSocket for the first time |
| `connected` | Byte relay is live |
| `reconnecting — retry in Ns` | Unexpected close; counting down to the next attempt |
| `ended` | Clean close (WebSocket code 1000) — the `kq` process exited |
| `error` | Auth failure (1008) or max reconnect attempts reached |

Unexpected disconnects trigger an exponential-backoff reconnect loop (up to 8 attempts, 1s → 15s) with inline "Reconnecting in Ns…" messages in the terminal. A clean close or auth failure stops the loop.

---

## Auth token gate

To restrict who can spawn a `kq` process, set `PTY_AUTH_TOKEN` on the server:

```bash
docker run -p 3000:3000 \
  -e KUBE_Q_URL=https://kube-q.example.com \
  -e PTY_AUTH_TOKEN=$(openssl rand -hex 32) \
  ghcr.io/mskazemi/kube_q:latest
```

Clients must supply the matching token via the `?token=` query parameter on the WebSocket URL. The frontend has a **🔑 Token** toolbar button that prompts for the token and stores it in `sessionStorage` (per-tab, cleared when the tab closes). A wrong or missing token is rejected with WebSocket close code `1008`, and the terminal shows an "Authentication failed" message with the token prompt re-opened.

Leave `PTY_AUTH_TOKEN` unset for local development — the server logs `auth: disabled` and all connections are accepted.

---

## Per-connection isolation

Each WebSocket connection spawns a separate `kq` process in its own PTY. Sessions are completely isolated — one browser tab cannot see another's session. Closing the tab kills the `kq` process.
