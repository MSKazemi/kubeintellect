/**
 * pty-server.mjs — WebSocket server that spawns a kq PTY session per connection.
 *
 * Each WebSocket connection gets its own `kq` process running in a pseudo-terminal.
 * Raw bytes flow in both directions: browser keystrokes → PTY stdin, PTY stdout → browser.
 *
 * Usage:
 *   node pty-server.mjs [--port 3001] [--cmd kq] [--args "arg1,arg2"]
 *
 * Run alongside `next dev`:
 *   npm run dev:pty
 */

import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";
import { WebSocketServer } from "ws";
import pty from "node-pty";

// ── .env loader ───────────────────────────────────────────────────────────────

const __dirname = dirname(fileURLToPath(import.meta.url));

/**
 * Parse a .env file into a plain object.
 * Skips blank lines and comments (#). Strips surrounding quotes from values.
 * Returns {} if the file doesn't exist or can't be read.
 */
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

// Load KUBE_Q_* config from .env files (project root → ~/.kube-q/.env).
// Shell environment variables take precedence over both files.
const dotEnv = {
  ...parseEnvFile(join(__dirname, "..", ".env")),    // project root .env
  ...parseEnvFile(resolve(process.env.HOME ?? "/root", ".kube-q", ".env")),
};

// Merge: .env files supply defaults; shell env overrides them
const kqEnv = { ...dotEnv, ...process.env };

if (dotEnv.KUBE_Q_URL) console.log(`[pty] KUBE_Q_URL from .env: ${dotEnv.KUBE_Q_URL}`);

// ── Config ────────────────────────────────────────────────────────────────────

const PORT       = Number(process.env.PTY_PORT ?? 3001);
const CMD        = process.env.PTY_CMD         ?? "kq";
const ARGS       = process.env.PTY_ARGS ? process.env.PTY_ARGS.split(",") : [];
const AUTH_TOKEN = process.env.PTY_AUTH_TOKEN ?? kqEnv.PTY_AUTH_TOKEN ?? "";

if (AUTH_TOKEN) {
  console.log("[pty] auth: enabled (PTY_AUTH_TOKEN set)");
} else {
  console.log("[pty] auth: disabled (set PTY_AUTH_TOKEN to require a token)");
}

// ── HTTP server (WebSocket upgrade only) ──────────────────────────────────────

const server = createServer((req, res) => {
  if (req.url === "/health") {
    res.writeHead(200, { "Content-Type": "text/plain" });
    res.end("ok\n");
  } else {
    res.writeHead(404);
    res.end();
  }
});

const wss = new WebSocketServer({ server });

// ── Per-connection PTY lifecycle ──────────────────────────────────────────────

wss.on("connection", (ws, req) => {
  const remote = req.socket.remoteAddress ?? "unknown";
  console.log(`[pty] connect  ${remote}`);

  // Parse initial window size from query string: ?cols=120&rows=40
  const url    = new URL(req.url ?? "/", `http://localhost:${PORT}`);
  const cols   = Number(url.searchParams.get("cols") ?? 220);
  const rows   = Number(url.searchParams.get("rows") ?? 50);

  // Auth gate: if PTY_AUTH_TOKEN is set, require matching ?token=...
  if (AUTH_TOKEN) {
    const provided = url.searchParams.get("token") ?? "";
    if (provided !== AUTH_TOKEN) {
      console.warn(`[pty] auth reject ${remote} (token ${provided ? "mismatch" : "missing"})`);
      ws.close(1008, "authentication failed");
      return;
    }
  }

  // Spawn the kq process
  let proc;
  try {
    proc = pty.spawn(CMD, ARGS, {
      name: "xterm-256color",
      cols,
      rows,
      cwd:  process.env.HOME ?? "/tmp",
      env:  {
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

  // PTY output → WebSocket
  proc.onData(data => {
    if (ws.readyState === ws.OPEN) ws.send(data);
  });

  // Process exit → close socket
  proc.onExit(({ exitCode, signal }) => {
    console.log(`[pty] exit     pid=${proc.pid} code=${exitCode} signal=${signal}`);
    if (ws.readyState === ws.OPEN) ws.close(1000, "process exited");
  });

  // WebSocket messages → PTY stdin
  ws.on("message", msg => {
    try {
      const text = msg.toString("utf8");

      // Control message: JSON with "type"
      if (text.startsWith("{")) {
        const ctrl = JSON.parse(text);
        if (ctrl.type === "resize") {
          const c = Number(ctrl.cols ?? cols);
          const r = Number(ctrl.rows ?? rows);
          proc.resize(c, r);
        }
        return;
      }

      // Raw keystroke data
      proc.write(text);
    } catch {
      // ignore malformed messages
    }
  });

  // WebSocket close → kill PTY
  ws.on("close", () => {
    console.log(`[pty] disconnect ${remote} pid=${proc.pid}`);
    try { proc.kill(); } catch { /* already dead */ }
  });

  ws.on("error", err => {
    console.error(`[pty] ws error pid=${proc.pid}: ${err.message}`);
  });
});

// ── Start ─────────────────────────────────────────────────────────────────────

server.listen(PORT, () => {
  console.log(`[pty] server ready  ws://localhost:${PORT}`);
  console.log(`[pty] spawning      ${CMD} ${ARGS.join(" ")}`);
});
