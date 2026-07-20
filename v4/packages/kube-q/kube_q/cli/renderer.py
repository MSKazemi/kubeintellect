"""
renderer.py — Display utilities: Rich console, ANSI helpers, logo, markdown rendering,
and side-channel event renderers for the CLI.
"""

import datetime
import sys

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

from kube_q.cli.theme import PLAN_ICONS, get_theme


def error_timestamp() -> str:
    """Return a dim Rich-markup prefix like '[dim][14:07:33][/dim] ' for error lines."""
    return f"[dim][{datetime.datetime.now().strftime('%H:%M:%S')}][/dim] "

# ── Rich console ──────────────────────────────────────────────────────────────
# Themed once here (see kube_q.cli.theme). Tests and other modules import this
# singleton, so the theme applies everywhere markup is printed.

console = Console(highlight=False, theme=get_theme())

# ── ANSI colour helpers (used for input() prompts only) ──────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
CYAN   = "\033[36m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"


def c(text: str, *codes: str) -> str:
    """Wrap text in ANSI escape codes (no-op if stdout is not a TTY)."""
    if not sys.stdout.isatty():
        return text
    return "".join(codes) + text + RESET


# ── Logo ──────────────────────────────────────────────────────────────────────

_DEFAULT_LOGO_ART = (
    "\033[1;36m    __ __      __         ____      __       ____          __ \033[0m\n"
    "\033[1;36m   / //_/_  __/ /_  ___  /  _/___  / /____  / / /__  _____/ /_\033[0m\n"
    "\033[1;36m  / ,< / / / / __ \\/ _ \\ / // __ \\/ __/ _ \\/ / / _ \\/ ___/ __/\033[0m\n"
    "\033[1;36m / /| / /_/ / /_/ /  __// // / / / /_/  __/ / /  __/ /__/ /_  \033[0m\n"
    "\033[1;36m/_/ |_\\__,_/_.___/\\___/___/_/ /_/\\__/\\___/_/_/\\___/\\___/\\__/  \033[0m"
)
_DEFAULT_TAGLINE = "Your AI co-pilot for Kubernetes."

# Small watermark shown below a custom logo when KUBE_Q_LOGO is set.
_KUBE_Q_WATERMARK = "\033[2m  powered by kube-q\033[0m"

_custom_logo: str | None = None
_custom_tagline: str | None = None


def set_custom_logo(text: str | None) -> None:
    """Set a custom logo block (replaces the ASCII art).  Use \\n for newlines."""
    global _custom_logo
    _custom_logo = text.replace("\\n", "\n") if text else None


def set_custom_tagline(text: str | None) -> None:
    """Set a custom tagline / copyright line."""
    global _custom_tagline
    _custom_tagline = text


def _print_logo(connected: bool = True) -> None:
    if not sys.stdout.isatty():
        return
    print()
    if _custom_logo:
        # Big custom logo + small kube-q watermark
        print(_custom_logo)
        tagline = _custom_tagline or _DEFAULT_TAGLINE
        print(f"\033[2m  {tagline}\033[0m")
        print(_KUBE_Q_WATERMARK)
    else:
        # Default kube-q ASCII art
        colour = "\033[1;36m" if connected else "\033[1;31m"
        art = _DEFAULT_LOGO_ART.replace("\033[1;36m", colour)
        tagline = _custom_tagline or _DEFAULT_TAGLINE
        print(art)
        print(f"\033[2m   {tagline}\033[0m")
    print()


def _print_not_connected_panel(url: str, reason: str) -> None:
    """Show an actionable panel when the backend is unreachable."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    console.print(Panel(
        f"[red bold]✗ Cannot reach:[/red bold] {url}\n"
        f"[dim]  Time:   {ts}[/dim]\n"
        f"[dim]  Reason: {reason}[/dim]\n\n"
        "[bold]To configure the backend URL:[/bold]\n"
        "  [yellow]kq --url https://api.kubeintellect.com[/yellow]"
        "          One-time launch flag\n"
        "  [yellow]export KUBE_Q_URL=https://api.kubeintellect.com[/yellow]"
        "   Current shell session\n"
        "  [yellow]/config set url=https://api.kubeintellect.com[/yellow]"
        "   Persist permanently  [dim](takes effect immediately)[/dim]\n\n"
        "[dim]These commands work offline: "
        "/help  /sessions  /save  /state  /tokens  /search  /branch[/dim]",
        title="[red]Backend not reachable[/red]",
        border_style="red",
    ))


# ── Output format ─────────────────────────────────────────────────────────────

_plain_output: bool = False


def set_output_plain(plain: bool) -> None:
    """Switch between rich markdown rendering (default) and plain text output."""
    global _plain_output
    _plain_output = plain


# ── Response rendering ────────────────────────────────────────────────────────

# Maximum number of lines before auto-paging, regardless of actual terminal height.
_PAGER_LINE_THRESHOLD = 40


def _should_use_pager(text: str) -> bool:
    if not sys.stdout.isatty():
        return False
    terminal_height = console.height or 24
    threshold = max(min(terminal_height - 4, _PAGER_LINE_THRESHOLD), 10)
    return text.count("\n") > threshold


def print_response(text: str) -> None:
    """Render assistant response, paging long output. Respects --output plain."""
    if _plain_output:
        print(text)
        return
    md = Markdown(text)
    if _should_use_pager(text):
        with console.pager(styles=False):
            console.print(md)
    else:
        console.print()
        console.print(md)
        console.print()


# ── Side-channel event renderers ──────────────────────────────────────────────

def render_status(event: dict, live: Live, first_token: bool) -> None:
    """Render a ``status`` side-channel event.

    While the spinner is still visible (no tokens yet), replace the spinner
    text with the new status message.  After the first token has arrived the
    spinner is gone, so fall back to printing a dim ephemeral line above the
    live markdown area.
    """
    msg = event.get("message") or event.get("phase") or ""
    if not msg:
        return
    if first_token:
        live.update(Spinner("dots", text=Text.assemble((" ", ""), (msg, "dim cyan"))))
    else:
        console.print(f"[dim]⚙ {msg}[/dim]")


def render_tool_call(event: dict) -> None:
    """Render a ``tool_call`` side-channel event above the live area."""
    tool = event.get("tool", "")
    msg = event.get("message", "")
    if tool and msg:
        console.print(f"[dim cyan]⚙ {tool}[/dim cyan][dim] → {msg}[/dim]")
    elif tool:
        console.print(f"[dim cyan]⚙ {tool}[/dim cyan]")
    elif msg:
        console.print(f"[dim]⚙ {msg}[/dim]")


def render_error_event(event: dict) -> None:
    """Render an ``error`` side-channel event."""
    console.print(
        f"{error_timestamp()}[red]✗ {event.get('message', str(event))}[/red]"
    )


# Kept as an alias for backwards compatibility with external callers/tests.
_PLAN_STATUS_ICON = PLAN_ICONS


def plan_panel(event: dict) -> Panel | None:
    """Build the Investigation Plan panel for a ``plan`` event.

    Returned as a renderable (not printed) so it can live inside a Rich ``Live``
    group and update its step icons in real time as the stream progresses.
    Returns ``None`` when there are no steps.
    """
    steps = event.get("steps") or []
    if not steps:
        return None
    parts: list[str] = []
    for i, s in enumerate(steps, 1):
        if isinstance(s, dict):
            desc = s.get("description", "")
            status = s.get("status", "pending")
        else:
            desc = str(s)
            status = "pending"
        icon = PLAN_ICONS.get(status, PLAN_ICONS["pending"])
        parts.append(f"  {icon} [accent]{i}.[/accent] {desc}")
    return Panel(
        "\n".join(parts),
        title="[plan.title]Investigation Plan[/plan.title]",
        border_style="border",
        expand=False,
        padding=(0, 1),
    )


def render_plan(event: dict) -> None:
    """Render a ``plan`` side-channel event as a numbered step list with status icons."""
    panel = plan_panel(event)
    if panel is not None:
        console.print(panel)


def render_status_footer(
    *,
    kube_context: str | None,
    namespace: str | None,
    total_tokens: int,
    cost: str | None,
) -> None:
    """Print the compact per-turn status footer: ``[ctx · ns · N tok · $cost]``.

    Pieces that are absent are omitted so the line stays terse. Nothing is
    printed when there's nothing meaningful to show.
    """
    parts: list[str] = []
    if kube_context:
        parts.append(f"[footer.key]ctx[/footer.key] {kube_context}")
    if namespace:
        parts.append(f"[footer.key]ns[/footer.key] {namespace}")
    if total_tokens:
        parts.append(f"{total_tokens:,} tok")
    if cost:
        parts.append(cost)
    if not parts:
        return
    console.print(f"[footer]  {'  ·  '.join(parts)}[/footer]")


def render_hitl_panel(command: str | None = None) -> None:
    """Show the pending write-action approval panel.

    ``command`` is the proposed command/action text when the server provides it;
    otherwise a generic approval prompt is shown.
    """
    body = "[warn]This action needs your approval before it runs.[/warn]\n"
    if command:
        body += f"\n[muted]Proposed:[/muted]\n  [bold]{command}[/bold]\n"
    body += (
        "\n[accent]/approve[/accent] to run it"
        "    [accent]/deny[/accent] to cancel"
    )
    console.print(Panel(
        body,
        title="[warn]Approval required[/warn]",
        border_style="warn",
        expand=False,
        padding=(0, 1),
    ))


# ── Help panel ────────────────────────────────────────────────────────────────

def format_search_results(results: list[dict]) -> None:
    """Print a Rich table of FTS5 search results."""
    from rich.table import Table

    if not results:
        console.print("[dim]No results found.[/dim]")
        return

    table = Table(
        "Session", "Title", "Updated", "Msgs", "Match",
        title="[bold cyan]Search Results[/bold cyan]",
        border_style="dim cyan",
        show_lines=True,
    )
    for r in results:
        snippet = r.get("snippet") or ""
        snippet = snippet.replace(">>>", "[bold yellow]").replace("<<<", "[/bold yellow]")
        title = r.get("title") or "[dim](untitled)[/dim]"
        updated = (r.get("updated_at") or "")[:16].replace("T", " ")
        table.add_row(
            (r.get("session_id") or "")[:8],
            title,
            updated,
            str(r.get("message_count", 0)),
            snippet,
        )
    console.print(table)


def format_branches(branches: list[dict], current_id: str) -> None:
    """Print a Rich table of branched sessions."""
    from rich.table import Table

    if not branches:
        console.print("[dim]No branches of this session.[/dim]")
        return

    table = Table(
        "", "Session", "Title", "Branched at", "Msgs", "Updated",
        title="[bold cyan]Branches[/bold cyan]",
        border_style="dim cyan",
        show_lines=False,
    )
    for b in branches:
        marker = "[bold cyan]→[/bold cyan]" if b["session_id"] == current_id else ""
        title = b.get("title") or "[dim](untitled)[/dim]"
        updated = (b.get("updated_at") or "")[:16].replace("T", " ")
        bp = str(b.get("branch_point") or "—")
        table.add_row(
            marker,
            (b.get("session_id") or "")[:8],
            title,
            bp,
            str(b.get("message_count", 0)),
            updated,
        )
    console.print(table)


def _print_sessions_table(sessions: list[dict]) -> None:
    """Render a Rich table of sessions."""
    from rich.table import Table

    if not sessions:
        console.print("[dim]No sessions found.[/dim]")
        return

    table = Table(
        "Session ID", "Title", "Messages", "Tokens", "Namespace", "Context", "Updated",
        title="[bold cyan]Recent Sessions[/bold cyan]",
        border_style="dim cyan",
        show_lines=False,
    )
    for s in sessions:
        title = s["title"] or "[dim](untitled)[/dim]"
        ns = s["namespace"] or "[dim]—[/dim]"
        ctx = s.get("kube_context") or "[dim]—[/dim]"
        updated = s["updated_at"][:19].replace("T", " ") if s["updated_at"] else "—"
        total_tok = s.get("total_tokens", 0)
        tok_str = f"{total_tok:,}" if total_tok else "[dim]—[/dim]"
        table.add_row(
            s["session_id"][:36],
            title,
            str(s["message_count"]),
            tok_str,
            ns,
            ctx,
            updated,
        )
    console.print(table)


def _print_token_panel(
    session_id: str,
    override_prompt: float | None = None,
    override_completion: float | None = None,
) -> None:
    """Print a Rich panel showing token usage and estimated cost for a session."""
    from kube_q.cli.store import get_last_usage, get_session_tokens
    from kube_q.core import costs

    tok = get_session_tokens(session_id)
    last = get_last_usage(session_id)

    model = last.get("model") if last else None
    session_cost = costs.estimate_cost(
        model,
        tok["total_prompt_tokens"],
        tok["total_completion_tokens"],
        override_prompt,
        override_completion,
    )

    body = (
        f"  [bold]This session:[/bold]\n"
        f"    Prompt:     {tok['total_prompt_tokens']:,} tokens\n"
        f"    Completion: {tok['total_completion_tokens']:,} tokens\n"
        f"    Total:      {tok['total_tokens']:,} tokens\n"
        f"    Requests:   {tok['request_count']}\n"
        f"    Est. cost:  {costs.format_cost(session_cost)}"
    )

    if last:
        lp = last["prompt_tokens"]
        lc = last["completion_tokens"]
        last_cost = costs.estimate_cost(
            last.get("model"), lp, lc, override_prompt, override_completion
        )
        body += (
            f"\n\n  [bold]Last response:[/bold]\n"
            f"    {costs.format_tokens(lp, lc)} ({costs.format_cost(last_cost)})"
        )

    console.print(Panel(
        body,
        title="[bold cyan]Token Usage[/bold cyan]",
        border_style="dim cyan",
        expand=False,
        padding=(0, 1),
    ))


def _fmt_help() -> None:
    """Print the full help overview.

    Back-compat shim: the help content now lives in :mod:`kube_q.cli.help_text`.
    Imported lazily to avoid a circular import (help_text imports this module's
    ``console``).
    """
    from kube_q.cli.help_text import render_help
    render_help("")
