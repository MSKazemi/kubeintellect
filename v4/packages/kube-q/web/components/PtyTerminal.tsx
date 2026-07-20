"use client";

/**
 * PtyTerminal.tsx — xterm.js terminal connected to the PTY WebSocket server.
 *
 * Pure byte relay: browser keystrokes → PTY stdin, PTY stdout → browser.
 * All readline, history, slash commands, markdown rendering are handled by
 * the Python `kq` process — nothing is reimplemented here.
 *
 * WebSocket path: /pty-ws (same host+port as the page — no extra port needed).
 * Requires the custom Next.js server:  npm run dev:pty
 */

import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";

// Dev: set NEXT_PUBLIC_PTY_PORT=3001 (separate pty-server.mjs port).
// Production: leave unset → connect to /pty-ws on the same origin (server.mjs).
const PTY_PORT = process.env.NEXT_PUBLIC_PTY_PORT ?? "";

function ptyWsUrl(cols: number, rows: number, token?: string): string {
  const params = new URLSearchParams({ cols: String(cols), rows: String(rows) });
  if (token) params.set("token", token);
  const qs = `?${params.toString()}`;
  if (typeof window === "undefined") {
    return PTY_PORT
      ? `ws://localhost:${PTY_PORT}${qs}`
      : `ws://localhost:3000/pty-ws${qs}`;
  }
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  if (PTY_PORT) {
    return `${proto}//${window.location.hostname}:${PTY_PORT}${qs}`;
  }
  return `${proto}//${window.location.host}/pty-ws${qs}`;
}

export type PtyStatus = "idle" | "connecting" | "connected" | "reconnecting" | "ended" | "error";

export interface PtyTerminalHandle {
  downloadBuffer(): void;
}

export interface PtyTerminalProps {
  /** Optional auth token sent as ?token=... on the websocket URL. */
  authToken?: string;
  /** Fired whenever the connection status changes. */
  onStatusChange?: (status: PtyStatus, detail?: string) => void;
}

const PtyTerminal = forwardRef<PtyTerminalHandle, PtyTerminalProps>(
  ({ authToken, onStatusChange }, ref) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<import("@xterm/xterm").Terminal | null>(null);
  const statusRef = useRef<PtyStatus>("idle");
  const setStatus = (s: PtyStatus, detail?: string) => {
    statusRef.current = s;
    onStatusChange?.(s, detail);
  };

  useImperativeHandle(ref, () => ({
    downloadBuffer() {
      const term = termRef.current;
      if (!term) return;

      const buf = term.buffer.active;
      const lines: string[] = [];
      for (let i = 0; i < buf.length; i++) {
        const line = buf.getLine(i);
        lines.push(line ? line.translateToString(true) : "");
      }

      // Trim leading/trailing blank lines
      let start = 0;
      while (start < lines.length && lines[start].trim() === "") start++;
      let end = lines.length - 1;
      while (end > start && lines[end].trim() === "") end--;
      const body = lines.slice(start, end + 1).join("\n");

      const ts = new Date().toISOString();
      const md = `# kube-q Session\n\n*Downloaded: ${ts}*\n\n\`\`\`\n${body}\n\`\`\`\n`;

      const blob = new Blob([md], { type: "text/markdown" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `kube-q-${ts.replace(/[:.]/g, "-")}.md`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 100);
    },
  }));

  useEffect(() => {
    if (!containerRef.current) return;
    const el = containerRef.current;
    let destroyed = false;
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    let term: import("@xterm/xterm").Terminal | null = null;
    let ro: ResizeObserver | null = null;
    const MAX_ATTEMPTS = 8;

    function connect() {
      if (destroyed || !term) return;

      if (attempt === 0) {
        setStatus("connecting");
        term.write("\x1b[2mConnecting to kq…\x1b[0m");
      } else {
        setStatus("reconnecting", `attempt ${attempt}/${MAX_ATTEMPTS}`);
        term.write(`\r\n\x1b[33m↻ Reconnecting (attempt ${attempt}/${MAX_ATTEMPTS})…\x1b[0m\r\n`);
      }

      const wsUrl = ptyWsUrl(term.cols, term.rows, authToken);
      ws = new WebSocket(wsUrl);
      ws.binaryType = "arraybuffer";

      ws.onopen = () => {
        if (destroyed || !term) return;
        attempt = 0;
        setStatus("connected");
        term.write("\r\x1b[K");
      };

      ws.onmessage = ({ data }) => {
        if (destroyed || !term) return;
        if (data instanceof ArrayBuffer) {
          term.write(new Uint8Array(data));
        } else {
          term.write(data as string);
        }
      };

      ws.onerror = () => {
        if (destroyed) return;
        setStatus("error", "websocket error");
      };

      ws.onclose = ({ code, reason }) => {
        if (destroyed || !term) return;

        // 1008 = policy violation (auth failure). Do not retry.
        if (code === 1008) {
          setStatus("error", reason || "authentication failed");
          term.write("\r\x1b[K");
          term.write(`\r\n\x1b[31m✗  ${reason || "Authentication failed."}\x1b[0m\r\n`);
          term.write("\x1b[2m  Check your PTY_AUTH_TOKEN and reload the page.\x1b[0m\r\n");
          return;
        }

        // Normal close — session ended cleanly.
        if (code === 1000) {
          setStatus("ended", reason || "session ended");
          term.write(`\r\n\x1b[2m[session ended: ${reason || "closed"}]\x1b[0m\r\n`);
          return;
        }

        // Unexpected close — retry with exponential backoff.
        attempt++;
        if (attempt > MAX_ATTEMPTS) {
          setStatus("error", "max reconnect attempts reached");
          term.write(
            `\r\n\x1b[31m✗  Connection lost (${reason || code}). ` +
              `Max reconnect attempts reached — reload the page.\x1b[0m\r\n`
          );
          return;
        }
        const delay = Math.min(1000 * Math.pow(2, attempt - 1), 15000);
        setStatus("reconnecting", `retry in ${Math.round(delay / 1000)}s`);
        term.write(
          `\r\n\x1b[33m⚠  Disconnected (${reason || code}). ` +
            `Reconnecting in ${Math.round(delay / 1000)}s…\x1b[0m\r\n`
        );
        reconnectTimer = setTimeout(connect, delay);
      };
    }

    async function init() {
      const { Terminal } = await import("@xterm/xterm");
      const { FitAddon } = await import("@xterm/addon-fit");
      if (destroyed) return;

      term = new Terminal({
        cursorBlink:  true,
        cursorStyle:  "block",
        theme: {
          background:          "#0d0d0d",
          foreground:          "#d4d4d4",
          cursor:              "#d4d4d4",
          selectionBackground: "#ffffff33",
        },
        fontFamily:   "var(--font-geist-mono), 'Cascadia Code', 'Fira Code', monospace",
        fontSize:     13,
        lineHeight:   1.4,
        scrollback:   10_000,
        allowProposedApi: true,
      });

      const fit = new FitAddon();
      term.loadAddon(fit);
      term.open(el);
      fit.fit();
      requestAnimationFrame(() => { if (!destroyed) fit.fit(); });

      termRef.current = term;

      ro = new ResizeObserver(() => {
        if (!term) return;
        fit.fit();
        if (ws?.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
        }
      });
      ro.observe(el);

      // Raw keystrokes → PTY
      term.onData(data => {
        if (ws?.readyState === WebSocket.OPEN) ws.send(data);
      });

      connect();
    }

    init().catch(err => {
      // xterm failed to load — show plain-text error in the container
      console.error("[PtyTerminal] init failed:", err);
      setStatus("error", String(err));
      el.innerHTML =
        `<pre style="color:#f88;padding:16px;font-family:monospace">` +
        `✗  Terminal failed to initialise:\n${err}\n\n` +
        `Check the browser console for details.</pre>`;
    });

    return () => {
      destroyed = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      ws?.close();
      termRef.current?.dispose();
      termRef.current = null;
      ro?.disconnect();
    };
  }, [authToken]);

  return (
    <div
      ref={containerRef}
      className="w-full h-full bg-[#0d0d0d]"
      style={{ padding: "4px" }}
    />
  );
});

PtyTerminal.displayName = "PtyTerminal";

export default PtyTerminal;
