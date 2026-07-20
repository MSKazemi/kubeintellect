"use client";
import { useEffect, useRef, useState } from "react";
import PtyTerminal, {
  type PtyStatus,
  type PtyTerminalHandle,
} from "../components/PtyTerminal";

const TOKEN_KEY = "kq_pty_token";

function statusColor(s: PtyStatus): string {
  switch (s) {
    case "connected":    return "#00e676";
    case "connecting":   return "#ffb74d";
    case "reconnecting": return "#ffb74d";
    case "error":        return "#ff5252";
    case "ended":        return "#888";
    default:             return "#666";
  }
}

function statusLabel(s: PtyStatus, detail?: string): string {
  const base = {
    idle:         "idle",
    connecting:   "connecting…",
    connected:    "connected",
    reconnecting: "reconnecting",
    ended:        "session ended",
    error:        "error",
  }[s];
  return detail ? `${base} — ${detail}` : base;
}

export default function Home() {
  const termRef = useRef<PtyTerminalHandle>(null);
  const [token, setToken] = useState<string | undefined>(undefined);
  const [tokenReady, setTokenReady] = useState(false);
  const [status, setStatus] = useState<PtyStatus>("idle");
  const [statusDetail, setStatusDetail] = useState<string | undefined>();
  const [showTokenPrompt, setShowTokenPrompt] = useState(false);
  const [tokenDraft, setTokenDraft] = useState("");

  // Load token from sessionStorage on mount (client-only).
  useEffect(() => {
    const saved = typeof window !== "undefined"
      ? window.sessionStorage.getItem(TOKEN_KEY) ?? ""
      : "";
    setToken(saved || undefined);
    setTokenReady(true);
  }, []);

  // If the PTY rejected us for auth, prompt for a token.
  useEffect(() => {
    if (status === "error" && /authentication/i.test(statusDetail ?? "")) {
      setShowTokenPrompt(true);
    }
  }, [status, statusDetail]);

  const saveTokenAndReload = () => {
    if (typeof window === "undefined") return;
    window.sessionStorage.setItem(TOKEN_KEY, tokenDraft);
    window.location.reload();
  };

  const clearToken = () => {
    if (typeof window === "undefined") return;
    window.sessionStorage.removeItem(TOKEN_KEY);
    window.location.reload();
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", width: "100%", height: "100vh" }}>
      {/* Toolbar */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "4px 8px",
          background: "#0d0d0d",
          borderBottom: "1px solid #1e1e1e",
          flexShrink: 0,
          gap: 8,
        }}
      >
        {/* Status badge */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            fontFamily: "var(--font-geist-mono), 'Cascadia Code', 'Fira Code', monospace",
            fontSize: "12px",
            color: "#888",
          }}
        >
          <span
            aria-label={`status ${status}`}
            style={{
              width: 8,
              height: 8,
              borderRadius: "50%",
              background: statusColor(status),
              display: "inline-block",
              boxShadow: `0 0 4px ${statusColor(status)}`,
            }}
          />
          <span>{statusLabel(status, statusDetail)}</span>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <button
            onClick={() => {
              setTokenDraft(token ?? "");
              setShowTokenPrompt(true);
            }}
            style={{
              background: "transparent",
              border: "1px solid #555",
              color: "#bbb",
              fontFamily: "var(--font-geist-mono), 'Cascadia Code', 'Fira Code', monospace",
              fontSize: "12px",
              padding: "3px 10px",
              cursor: "pointer",
              borderRadius: "3px",
              letterSpacing: "0.03em",
            }}
            title="Set PTY auth token"
          >
            🔑 Token
          </button>

          {/* TODO Phase 2: replace buffer scrape with GET /conversations/{session_id}/export
              from the FastAPI backend (see kube_q/cli/repl.py _save_conversation) once the
              backend exposes a session ID to the frontend. */}
          <button
            onClick={() => termRef.current?.downloadBuffer()}
            style={{
              background: "transparent",
              border: "1px solid #00e676",
              color: "#00e676",
              fontFamily: "var(--font-geist-mono), 'Cascadia Code', 'Fira Code', monospace",
              fontSize: "12px",
              padding: "3px 10px",
              cursor: "pointer",
              borderRadius: "3px",
              letterSpacing: "0.03em",
            }}
            onMouseEnter={e => {
              (e.currentTarget as HTMLButtonElement).style.background = "#00e67622";
            }}
            onMouseLeave={e => {
              (e.currentTarget as HTMLButtonElement).style.background = "transparent";
            }}
          >
            ⬇ Download
          </button>
        </div>
      </div>

      {/* Token modal */}
      {showTokenPrompt && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.7)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 50,
          }}
        >
          <div
            style={{
              background: "#1a1a1a",
              border: "1px solid #333",
              borderRadius: 6,
              padding: 20,
              minWidth: 360,
              fontFamily: "var(--font-geist-mono), 'Cascadia Code', 'Fira Code', monospace",
              color: "#d4d4d4",
            }}
          >
            <div style={{ fontSize: 14, marginBottom: 8, color: "#00e676" }}>
              PTY auth token
            </div>
            <div style={{ fontSize: 12, color: "#888", marginBottom: 12 }}>
              Enter the token that matches <code>PTY_AUTH_TOKEN</code> on the server.
              Leave blank if auth is disabled.
            </div>
            <input
              type="password"
              value={tokenDraft}
              onChange={e => setTokenDraft(e.target.value)}
              placeholder="token"
              autoFocus
              onKeyDown={e => {
                if (e.key === "Enter") saveTokenAndReload();
                if (e.key === "Escape") setShowTokenPrompt(false);
              }}
              style={{
                width: "100%",
                padding: "6px 8px",
                background: "#0d0d0d",
                border: "1px solid #444",
                borderRadius: 3,
                color: "#d4d4d4",
                fontFamily: "inherit",
                fontSize: 13,
                marginBottom: 12,
              }}
            />
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
              <button
                onClick={() => setShowTokenPrompt(false)}
                style={{
                  background: "transparent",
                  border: "1px solid #555",
                  color: "#bbb",
                  padding: "4px 12px",
                  borderRadius: 3,
                  fontSize: 12,
                  cursor: "pointer",
                  fontFamily: "inherit",
                }}
              >
                cancel
              </button>
              {token && (
                <button
                  onClick={clearToken}
                  style={{
                    background: "transparent",
                    border: "1px solid #ff5252",
                    color: "#ff5252",
                    padding: "4px 12px",
                    borderRadius: 3,
                    fontSize: 12,
                    cursor: "pointer",
                    fontFamily: "inherit",
                  }}
                >
                  clear
                </button>
              )}
              <button
                onClick={saveTokenAndReload}
                style={{
                  background: "#00e67622",
                  border: "1px solid #00e676",
                  color: "#00e676",
                  padding: "4px 12px",
                  borderRadius: 3,
                  fontSize: 12,
                  cursor: "pointer",
                  fontFamily: "inherit",
                }}
              >
                save & reload
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Terminal — takes remaining height */}
      <div style={{ flex: 1, minHeight: 0, overflow: "hidden", position: "relative" }}>
        {tokenReady && (
          <PtyTerminal
            ref={termRef}
            authToken={token}
            onStatusChange={(s, detail) => {
              setStatus(s);
              setStatusDetail(detail);
            }}
          />
        )}
      </div>
    </div>
  );
}
