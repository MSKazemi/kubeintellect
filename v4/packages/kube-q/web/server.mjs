/**
 * server.mjs — Production server: Next.js HTTP + PTY WebSocket on one port.
 *
 * WebSocket upgrades to /pty-ws are handled as PTY sessions (spawns `kq`).
 * All other requests are forwarded to the Next.js request handler.
 *
 * Usage:
 *   NODE_ENV=production node server.mjs
 *
 * Dev uses separate ports via `npm run dev:pty` (Next.js :3000, PTY :3001).
 */

import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";
import next from "next";
import { WebSocketServer } from "ws";
import pty from "node-pty";

// ── .env loader ───────────────────────────────────────────────────────────────

const __dirname = dirname(fileURLToPath(import.meta.url));

function parseEnvFile(filePath) {
  try {
    const text = readFileSync(filePath, "utf8");
    const out = {};
    for (const line of text.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) continue;
      const eq = trimmed.indexOf("=");
      if (eq < 1) continue;
      const key = trimmed.slice(0, eq).trim();
      let val = trimmed.slice(eq + 1).trim();
      if (
        (val.startsWith('"') && val.endsWith('"')) ||
        (val.startsWith("'") && val.endsWith("'"))
      ) {
        val = val.slice(1, -1);
      }
      out[key] = val;
    }
    return out;
  } catch {
    return {};
  }
}

const dotEnv = {
  ...parseEnvFile(join(__dirname, "..", ".env")),    // project root .env
  ...parseEnvFile(resolve(process.env.HOME ?? "/root", ".kube-q", ".env")),
};

const kqEnv = { ...dotEnv, ...process.env };

if (dotEnv.KUBE_Q_URL) console.log(`[server] KUBE_Q_URL from .env: ${dotEnv.KUBE_Q_URL}`);

// ── Config ────────────────────────────────────────────────────────────────────

const PORT       = Number(process.env.PORT   ?? 3000);
const CMD        = process.env.PTY_CMD       ?? "kq";
const ARGS       = process.env.PTY_ARGS ? process.env.PTY_ARGS.split(",") : [];
const AUTH_TOKEN = process.env.PTY_AUTH_TOKEN ?? kqEnv.PTY_AUTH_TOKEN ?? "";

if (AUTH_TOKEN) {
  console.log("[server] pty auth: enabled (PTY_AUTH_TOKEN set)");
} else {
  console.log("[server] pty auth: disabled (set PTY_AUTH_TOKEN to require a token)");
}

// ── Next.js ───────────────────────────────────────────────────────────────────

const dev    = process.env.NODE_ENV !== "production";
const app    = next({ dev });
const handle = app.getRequestHandler();

await app.prepare();

// ── HTTP + WebSocket server ───────────────────────────────────────────────────

const server = createServer((req, res) => handle(req, res));

const wss = new WebSocketServer({ noServer: true });

// Route WebSocket upgrades: /pty-ws → PTY, anything else → destroy
server.on("upgrade", (req, socket, head) => {
  if (req.url?.startsWith("/pty-ws")) {
    wss.handleUpgrade(req, socket, head, ws => wss.emit("connection", ws, req));
  } else {
    socket.destroy();
  }
});

// ── Per-connection PTY lifecycle ──────────────────────────────────────────────

wss.on("connection", (ws, req) => {
  const remote = req.socket.remoteAddress ?? "unknown";
  console.log(`[pty] connect  ${remote}`);

  const url  = new URL(req.url ?? "/pty-ws", `http://localhost:${PORT}`);
  const cols = Number(url.searchParams.get("cols") ?? 220);
  const rows = Number(url.searchParams.get("rows") ?? 50);

  // Auth gate: if PTY_AUTH_TOKEN is set, require matching ?token=...
  if (AUTH_TOKEN) {
    const provided = url.searchParams.get("token") ?? "";
    if (provided !== AUTH_TOKEN) {
      console.warn(`[pty] auth reject ${remote} (token ${provided ? "mismatch" : "missing"})`);
      ws.close(1008, "authentication failed");
      return;
    }
  }

  let proc;
  try {
    proc = pty.spawn(CMD, ARGS, {
      name: "xterm-256color",
      cols,
      rows,
      cwd: process.env.HOME ?? "/tmp",
      env: {
        ...kqEnv,
        TERM:      "xterm-256color",
        COLORTERM: "truecolor",
      },
    });
  } catch (err) {
    console.error(`[pty] spawn failed: ${err.message}`);
    ws.close(1011, `spawn failed: ${err.message}`);
    return;
  }

  console.log(`[pty] spawned  pid=${proc.pid} cmd=${CMD} cols=${cols} rows=${rows}`);

  proc.onData(data => { if (ws.readyState === ws.OPEN) ws.send(data); });

  proc.onExit(({ exitCode, signal }) => {
    console.log(`[pty] exit     pid=${proc.pid} code=${exitCode} signal=${signal}`);
    if (ws.readyState === ws.OPEN) ws.close(1000, "process exited");
  });

  ws.on("message", msg => {
    try {
      const text = msg.toString("utf8");
      if (text.startsWith("{")) {
        const ctrl = JSON.parse(text);
        if (ctrl.type === "resize") proc.resize(Number(ctrl.cols ?? cols), Number(ctrl.rows ?? rows));
        return;
      }
      proc.write(text);
    } catch { /* ignore malformed */ }
  });

  ws.on("close", () => {
    console.log(`[pty] disconnect ${remote} pid=${proc.pid}`);
    try { proc.kill(); } catch { /* already dead */ }
  });

  ws.on("error", err => console.error(`[pty] ws error pid=${proc.pid}: ${err.message}`));
});

// ── Start ─────────────────────────────────────────────────────────────────────

server.listen(PORT, () => {
  console.log(`[server] ready  http://localhost:${PORT}`);
  console.log(`[pty]    ws://localhost:${PORT}/pty-ws`);
});
