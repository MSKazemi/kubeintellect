# kube-q Web Terminal

xterm.js terminal that runs `kq` in a pseudo-terminal (PTY) and relays bytes via WebSocket. Zero TypeScript logic — Python handles everything.

Requires Node.js 20+ and `kq` installed on the machine running the server.

---

## Running

```bash
cd web
npm install
npm run dev      # http://localhost:3000
```

That's it. `npm run dev` starts both the Next.js server and the PTY WebSocket server together.

---

## How it works

```
Browser (xterm.js)  ←→  WebSocket (/pty-ws)  ←→  pty-server.mjs  ←→  kq process (PTY)
```

- `pty-server.mjs` — spawns `kq` in a pseudo-terminal per WebSocket connection; sends raw bytes both ways
- `components/PtyTerminal.tsx` — ~150 lines; connects xterm.js to the WebSocket; handles resize
- `app/page.tsx` — 5 lines; renders `<PtyTerminal />`

All REPL logic (slash commands, streaming, HITL, history, file attachments) is handled by the Python `kq` binary. Nothing is reimplemented in TypeScript.

---

## Production

```bash
npm run build
npm run start     # starts Next.js + PTY WebSocket server on the same port
```

`server.mjs` upgrades `/pty-ws` WebSocket connections and handles them with the same PTY logic, so no second port is needed in production.

---

## Configuration

Add your backend settings to the project root `.env` file:

```bash
KUBE_Q_URL=http://localhost:8000
KUBE_Q_API_KEY=your-api-key   # if your server requires auth
```

The PTY server reads the project root `.env` and `~/.kube-q/.env` at startup and forwards all variables to the spawned `kq` process. Shell environment variables take precedence over both files.

Alternatively, set `KUBE_Q_URL` (and other `KUBE_Q_*` vars) in your shell before running `npm run dev`.

```bash
# Dev only — override the PTY WebSocket port (default: 3001)
NEXT_PUBLIC_PTY_PORT=3001
```

In production (`npm run start`) the WebSocket runs on the same port as Next.js — no env var needed.
