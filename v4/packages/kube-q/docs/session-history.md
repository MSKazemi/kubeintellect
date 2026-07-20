# Session History

kube-q saves every conversation to a local SQLite database at `~/.kube-q/history.db`. Nothing is sent to or stored on the server — this is a local-only mirror. The database never affects your cluster.

---

## Listing sessions

```bash
kq --list
```

Shows the 20 most recent sessions with ID, title, message count, token total, and last-used time (non-interactive table, exits after printing).

---

## Resuming a session

### From the REPL — interactive picker

```
/sessions       (or /resume)
```

Opens a full-screen picker of the 20 most recent sessions. Use **↑/↓** to navigate, **Enter** to resume the highlighted session in place, **Esc** to cancel. Each row shows `updated · title · msg count · tokens · namespace · short id`.

On selection, kube-q:

1. Swaps the active conversation ID.
2. Hydrates the full message history from `~/.kube-q/history.db`.
3. Re-renders the stored transcript (user turns in green, assistant turns as markdown, separated by a rule showing the message count) so you see the whole conversation before continuing.
4. **Restores the kube context** that was active when the session was last used — so you're automatically pointed back at the same cluster. A dim `Context restored to …` line confirms it. Override any time with `/context <name>`.
5. Clears any pending HITL flag — you're ready to send the next message.

!!! tip "Picker shows the context"
    Each row in the picker and `kq --list` table now includes the session's kube context (`ctx=prod-cluster`) so you can tell at a glance which cluster a session was working against.

No restart is required. The picker renders identically in the web UI because the browser is a pure PTY relay to the `kq` process.

### From the shell — by session ID

```bash
kq --session-id <id>
```

Launches kube-q directly into the chosen session. The stored transcript is replayed at startup the same way it is in the REPL picker, and the stored kube context is restored onto `state.current_context` so the next message is routed to the same cluster. The session ID is shown in `kq --list` output. If you pass `--context <name>` explicitly on the command line, your flag wins over the stored value.

---

## Replaying the current session on demand

```
/history              # all messages
/history <N>          # last N messages
/history <X-Y>        # messages X through Y (1-indexed, inclusive)
/history #<N>         # just message #N
```

Useful when the transcript has scrolled off the top of the terminal, or when you want to quote a specific earlier exchange. Every line is prefixed with a `[#N]` marker so you can jump back by number:

```
/history 5            # review the last 5 turns
/history 1-4          # re-read the opening exchange
/history #7           # pull back just assistant message #7
```

Malformed or out-of-range specs print a usage hint — no state is changed. `/history` operates purely on the session already in memory; it does not hit the database.

---

## Full-text search

```bash
kq --search "deployment rollback"
kq --search "pods AND crash"
kq --search "oom killed OR crash loop"
```

Powered by SQLite FTS5. Results show session title, matched message excerpt with highlighted terms, and the session ID. Supports FTS5 boolean syntax:

| Syntax | Meaning |
|---|---|
| `pods crash` | both words anywhere |
| `pods AND crash` | both words (explicit) |
| `pods OR crash` | either word |
| `pods NOT staging` | pods but not staging |
| `"crash loop"` | exact phrase |

Inside the REPL: `/search <query>` works the same way.

---

## Conversation branching

```
/branch
```

Forks the current conversation at the current message count into a new independent session. The original session is never modified — you get a clean fork you can take in a different direction.

```
/branches        — list all forks of (and siblings of) this session
/title <text>    — rename the current session
```

Branches show up in `kq --list` as regular sessions. Search finds them automatically.

---

## Deleting a session

```
/forget
```

Deletes the current session from local history. Server-side state (if any) is not affected.

---

## Database details

| Property | Value |
|---|---|
| Path | `~/.kube-q/history.db` |
| Format | SQLite 3, WAL mode |
| Schema version | v3 (auto-migrates from v1 and v2) |
| What's stored | Session metadata, messages, token counts, FTS index |
| What's NOT stored | Cluster credentials, API keys, server state |

Schema migrations run automatically at startup — old databases from v1.0.0 are upgraded transparently.
