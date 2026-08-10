"""
repl.py — prompt_toolkit REPL loop and slash command dispatch for the kube_q CLI.
"""


import datetime
import difflib
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from prompt_toolkit.formatted_text import FormattedText
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

from kube_q import costs, plugins, store
from kube_q.cli import config_cmd as _config_cmd
from kube_q.cli.help_text import render_help

# Re-exported so existing tests that patch ``repl._HISTORY_FILE`` keep working.
from kube_q.cli.prompt import (
    _HISTORY_FILE,  # noqa: F401
    _SLASH_COMMANDS,
    _list_profiles,
    _make_prompt_session,
)
from kube_q.cli.renderer import (
    _print_logo,
    _print_not_connected_panel,
    _print_sessions_table,
    _print_token_panel,
    console,
    error_timestamp,
    format_branches,
    format_search_results,
    render_hitl_panel,
    render_status_footer,
)
from kube_q.cli.sessions_ui import (
    _pick_session_interactive,
    _print_history,
    _replay_history,
    _resume_session,
)
from kube_q.core.kubeconfig import list_contexts
from kube_q.core.session import SessionState
from kube_q.core.session import load_or_create_user_id as _load_or_create_user_id
from kube_q.core.session import resolve_attachments as _resolve_attachments
from kube_q.core.transport import fetch_namespaces
from kube_q.transport import check_health, non_stream_query, stream_query

# ── REPL configuration dataclass ─────────────────────────────────────────────

@dataclass
class ReplConfig:
    """All configuration for a single run_repl() invocation."""
    url: str = "https://api.kubeintellect.com"
    stream: bool = True
    initial_conversation_id: str | None = None
    initial_session_id: str | None = None
    show_header: bool = True
    initial_messages: list[dict] = field(default_factory=list)
    user_id: str | None = None
    quiet: bool = False
    api_key: str | None = None
    ca_cert: str | None = None
    query_timeout: float = 120.0
    health_timeout: float = 5.0
    namespace_timeout: float = 3.0
    startup_retry_timeout: int = 0
    startup_retry_interval: int = 5
    skip_health_check: bool = False
    user_name: str = "You"
    agent_name: str = "kube-q"
    model: str = "kubeintellect-v2"
    cost_prompt_override: float | None = None
    cost_completion_override: float | None = None
    # Backend routing
    chat_path: str = "/v1/chat/completions"
    auth_scheme: str = "bearer"
    health_path: str | None = "/healthz"
    backend_label: str = "kube-q"
    # Kubernetes context
    kube_context: str | None = None
    # Active profile name (for display only)
    profile: str | None = None
    # Skip HITL approval prompts for all destructive operations this session
    auto_approve: bool = False


def _update_env_url(new_url: str) -> None:
    """Write/replace KUBE_Q_URL in ~/.kube-q/.env (creates file if absent)."""
    env_path = Path.home() / ".kube-q" / ".env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    lines = [ln for ln in lines if not ln.startswith("KUBE_Q_URL=")]
    lines.append(f"KUBE_Q_URL={new_url}")
    env_path.write_text("\n".join(lines) + "\n")


# ── Conversation persistence ──────────────────────────────────────────────────

def _save_conversation(
    messages: list[dict],
    path: str | None,
    user_name: str = "You",
    agent_name: str = "kube-q",
) -> None:
    """Write the conversation to a markdown file."""
    if not path:
        ts = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        path = f"kube-q-{ts}.md"
    lines = [f"# kube-q Conversation\n\n*Saved: {datetime.datetime.now().isoformat()}*\n"]
    for msg in messages:
        role = f"**{user_name}**" if msg["role"] == "user" else f"**{agent_name}**"
        lines.append(f"\n{role}:\n\n{msg['content']}\n\n---")
    with open(path, "w") as f:
        f.write("\n".join(lines))
    console.print(f"[dim]Conversation saved to[/dim] {path}")


# ── REPL ──────────────────────────────────────────────────────────────────────

def run_repl(cfg: ReplConfig) -> None:
    """Run the interactive REPL loop using the provided configuration."""
    url = cfg.url
    stream = cfg.stream
    initial_conversation_id = cfg.initial_conversation_id
    initial_session_id = cfg.initial_session_id
    show_header = cfg.show_header
    initial_messages = cfg.initial_messages
    user_id = cfg.user_id
    quiet = cfg.quiet
    api_key = cfg.api_key
    ca_cert = cfg.ca_cert
    query_timeout = cfg.query_timeout
    health_timeout = cfg.health_timeout
    namespace_timeout = cfg.namespace_timeout
    startup_retry_timeout = cfg.startup_retry_timeout
    startup_retry_interval = cfg.startup_retry_interval
    skip_health_check = cfg.skip_health_check
    user_name = cfg.user_name
    agent_name = cfg.agent_name
    model = cfg.model
    cost_prompt_override = cfg.cost_prompt_override
    cost_completion_override = cfg.cost_completion_override
    chat_path = cfg.chat_path
    auth_scheme = cfg.auth_scheme
    health_path = cfg.health_path
    backend_label = cfg.backend_label

    # initial_session_id takes precedence over initial_conversation_id
    effective_id = initial_session_id or initial_conversation_id
    resolved_user_id = user_id or _load_or_create_user_id()

    state = SessionState(
        conversation_id=effective_id or str(uuid.uuid4()),
        user_id=resolved_user_id,
        messages=list(initial_messages) if initial_messages else [],
    )
    state.current_context = cfg.kube_context

    # ── Load user plugins (best-effort) ───────────────────────────────────────
    loaded_plugins = plugins.load_plugins()
    plugin_cmds: dict[str, str] = {
        name: (help_text or "plugin command")
        for name, (_fn, help_text) in plugins.registered_commands().items()
    }

    # Hydrate from store when resuming a named session
    if initial_session_id and not initial_messages:
        stored = store.load_messages(initial_session_id)
        if stored:
            state.messages = stored
            _replay_history(stored, user_name=user_name, agent_name=agent_name)
        else:
            console.print(
                f"[dim]No stored history for session {initial_session_id}.[/dim]"
            )
        # Restore stored kube_context only when the user didn't explicitly
        # pass --context on the command line (explicit CLI flag wins).
        if not cfg.kube_context:
            meta = store.load_session_meta(initial_session_id) or {}
            if meta.get("kube_context"):
                state.current_context = meta["kube_context"]
                console.print(
                    f"[dim]Restored kube context[/dim] "
                    f"[bold]{state.current_context}[/bold] [dim]from session.[/dim]"
                )
    _prepend_ns_once = False
    _pending_retry: str = ""  # pre-fills next prompt after a failed send

    if show_header and not quiet:
        if skip_health_check or health_path is None:
            connected = True
            reason = ""
        else:
            connected, reason = check_health(
                url, api_key=api_key, ca_cert=ca_cert, timeout=health_timeout,
                health_path=health_path, auth_scheme=auth_scheme,
            )

        if not connected:
            console.print(f"[yellow]Cannot reach {url}{health_path or ''}[/yellow]")
            console.print(f"[dim]  Reason: {reason}[/dim]")

            # "Connection refused" and DNS failures are definitive — nothing will
            # start listening while we wait, so skip the retry loop immediately.
            _definitive = any(k in reason for k in ("Connection refused", "DNS resolution failed"))

            if _definitive or startup_retry_timeout <= 0:
                console.print(
                    "[dim]  Entering offline mode — use [bold]/config set url=<URL>[/bold] to point at a running backend.[/dim]\n"
                )
            else:
                console.print(
                    f"[dim]  Retrying every {startup_retry_interval}s "
                    f"for up to {startup_retry_timeout}s…[/dim]\n"
                )
                deadline = time.monotonic() + startup_retry_timeout
                attempt = 0
                try:
                    while not connected and time.monotonic() < deadline:
                        remaining = int(deadline - time.monotonic())
                        attempt += 1
                        status_text = Text.assemble(
                            (" Waiting for API… ", "cyan"),
                            (f"attempt {attempt}", "dim"),
                            (f"  ({remaining}s remaining)", "dim"),
                        )
                        with Live(
                            Spinner("dots", text=status_text),
                            console=console,
                            refresh_per_second=4,
                            transient=True,
                        ):
                            time.sleep(min(startup_retry_interval, max(0, remaining)))

                        connected, reason = check_health(
                            url, api_key=api_key, ca_cert=ca_cert, timeout=health_timeout,
                            health_path=health_path, auth_scheme=auth_scheme,
                        )
                        if connected:
                            break
                        if any(k in reason for k in ("Connection refused", "DNS resolution failed")):
                            console.print(
                                "[dim]  Connection refused — stopping retry. "
                                "Use [bold]/config set url=<URL>[/bold] to change the backend.[/dim]"
                            )
                            break
                except KeyboardInterrupt:
                    console.print(
                        "\n[dim]Startup wait cancelled — continuing without API connection.[/dim]\n"
                    )
                    connected = False
                    reason = "Cancelled by user"

                if connected:
                    console.print(f"[green]Connected to {url}[/green]\n")
                else:
                    _print_not_connected_panel(url, reason)

        _print_logo(connected=connected)
        backend_part = (
            f"   [dim]Backend:[/dim] {backend_label}"
            if backend_label and backend_label != "kube-q"
            else ""
        )
        profile_part = (
            f"   [dim]Profile:[/dim] {cfg.profile}" if cfg.profile else ""
        )
        context_part = (
            f"   [dim]Context:[/dim] {state.current_context}"
            if state.current_context else ""
        )
        plugin_part = (
            f"\n[dim]Plugins loaded: {', '.join(loaded_plugins)}[/dim]"
            if loaded_plugins else ""
        )
        console.print(Panel.fit(
            f"[dim]API:[/dim] {url}{backend_part}{profile_part}{context_part}\n"
            f"[dim]Conversation:[/dim] {state.conversation_id}\n"
            f"[dim]Type [yellow]/help[/yellow] for commands · "
            f"[yellow]Enter[/yellow] to send · "
            f"[yellow]Alt+Enter[/yellow] for newline[/dim]"
            f"{plugin_part}",
            border_style="dim cyan",
        ))
        console.print()

    # Load kubectl contexts once for tab-completion; cheap enough to do every start.
    try:
        _kube_contexts = list_contexts()
    except Exception:
        _kube_contexts = []

    def _namespaces_provider() -> list[str]:
        return fetch_namespaces(
            url, resolved_user_id, api_key=api_key,
            ca_cert=ca_cert, timeout=namespace_timeout,
        ) or []

    pt_session = _make_prompt_session(
        contexts=_kube_contexts,
        profiles=_list_profiles(),
        extra_commands=plugin_cmds,
        namespaces_provider=_namespaces_provider,
    )

    while True:
        if state.hitl_pending:
            prompt = FormattedText([("bold fg:ansiyellow", "HITL> ")])
        elif state.current_namespace:
            prompt = FormattedText(
                [("bold fg:ansigreen", f"{user_name} [{state.current_namespace}]: ")]
            )
        else:
            prompt = FormattedText([("bold fg:ansigreen", f"{user_name}: ")])

        try:
            user_input = pt_session.prompt(prompt, default=_pending_retry).strip()
            _pending_retry = ""
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye.[/dim]")
            break

        if not user_input:
            continue

        if user_input.lower() in ("/quit", "/exit", "/q"):
            console.print("[dim]Goodbye.[/dim]")
            break

        if user_input.lower().startswith("/help"):
            parts = user_input.split(maxsplit=1)
            topic = parts[1].strip().lower() if len(parts) > 1 else ""
            render_help(topic)
            continue

        if user_input.lower() == "/new":
            cleared = len(state.messages)
            state.conversation_id = str(uuid.uuid4())
            state.messages = []
            state.hitl_pending = False
            state.pending_action_id = None
            s = "s" if cleared != 1 else ""
            cleared_note = f"  [dim]({cleared} message{s} cleared)[/dim]" if cleared else ""
            console.print(
                f"[dim]New conversation started:[/dim] {state.conversation_id}{cleared_note}"
            )
            continue

        if user_input.lower() == "/id":
            console.print(f"[dim]Conversation ID:[/dim] {state.conversation_id}")
            continue

        if user_input.lower() == "/state":
            ns = state.current_namespace
            ns_line = (
                f"  [dim]Namespace    [/dim] {ns}"
                if ns
                else "  [dim]Namespace    [/dim] [dim italic](none)[/dim italic]"
            )
            ctx = state.current_context
            ctx_line = (
                f"  [dim]Kube Context [/dim] {ctx}"
                if ctx
                else "  [dim]Kube Context [/dim] [dim italic](none)[/dim italic]"
            )
            backend_line = f"  [dim]Backend      [/dim] {backend_label}"
            hitl_line = (
                f"  [dim]HITL pending [/dim] [bold yellow]yes — action_id={state.pending_action_id}[/bold yellow]"  # noqa: E501
                if state.hitl_pending
                else "  [dim]HITL pending [/dim] no"
            )
            tok = store.get_session_tokens(state.conversation_id)
            last_u = store.get_last_usage(state.conversation_id)
            last_model = last_u.get("model") if last_u else None
            cost = costs.estimate_cost(
                last_model,
                tok["total_prompt_tokens"],
                tok["total_completion_tokens"],
                cost_prompt_override,
                cost_completion_override,
            )
            cost_part = f" (~{costs.format_cost(cost)})" if tok["total_tokens"] else ""
            tokens_line = f"  [dim]Tokens       [/dim] {tok['total_tokens']:,}{cost_part}"
            console.print(Panel(
                f"  [dim]Conversation [/dim] {state.conversation_id}\n"
                f"  [dim]User ID      [/dim] {state.user_id}\n"
                f"  [dim]Messages     [/dim] {len(state.messages)}\n"
                f"{tokens_line}\n"
                f"{backend_line}\n"
                f"{ctx_line}\n"
                f"{ns_line}\n"
                f"{hitl_line}",
                title="[bold cyan]Session State[/bold cyan]",
                border_style="dim cyan",
                expand=False,
                padding=(0, 1),
            ))
            continue

        if user_input.lower() == "/clear":
            os.system("clear")
            continue

        if user_input.lower().startswith("/save"):
            parts = user_input.split(maxsplit=1)
            save_path = parts[1] if len(parts) > 1 else None
            if state.messages:
                console.print(
                    "[dim yellow]Note: conversations may contain sensitive cluster data "
                    "— save to a secure location.[/dim yellow]"
                )
            _save_conversation(
                state.messages, save_path, user_name=user_name, agent_name=agent_name
            )
            continue

        if user_input.lower().startswith("/context"):
            parts = user_input.split(maxsplit=1)
            ctx_arg = parts[1].strip() if len(parts) > 1 else ""
            if not ctx_arg:
                if state.current_context:
                    state.current_context = None
                    console.print("[dim]Kubernetes context cleared.[/dim]")
                else:
                    known = _kube_contexts or list_contexts()
                    if known:
                        console.print("[dim]Available kubectl contexts:[/dim]")
                        for name in known:
                            console.print(f"  • {name}")
                    else:
                        console.print(
                            "[dim]No kubectl contexts found "
                            "(is kubectl installed and is ~/.kube/config valid?).[/dim]"
                        )
            else:
                known = _kube_contexts or list_contexts()
                if known and ctx_arg not in known:
                    console.print(
                        f"[yellow]Warning:[/yellow] '{ctx_arg}' is not in your kubeconfig. "
                        "Setting anyway — the backend may still accept it."
                    )
                state.current_context = ctx_arg
                console.print(
                    f"[dim]Active kube context set to[/dim] [bold]{ctx_arg}[/bold]"
                )
            continue

        if user_input.lower().startswith("/profile"):
            parts = user_input.split(maxsplit=2)
            sub = parts[1].strip().lower() if len(parts) > 1 else ""
            sub_arg = parts[2].strip() if len(parts) > 2 else ""

            def _show_profile_list() -> None:
                profiles_here = _list_profiles()
                if not profiles_here:
                    console.print(
                        "[dim]No profiles in ~/.kube-q/profiles/. "
                        "Create one with [yellow]/profile new <name>[/yellow].[/dim]"
                    )
                    return
                active = cfg.profile or "[dim](none)[/dim]"
                console.print(f"[dim]Active profile:[/dim] {active}")
                console.print("[dim]Available profiles:[/dim]")
                for p in profiles_here:
                    mark = "→" if p == cfg.profile else " "
                    console.print(f"  {mark} {p}")

            if not sub or sub == "list":
                _show_profile_list()
                console.print(
                    "[dim]Switching profiles mid-session is not supported — "
                    "restart with [yellow]KUBE_Q_PROFILE=<name> kq[/yellow] "
                    "or [yellow]kq --profile <name>[/yellow].[/dim]"
                )
            elif sub == "new":
                if not sub_arg:
                    console.print("[yellow]Usage: /profile new <name>[/yellow]")
                else:
                    _config_cmd.cmd_profile_new(sub_arg)
            elif sub in ("delete", "rm"):
                if not sub_arg:
                    console.print("[yellow]Usage: /profile delete <name>[/yellow]")
                else:
                    _config_cmd.cmd_profile_delete(sub_arg)
            elif sub == "show":
                if not sub_arg:
                    console.print("[yellow]Usage: /profile show <name>[/yellow]")
                else:
                    _config_cmd.cmd_profile_show(sub_arg)
            else:
                # Bare name → legacy "how to switch" hint.
                console.print(
                    f"[yellow]To use profile '{sub}', restart kq:[/yellow]\n"
                    f"  [bold]kq --profile {sub}[/bold]\n"
                    f"  [bold]KUBE_Q_PROFILE={sub} kq[/bold]"
                )
            continue

        if user_input.lower().startswith("/config"):
            parts = user_input.split(maxsplit=2)
            sub = parts[1].strip().lower() if len(parts) > 1 else "show"
            sub_arg = parts[2].strip() if len(parts) > 2 else ""
            if sub == "show":
                _config_cmd.cmd_show()
            elif sub == "set":
                if not sub_arg or "=" not in sub_arg:
                    console.print("[yellow]Usage: /config set KEY=VALUE[/yellow]")
                else:
                    if _config_cmd.cmd_set(sub_arg) == 0:
                        api_key = os.environ.get("KUBE_Q_API_KEY") or None
                        url = os.environ.get("KUBE_Q_URL") or url
                        model = os.environ.get("KUBE_Q_MODEL") or model
                        raw_key = sub_arg.partition("=")[0].strip().lower()
                        if raw_key in ("url", "kube_q_url"):
                            _ok, _reason = check_health(
                                url, api_key=api_key, ca_cert=ca_cert,
                                timeout=health_timeout, health_path=health_path,
                                auth_scheme=auth_scheme,
                            )
                            if _ok:
                                console.print(f"[green]✓ Connected to {url}[/green]")
                            else:
                                console.print(f"[red]✗ Cannot reach {url}[/red]")
                                console.print(f"[dim]  {_reason}[/dim]")
            elif sub == "reset":
                _config_cmd.cmd_reset(sub_arg or None)
            else:
                console.print(
                    "[yellow]Usage: /config [show|set KEY=VAL|reset [KEY]][/yellow]"
                )
            continue

        if user_input.lower() == "/list":
            _print_sessions_table(store.list_sessions(20))
            continue

        if user_input.lower() == "/version":
            try:
                from importlib.metadata import PackageNotFoundError
                from importlib.metadata import version as _pkg_version
                try:
                    _v = _pkg_version("kube-q")
                except PackageNotFoundError:
                    _v = "unknown"
            except Exception:
                _v = "unknown"
            console.print(f"[bold cyan]kube-q[/bold cyan] {_v}")
            continue

        if user_input.lower() == "/plugins":
            entries = plugins.registered_commands()
            if not entries:
                console.print(
                    "[dim]No plugins loaded. Drop Python files into "
                    "[yellow]~/.kube-q/plugins/[/yellow] that call "
                    "[yellow]kube_q.plugins.register(...)[/yellow].[/dim]"
                )
            else:
                console.print(
                    f"[bold cyan]Plugin commands ({len(entries)}):[/bold cyan]"
                )
                for name in sorted(entries):
                    _fn, help_text = entries[name]
                    console.print(
                        f"  [yellow]{name}[/yellow]"
                        + (f"  [dim]— {help_text}[/dim]" if help_text else "")
                    )
            continue

        if user_input.lower().startswith("/ns"):
            parts = user_input.split(maxsplit=1)
            ns_arg = parts[1].strip() if len(parts) > 1 else ""
            if not ns_arg:
                state.current_namespace = None
                _prepend_ns_once = False
                console.print("[dim]Namespace cleared.[/dim]")
            else:
                # Validate against the cluster namespace list from the backend.
                _known = fetch_namespaces(
                    url, state.user_id, api_key=api_key,
                    ca_cert=ca_cert, timeout=namespace_timeout
                )
                _ns_valid: bool | None = (
                    ns_arg in _known if _known is not None else None
                )

                if _ns_valid is False:
                    console.print(
                        f"[red]Namespace '{ns_arg}' not found in the cluster.[/red] "
                        "Use [bold]list all ns[/bold] to see available namespaces."
                    )
                else:
                    state.current_namespace = ns_arg
                    _prepend_ns_once = True
                    console.print(f"[dim]Active namespace set to[/dim] [bold]{ns_arg}[/bold]")
            continue

        if user_input.lower() in ("/sessions", "/resume"):
            picked = _pick_session_interactive(20)
            if picked:
                _resume_session(
                    state, picked, user_name=user_name, agent_name=agent_name
                )
            continue

        if user_input.lower().startswith("/history"):
            parts = user_input.split(maxsplit=1)
            arg = parts[1] if len(parts) > 1 else ""
            _print_history(
                state.messages, arg, user_name=user_name, agent_name=agent_name
            )
            continue

        if user_input.lower() == "/forget":
            console.print(
                f"[yellow]This will delete session [bold]{state.conversation_id}[/bold] "
                "from local history (server-side data is not affected).[/yellow]"
            )
            try:
                confirm = pt_session.prompt(
                    FormattedText([("bold fg:ansiyellow", "Delete? [y/N] ")])
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                confirm = ""
            if confirm == "y":
                store.delete_session(state.conversation_id)
                new_id = str(uuid.uuid4())
                state.conversation_id = new_id
                state.messages = []
                state.hitl_pending = False
                state.pending_action_id = None
                console.print(f"[dim]Session deleted. New session started:[/dim] {new_id}")
            else:
                console.print("[dim]Cancelled.[/dim]")
            continue

        if user_input.lower() in ("/tokens", "/cost"):
            _print_token_panel(
                state.conversation_id, cost_prompt_override, cost_completion_override
            )
            continue

        if user_input.lower().startswith("/search"):
            parts = user_input.split(maxsplit=1)
            query = parts[1].strip() if len(parts) > 1 else ""
            if not query:
                console.print("[yellow]Usage: /search <query>[/yellow]")
            else:
                results = store.search_sessions(query)
                if results:
                    format_search_results(results)
                else:
                    console.print(f"[dim]No sessions matched '{query}'.[/dim]")
            continue

        if user_input.lower() == "/branch":
            new_id = str(uuid.uuid4())
            n = len(state.messages)
            store.branch_session(state.conversation_id, new_id, n)
            state.conversation_id = new_id
            console.print(f"[dim]Branched at message {n}. New session:[/dim] {new_id}")
            console.print("[dim]Original session preserved. Use /sessions to see both.[/dim]")
            continue

        if user_input.lower() == "/branches":
            branches = store.list_branches(state.conversation_id)
            format_branches(branches, state.conversation_id)
            continue

        if user_input.lower().startswith("/title"):
            parts = user_input.split(maxsplit=1)
            new_title = parts[1].strip() if len(parts) > 1 else ""
            if not new_title:
                console.print("[yellow]Usage: /title <text>[/yellow]")
            else:
                store.rename_session(state.conversation_id, new_title)
                console.print(f"[dim]Session title set to '{new_title}'[/dim]")
            continue

        if user_input.lower() == "/approve":
            user_input = "approve"
            state.hitl_pending = False

        elif user_input.lower() == "/deny":
            user_input = "deny"
            state.hitl_pending = False

        elif user_input.lower().startswith("/url"):
            parts = user_input.split(maxsplit=1)
            if len(parts) < 2:
                console.print(f"[dim]Current backend URL:[/dim] {url}")
                console.print("[dim]Use [bold]/config set url=<URL>[/bold] to change it.[/dim]")
            else:
                console.print(
                    f"[dim]Hint: use [bold]/config set url={parts[1].strip()}[/bold] to change the URL.[/dim]"
                )
            continue

        # Plugin slash commands
        elif user_input.startswith("/"):
            parts = user_input.split(maxsplit=1)
            cmd = parts[0].lower()
            plug_args = parts[1] if len(parts) > 1 else ""
            if plugins.dispatch(
                cmd,
                plugins.PluginContext(
                    args=plug_args,
                    state=state,
                    cfg=cfg,
                    console=console,
                ),
            ):
                continue

            # Catch typos in unknown slash commands
            known = list(_SLASH_COMMANDS) + list(plugin_cmds)
            suggestions = difflib.get_close_matches(cmd, known, n=1, cutoff=0.6)
            if suggestions:
                console.print(
                    f"[yellow]Unknown command.[/yellow] "
                    f"Did you mean [bold]{suggestions[0]}[/bold]?"
                )
            else:
                console.print(
                    f"[yellow]Unknown command [bold]{cmd}[/bold]. "
                    f"Type [bold]/help[/bold] for available commands.[/yellow]"
                )
            continue

        # Save original (pre-expansion) input for retry pre-fill
        original_input = user_input

        # Resolve @filename attachments
        user_input, attached, attach_errors = _resolve_attachments(user_input)
        for err in attach_errors:
            console.print(err)
        if attached:
            console.print(f"[dim]Attached: {', '.join(attached)}[/dim]")

        # Prepend namespace once (at /ns set) and kube context on every message
        context_prefix = ""
        if state.current_context:
            context_prefix += f"[context: kube_context={state.current_context}] "
        if _prepend_ns_once and state.current_namespace:
            context_prefix += f"[context: namespace={state.current_namespace}] "
            _prepend_ns_once = False
        if context_prefix:
            user_input = context_prefix + user_input
        state.messages.append({"role": "user", "content": user_input})

        request_id = f"req-{uuid.uuid4()}"
        state.last_request_id = request_id

        if stream:
            response_text, state.hitl_pending, action_id, usage = stream_query(
                url, state.messages, state.conversation_id, state.user_id,
                api_key=api_key, ca_cert=ca_cert, timeout=query_timeout,
                request_id=request_id, model=model,
                chat_path=chat_path, auth_scheme=auth_scheme,
                auto_approve=cfg.auto_approve, agent_name=agent_name,
            )
        else:
            response_text, state.hitl_pending, action_id, usage = non_stream_query(
                url, state.messages, state.conversation_id, state.user_id,
                api_key=api_key, ca_cert=ca_cert, timeout=query_timeout,
                request_id=request_id, model=model,
                chat_path=chat_path, auth_scheme=auth_scheme,
                agent_name=agent_name,
            )

        if not response_text:
            state.messages.pop()
            _pending_retry = original_input
            console.print(
                f"{error_timestamp()}[dim]Request failed — cannot reach {url}\n"
                f"  Use [yellow]/config set url=<URL>[/yellow] to change the backend "
                f"or check your connection.\n"
                f"  Your message is ready to resend.[/dim]"
            )
            continue

        if state.hitl_pending and action_id:
            state.pending_action_id = action_id
        elif not state.hitl_pending:
            state.pending_action_id = None

        state.messages.append({"role": "assistant", "content": response_text})

        # ── Persist to local store (best-effort) ──────────────────────────────
        store.upsert_session(
            state.conversation_id,
            state.user_id,
            state.current_namespace,
            kube_context=state.current_context,
        )
        store.append_message(state.conversation_id, "user", user_input, request_id)
        store.append_message(state.conversation_id, "assistant", response_text, request_id)
        if len(state.messages) == 2:  # first exchange
            store.set_session_title(state.conversation_id, user_input[:60])
        if usage:
            store.log_tokens(
                state.conversation_id,
                state.last_request_id,
                usage.get("model"),
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
            )

        # ── Per-turn status footer ────────────────────────────────────────────
        _sess_tok = store.get_session_tokens(state.conversation_id)
        _sess_cost = costs.estimate_cost(
            usage.get("model") if usage else None,
            _sess_tok["total_prompt_tokens"],
            _sess_tok["total_completion_tokens"],
            cost_prompt_override,
            cost_completion_override,
        )
        render_status_footer(
            kube_context=state.current_context,
            namespace=state.current_namespace,
            total_tokens=_sess_tok["total_tokens"],
            cost=costs.format_cost(_sess_cost) if _sess_tok["total_tokens"] else None,
        )

        if state.hitl_pending:
            render_hitl_panel()
