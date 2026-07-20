"""
sessions_ui.py — session picker, resume, and transcript-history rendering.

These are the read/replay surfaces of the REPL (``/sessions``, ``/resume``,
``/history``). Pulled out of the main loop so the loop stays focused on dispatch.
"""

import re

from prompt_toolkit.shortcuts import radiolist_dialog
from prompt_toolkit.styles import Style as PTStyle
from rich.markdown import Markdown
from rich.rule import Rule

from kube_q.cli import store
from kube_q.cli.renderer import _print_sessions_table, console
from kube_q.core.session import SessionState

# ── Interactive session picker ────────────────────────────────────────────────

_PICKER_STYLE = PTStyle.from_dict({
    "dialog":             "bg:#1e1e2e",
    "dialog frame.label": "bg:#1e1e2e #89b4fa bold",
    "dialog.body":        "bg:#1e1e2e #cdd6f4",
    "dialog shadow":      "bg:#11111b",
    "radio-selected":     "#a6e3a1 bold",
    "radio":              "#cdd6f4",
    "button":             "bg:#313244 #cdd6f4",
    "button.focused":     "bg:#89b4fa #1e1e2e bold",
})


def _format_session_row(s: dict) -> str:
    """One-line label for a session row in the picker."""
    title = s["title"] or "(untitled)"
    if len(title) > 40:
        title = title[:37] + "…"
    updated = (s["updated_at"] or "")[:16].replace("T", " ")
    msgs = s["message_count"]
    tok = s.get("total_tokens") or 0
    ns = s["namespace"] or "—"
    ctx = s.get("kube_context") or "—"
    sid = s["session_id"][:8]
    tok_str = f"{tok:,}t" if tok else "—"
    return (
        f"{updated}  {title:<40}  msgs={msgs:<3} {tok_str:<7}  "
        f"ns={ns:<12} ctx={ctx:<16} [{sid}]"
    )


def _pick_session_interactive(limit: int = 20) -> str | None:
    """Show an arrow-key picker of recent sessions; return session_id or None."""
    sessions = store.list_sessions(limit)
    if not sessions:
        console.print("[muted]No sessions found.[/muted]")
        return None

    values = [(s["session_id"], _format_session_row(s)) for s in sessions]
    try:
        result = radiolist_dialog(
            title="Resume a session",
            text="↑/↓ to navigate · Enter to resume · Esc to cancel",
            values=values,
            default=values[0][0],
            style=_PICKER_STYLE,
        ).run()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[warn]Picker unavailable ({exc}). Showing table instead.[/warn]")
        _print_sessions_table(sessions)
        return None
    return result


def _resume_session(
    state: SessionState,
    session_id: str,
    user_name: str = "You",
    agent_name: str = "kube-q",
) -> bool:
    """Swap state to `session_id`, hydrating messages from the local store
    and re-rendering the stored transcript (same as ``kq --session-id``).

    Returns True if the switch happened, False on no-op or failure.
    """
    if session_id == state.conversation_id:
        console.print("[muted]Already on this session — no change.[/muted]")
        return False
    stored = store.load_messages(session_id)
    meta = store.load_session_meta(session_id) or {}
    state.conversation_id = session_id
    state.messages = stored
    state.hitl_pending = False
    state.pending_action_id = None
    prior_ctx = state.current_context
    stored_ctx = meta.get("kube_context")
    if stored_ctx:
        state.current_context = stored_ctx
    console.print(f"[muted]Resumed session[/muted] [bold]{session_id[:8]}[/bold]")
    if stored_ctx and stored_ctx != prior_ctx:
        console.print(f"[muted]Context restored to[/muted] [bold]{stored_ctx}[/bold]")
    _replay_history(stored, user_name=user_name, agent_name=agent_name)
    return True


# ── History replay ────────────────────────────────────────────────────────────

# Matches `[context: foo=bar] ` prefixes that the REPL prepends to outgoing
# user turns (namespace + kube_context). Stripped for display only.
_CONTEXT_PREFIX_RE = re.compile(r"^(?:\[context:[^\]]*\]\s*)+")


def _render_message(msg: dict, index: int, user_name: str, agent_name: str) -> None:
    """Print a single stored message with a 1-indexed `[#N]` prefix."""
    role = msg.get("role")
    content = msg.get("content", "")
    marker = f"[muted]\\[#{index}][/muted] "
    if role == "user":
        display = _CONTEXT_PREFIX_RE.sub("", content)
        console.print(f"{marker}[user]{user_name}:[/user] {display}")
    elif role == "assistant":
        console.print(f"{marker}[agent]{agent_name}:[/agent]")
        console.print(Markdown(content))
    else:
        console.print(f"{marker}[muted]{role}:[/muted] {content}")


def _replay_history(
    messages: list[dict],
    user_name: str,
    agent_name: str,
) -> None:
    """Re-render stored messages so a resumed session shows its prior turns."""
    if not messages:
        return
    console.print(Rule(f"[muted]Resumed {len(messages)} messages[/muted]", style="dim"))
    for i, msg in enumerate(messages, start=1):
        _render_message(msg, i, user_name, agent_name)
    console.print(Rule(style="dim"))


def _parse_history_spec(spec: str, total: int) -> tuple[int, int] | None:
    """Parse `/history` arg → inclusive (start, end) 1-indexed slice.

    Accepts: ""/whitespace (all), "N" (last N), "X-Y" (range), "#N" (just N).
    Returns None when the spec is malformed or out of range.
    """
    spec = spec.strip()
    if not spec:
        return (1, total)
    if spec.startswith("#") or spec.startswith("@"):
        try:
            n = int(spec[1:])
        except ValueError:
            return None
        if not (1 <= n <= total):
            return None
        return (n, n)
    if "-" in spec:
        lo_s, _, hi_s = spec.partition("-")
        try:
            lo, hi = int(lo_s), int(hi_s)
        except ValueError:
            return None
        if lo < 1 or hi > total or lo > hi:
            return None
        return (lo, hi)
    try:
        n = int(spec)
    except ValueError:
        return None
    if n < 1:
        return None
    n = min(n, total)
    return (total - n + 1, total)


def _print_history(
    messages: list[dict],
    arg: str,
    user_name: str,
    agent_name: str,
) -> None:
    """Handle `/history [N | X-Y | #N]` — render the requested slice."""
    total = len(messages)
    if total == 0:
        console.print("[muted]No messages in this session yet.[/muted]")
        return
    window = _parse_history_spec(arg, total)
    if window is None:
        console.print(
            "[warn]Usage:[/warn] /history                 "
            "[muted]# all messages[/muted]\n"
            "        /history [bold]N[/bold]              "
            "[muted]# last N messages[/muted]\n"
            "        /history [bold]X-Y[/bold]          "
            "[muted]# messages X through Y (1-indexed)[/muted]\n"
            "        /history [bold]#N[/bold]            "
            "[muted]# just message #N[/muted]"
        )
        return
    lo, hi = window
    count = hi - lo + 1
    header = (
        f"[muted]Message #{lo} of {total}[/muted]" if count == 1
        else f"[muted]Messages {lo}–{hi} of {total}[/muted]"
    )
    console.print(Rule(header, style="dim"))
    for i in range(lo, hi + 1):
        _render_message(messages[i - 1], i, user_name, agent_name)
    console.print(Rule(style="dim"))
