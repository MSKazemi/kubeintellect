#!/usr/bin/env python3
"""
kube-q — interactive terminal interface for the kube-q API.

Usage:
  kq                          # interactive REPL
  kq --query "get all pods"   # single query and exit
  kq --url https://api.kubeintellect.com   # custom API URL
  kq --backend openai         # talk directly to OpenAI (bypass kube-q server)
  kq --backend azure          # talk directly to Azure OpenAI
  kq --profile prod           # load ~/.kube-q/profiles/prod.env on top of defaults
  kq --context prod-cluster   # kubectl context prepended to every query
  kq help                     # list all `kq <command>` subcommands
  kq config show              # list effective config keys with their source
  kq config set KEY=VAL       # persist a value to ~/.kube-q/.env
  kq config reset [KEY]       # remove KEY (or wipe ~/.kube-q/.env when no KEY)
  kq config profile list      # list profiles in ~/.kube-q/profiles/
  kq config profile new NAME  # create a new profile template
  kq findings                 # detector findings (zero-token detection feed)
  kq digest                   # what the watchtower did while you were away
  kq replay <session-id>      # replay a recorded session (flight recorder)
  kq postmortem <session-id>  # grounded incident postmortem for a session
  kq detector new "..."       # author a natural-language detector
  kq preference               # view/set remembered user preferences
  kq v5-status                # v5 trust-plane status (flags, kill switch, spend cap)
  kq completion bash          # print a shell completion script (bash|zsh|fish)
  kq --no-stream              # disable streaming
  kq --user-id myuser         # set persistent user ID
  kq --no-banner              # suppress logo (screen recording)
  kq --api-key <key>          # authenticate with an API key
  kq --ca-cert /path/cert.pem # custom CA cert for TLS
  kq --output plain           # plain text output (no markdown)
  kq --debug                  # show raw HTTP requests/responses
  kq --version                # print version and exit
  kq --user-name Alice        # your display name in the prompt (default: You)
  kq --agent-name MyBot       # assistant name in saved conversations (default: kube-q)
  kq --list                   # list recent sessions and exit
  kq --search "pod crash"     # full-text search across session history and exit
  kq --session-id <id>        # resume a previous session by ID (replays stored transcript)
  kq --model gpt-4o           # override model name sent in requests
  kq --no-health-check        # skip startup health check (useful for web/scripted contexts)

Environment variables:
  KUBE_Q_URL=http://...                 # set API URL
  KUBE_Q_API_KEY=...                    # set API key (required when server auth is enabled)
  KUBE_Q_MODEL=kubeintellect-v2         # model name sent in requests
  KUBE_Q_SKIP_HEALTH_CHECK=true         # skip startup health check
  KUBE_Q_BACKEND=kube-q|openai|azure    # select backend
  KUBE_Q_OPENAI_API_KEY=sk-...          # used when backend=openai
  KUBE_Q_AZURE_OPENAI_API_KEY=...       # used when backend=azure
  KUBE_Q_AZURE_OPENAI_ENDPOINT=https://my-resource.openai.azure.com
  KUBE_Q_AZURE_OPENAI_DEPLOYMENT=my-deployment
  KUBE_Q_CONTEXT=prod-cluster           # initial kubectl context
  KUBE_Q_PROFILE=prod                   # load ~/.kube-q/profiles/prod.env
  KUBE_Q_PLUGIN_DIR=~/.kube-q/plugins   # override plugin directory

Config (~/.kube-q/.env or ./.env):
  KUBE_Q_URL=https://api.kubeintellect.com
  KUBE_Q_API_KEY=                  # required only when server auth is enabled
  KUBE_Q_MODEL=kubeintellect-v2
  KUBE_Q_TIMEOUT=120
  KUBE_Q_STREAM=true
  KUBE_Q_OUTPUT=rich               # rich | plain
  KUBE_Q_LOG_LEVEL=INFO            # DEBUG | INFO | WARNING | ERROR
  KUBE_Q_USER_NAME=You
  KUBE_Q_AGENT_NAME=kube-q
  KUBE_Q_COST_PER_1K_PROMPT=0.003      # override cost rate for /tokens
  KUBE_Q_COST_PER_1K_COMPLETION=0.006  # override cost rate for /tokens
  KUBE_Q_LOGO=KubeIntellect            # custom banner logo (use \\n for newlines)
  KUBE_Q_TAGLINE=© 2025 Acme Corp     # custom tagline / copyright line

In-REPL commands:
  /new                    — start a new conversation (new conversation ID)
  /id                     — show current conversation ID
  /state                  — show current session state in one line
  /clear                  — clear the terminal screen
  /save [file]            — save conversation to markdown file
  /ns <name>              — set active namespace (/ns with no arg clears it)
  /context <n>            — set kubectl context (/context with no arg lists/clears)
  /profile                — list profiles
  /profile new <n>        — create a profile .env from template
  /profile show <n>       — print a profile's contents
  /profile delete <n>     — delete a profile file
  /profile <n>            — show restart command to activate profile <n>
  /config                 — show effective config with each value's source
  /config set KEY=VAL     — persist KEY=VAL to ~/.kube-q/.env
  /config reset [KEY]     — remove KEY (or wipe the whole file when no KEY)
  /plugins                — list loaded plugin commands
  /sessions               — interactive picker: ↑/↓ + Enter to resume a past session
  /resume                 — alias for /sessions
  /list                   — print recent sessions as a table (no picker)
  /forget                 — delete current session from local history
  /tokens                 — show token counts and estimated cost for this session
  /cost                   — alias for /tokens
  /search <q>             — full-text search across all past sessions
  /branch                 — fork this conversation at the current point
  /branches               — list all forks of this session
  /title <text>           — rename the current session
  /config set url=<URL>   — change the backend URL (saves to ~/.kube-q/.env)
  /version                — print the installed kube-q version
  /approve                — approve a pending HITL action
  /deny                   — deny a pending HITL action
  /help [topic]           — help overview, or drill into a topic (e.g. /help sessions)
  /quit                   — exit

Keyboard shortcuts:
  Enter          — send message
  Alt+Enter      — insert newline (for multi-line messages)
  Esc → Enter    — insert newline (universal fallback)
  Tab            — auto-complete slash commands, /context, /profile, /ns, and /save paths
                   (suggestions also pop up as you type "/")

Attaching files:
  @file.yaml                 — embed a file's contents in your message
  @~/path/to/file.json       — home-relative paths supported
  @"path/with spaces.txt"    — quote paths that contain spaces
  what is wrong? @pod.yaml   — use anywhere in a message; multiple files per message ok
  Supported: yaml, json, py, sh, go, tf, toml, js, ts, txt, log, and more (100 KB limit)
"""

import argparse
import os
import sys
import uuid
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from urllib.parse import urlparse

from kube_q.cli import subcommands as _subcommands
from kube_q.cli.renderer import (
    _print_sessions_table,
    console,
    format_search_results,
    print_store_failure,
    set_custom_logo,
    set_custom_tagline,
    set_output_plain,
)
from kube_q.cli.repl import ReplConfig, run_repl
from kube_q.cli.store import list_sessions as _list_sessions
from kube_q.cli.store import search_sessions as _search_sessions
from kube_q.core.backends import BackendSpec, resolve_backend
from kube_q.core.config import load_config, setup_logging
from kube_q.core.session import load_or_create_user_id as _load_or_create_user_id
from kube_q.core.transport import set_debug
from kube_q.transport import non_stream_query, stream_query

try:
    __version__ = _pkg_version("kube-q")
except PackageNotFoundError:
    __version__ = "unknown"


def _peek_flag(argv: list[str], flag: str) -> str | None:
    """Find ``--flag value`` or ``--flag=value`` in argv without consuming it.

    Used for options that must be resolved before argparse (e.g. --profile,
    which changes how load_config() reads .env files).
    """
    for i, arg in enumerate(argv):
        if arg == flag and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(f"{flag}="):
            return arg[len(flag) + 1:]
    return None


# ── Single-query mode ─────────────────────────────────────────────────────────

def run_single_query(
    url: str,
    query: str,
    stream: bool,
    user_id: str | None = None,
    api_key: str | None = None,
    ca_cert: str | None = None,
    timeout: float = 120.0,
    model: str = "kubeintellect-v2",
    *,
    chat_path: str = "/v1/chat/completions",
    auth_scheme: str = "bearer",
    auto_approve: bool = False,
    agent_name: str = "kube-q",
) -> bool:
    """Run one non-interactive query.

    Returns ``True`` when the server actually answered (or paused for HITL
    approval), ``False`` when it did not. Both transports signal every failure
    path — 401, non-200, invalid JSON, retries exhausted, **and a stream that
    died mid-turn** — by returning empty text, so an empty answer is the uniform
    "this did not work" signal. That last one was the exception until 2026-08-19:
    a crashed stream delivers its reason as ordinary content, so it arrived here
    as a non-empty answer and this returned ``True``. The
    caller turns that into a non-zero exit status: ``kq -q`` is used in scripts
    and CI, where a failure that exits 0 is indistinguishable from an answer.
    """
    conversation_id = str(uuid.uuid4())
    if user_id is None:
        user_id = _load_or_create_user_id()
    messages = [{"role": "user", "content": query}]

    if stream:
        text, hitl_pending, _action_id, _usage = stream_query(
            url, messages, conversation_id, user_id,
            api_key=api_key, ca_cert=ca_cert, timeout=timeout, model=model,
            chat_path=chat_path, auth_scheme=auth_scheme, auto_approve=auto_approve,
            agent_name=agent_name)
    else:
        text, hitl_pending, _action_id, _usage = non_stream_query(
            url, messages, conversation_id, user_id,
            api_key=api_key, ca_cert=ca_cert, timeout=timeout, model=model,
            chat_path=chat_path, auth_scheme=auth_scheme, agent_name=agent_name)

    return bool(text) or hitl_pending


# ── Entry point ───────────────────────────────────────────────────────────────

def _print_commands() -> None:
    """Print the ``kq <command>`` listing (for ``kq help`` / ``kq commands``)."""
    console.print("[bold]kube-q commands[/bold]  —  run [cyan]kq <command> -h[/cyan] for details\n")
    for name, desc in _subcommands.describe():
        console.print(f"  [cyan]{name:<11}[/cyan] {desc}")
    console.print(
        "\nRun [cyan]kq[/cyan] with no command for the interactive REPL, "
        "or [cyan]kq -q \"...\"[/cyan] for a one-shot query."
    )


def _unknown_command(name: str) -> None:
    """Friendly 'unknown command' message with a 'did you mean' suggestion."""
    console.print(f"[red]Unknown command:[/red] [bold]{name}[/bold]")
    matches = _subcommands.suggest(name)
    if matches:
        hint = matches[0] if len(matches) == 1 else ", ".join(matches)
        console.print(f"Did you mean: [cyan]{hint}[/cyan]?")
    console.print(
        "Run [cyan]kq help[/cyan] for the command list, or "
        f'[cyan]kq -q "{name}"[/cyan] to ask a question.'
    )


def main() -> None:
    # Subcommands (`kq config ...`, `kq findings ...`, ...) are dispatched from a
    # central registry before argparse, so we don't thread every flag through the
    # subcommand parsers. The registry also feeds `kq --help` and `kq help`.
    argv = sys.argv[1:]
    if argv and not argv[0].startswith("-"):
        first = argv[0]
        runner = _subcommands.get_runner(first)
        if runner is not None:
            sys.exit(runner(argv[1:]))
        if _subcommands.is_help_alias(first):
            _print_commands()
            sys.exit(0)
        # A bare non-flag token is neither a query (use -q or the REPL) nor a
        # known command — guide the user instead of dumping raw argparse usage.
        _unknown_command(first)
        sys.exit(2)

    # --profile must be applied before load_config() so the profile's .env
    # contributes to defaults. Detect it early.
    _peek_profile = _peek_flag(sys.argv[1:], "--profile")
    if _peek_profile:
        import os as _os
        _os.environ["KUBE_Q_PROFILE"] = _peek_profile

    # Load config file first so CLI args can override it
    cfg = load_config()

    parser = argparse.ArgumentParser(
        prog="kq",
        description=(
            "kube-q — chat with your Kubernetes cluster.\n\n"
            "Commands (run `kq <command> -h` for details):\n"
            f"{_subcommands.help_block()}\n\n"
            "With no command, kq opens the interactive REPL; the options below "
            "apply to the REPL and to `-q` one-shot queries."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"kube-q {__version__}",
    )
    parser.add_argument(
        "--url",
        default=cfg.url,
        help=(
            "kube-q API base URL "
            "(env: KUBE_Q_URL, config: url, default: https://api.kubeintellect.com)"
        ),
    )
    parser.add_argument(
        "--query", "-q",
        default=None,
        help="Run a single query and exit (non-interactive mode)",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        default=not cfg.stream,
        help="Disable streaming — wait for full response",
    )
    parser.add_argument(
        "--conversation-id",
        default=None,
        help="Resume an existing conversation by ID",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        metavar="ID",
        help=(
            "Resume a previous session by ID (loads history from local store "
            "and replays the transcript in the terminal)"
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        default=False,
        help="List recent sessions and exit (no server connection needed)",
    )
    parser.add_argument(
        "--search",
        default=None,
        metavar="QUERY",
        help="Full-text search across session history and exit (no server connection needed)",
    )
    parser.add_argument(
        "--user-id",
        default=None,
        metavar="ID",
        help=(
            "User ID to send with requests (persisted to ~/.kube-q/user-id). "
            "Reads from ~/.kube-q/user-id if not supplied."
        ),
    )
    parser.add_argument(
        "--no-banner", "--quiet",
        dest="quiet",
        action="store_true",
        default=False,
        help="Suppress logo and header panel (useful for screen recordings)",
    )
    parser.add_argument(
        "--api-key",
        default=cfg.api_key,
        metavar="KEY",
        help="API key for authentication (env: KUBE_Q_API_KEY, config: api_key)",
    )
    parser.add_argument(
        "--ca-cert",
        default=None,
        metavar="PATH",
        help="Path to custom CA certificate bundle for TLS verification",
    )
    parser.add_argument(
        "--output",
        choices=["rich", "plain"],
        default=cfg.output,
        help="Output format: 'rich' (default, markdown rendering) or 'plain' (raw text)",
    )
    parser.add_argument(
        "--user-name",
        default=cfg.user_name,
        metavar="NAME",
        help=(
            "Display name for you in the prompt and saved conversations "
            "(env: KUBE_Q_USER_NAME, config: user_name, default: You)"
        ),
    )
    parser.add_argument(
        "--agent-name",
        default=cfg.agent_name,
        metavar="NAME",
        help=(
            "Display name for the assistant in saved conversations "
            "(env: KUBE_Q_AGENT_NAME, config: agent_name, default: kube-q)"
        ),
    )
    parser.add_argument(
        "--model",
        default=cfg.model,
        help=(
            "Model name to send in requests "
            "(env: KUBE_Q_MODEL, config: model, default: kubeintellect-v2)"
        ),
    )
    parser.add_argument(
        "--no-health-check",
        dest="skip_health_check",
        action="store_true",
        default=cfg.skip_health_check,
        help=(
            "Skip the startup health check and retry loop — go straight to the prompt. "
            "(env: KUBE_Q_SKIP_HEALTH_CHECK)"
        ),
    )
    parser.add_argument(
        "--debug", "--verbose",
        dest="debug",
        action="store_true",
        default=False,
        help=(
            "Enable debug mode: log raw HTTP requests/responses to stderr "
            "and ~/.kube-q/kube-q.log"
        ),
    )
    parser.add_argument(
        "--backend",
        choices=["kube-q", "openai", "azure"],
        default=cfg.backend,
        help=(
            "Select the backend: 'kube-q' (default, uses --url), "
            "'openai' (direct OpenAI), or 'azure' (Azure OpenAI). "
            "(env: KUBE_Q_BACKEND)"
        ),
    )
    parser.add_argument(
        "--openai-api-key",
        default=cfg.openai_api_key,
        metavar="KEY",
        help="OpenAI API key (env: KUBE_Q_OPENAI_API_KEY). Used when --backend=openai.",
    )
    parser.add_argument(
        "--openai-endpoint",
        default=cfg.openai_endpoint,
        metavar="URL",
        help=(
            "OpenAI-compatible endpoint (env: KUBE_Q_OPENAI_ENDPOINT, "
            "default: https://api.openai.com). Used when --backend=openai."
        ),
    )
    parser.add_argument(
        "--azure-openai-api-key",
        default=cfg.azure_openai_api_key,
        metavar="KEY",
        help="Azure OpenAI key (env: KUBE_Q_AZURE_OPENAI_API_KEY).",
    )
    parser.add_argument(
        "--azure-openai-endpoint",
        default=cfg.azure_openai_endpoint,
        metavar="URL",
        help=(
            "Azure OpenAI resource endpoint, e.g. https://my-res.openai.azure.com "
            "(env: KUBE_Q_AZURE_OPENAI_ENDPOINT)."
        ),
    )
    parser.add_argument(
        "--azure-openai-deployment",
        default=cfg.azure_openai_deployment,
        metavar="NAME",
        help=(
            "Azure OpenAI deployment name — NOT the model name "
            "(env: KUBE_Q_AZURE_OPENAI_DEPLOYMENT)."
        ),
    )
    parser.add_argument(
        "--profile",
        default=None,
        metavar="NAME",
        help=(
            "Load ~/.kube-q/profiles/<NAME>.env on top of the normal config "
            "(env: KUBE_Q_PROFILE). Use 'kq config profile list' to see available profiles."
        ),
    )
    parser.add_argument(
        "--context",
        default=cfg.kube_context,
        metavar="NAME",
        help=(
            "kubectl context to target — prepended to every query as "
            "[context: kube_context=...] (env: KUBE_Q_CONTEXT)."
        ),
    )
    parser.add_argument(
        "--auto-approve",
        dest="auto_approve",
        action="store_true",
        default=False,
        help=(
            "Skip HITL approval prompts — all destructive operations are executed "
            "automatically without asking for confirmation. Use for testing only."
        ),
    )

    args = parser.parse_args()

    # ── Logging ───────────────────────────────────────────────────────────────
    setup_logging(log_level=cfg.log_level, debug=args.debug)
    if args.debug:
        set_debug(True)

    stream = not args.no_stream
    user_id = _load_or_create_user_id(args.user_id)

    if args.output == "plain":
        set_output_plain(True)

    # Apply banner customisation from config
    set_custom_logo(cfg.logo)
    set_custom_tagline(cfg.tagline)

    # Fold CLI args back into cfg so resolve_backend() sees everything
    cfg.url                     = args.url
    cfg.api_key                 = args.api_key
    cfg.model                   = args.model
    cfg.backend                 = args.backend
    cfg.openai_api_key          = args.openai_api_key
    cfg.openai_endpoint         = args.openai_endpoint
    cfg.azure_openai_api_key    = args.azure_openai_api_key
    cfg.azure_openai_endpoint   = args.azure_openai_endpoint
    cfg.azure_openai_deployment = args.azure_openai_deployment
    cfg.kube_context            = args.context

    backend: BackendSpec = resolve_backend(cfg)

    # Warn on plain-HTTP connections to non-local hosts (only for kube-q backend;
    # OpenAI/Azure are https and the user owns the endpoint choice).
    if backend.kind == "kube-q":
        parsed_url = urlparse(backend.url)
        if parsed_url.scheme == "http":
            host = parsed_url.hostname or ""
            if host not in ("localhost", "127.0.0.1", "::1", ""):
                console.print(
                    f"[yellow]Warning:[/yellow] connecting over plain HTTP to "
                    f"[bold]{backend.url}[/bold] — consider using HTTPS in production."
                )

    if args.list:
        _print_sessions_table(_list_sessions(20))
        return

    if args.search:
        results = _search_sessions(args.search, limit=20)
        if results:
            format_search_results(results)
        elif not print_store_failure():
            console.print(f"[dim]No sessions matched '{args.search}'.[/dim]")
        return

    # For non-kube-q backends, there's no /healthz endpoint — skip by default
    skip_health = args.skip_health_check or backend.health_path is None

    if args.query:
        answered = run_single_query(
            backend.url, args.query, stream,
            user_id=user_id, api_key=backend.api_key, ca_cert=args.ca_cert,
            timeout=cfg.timeout, model=backend.model,
            chat_path=backend.chat_path, auth_scheme=backend.auth_scheme,
            auto_approve=args.auto_approve, agent_name=args.agent_name,
        )
        if not answered:
            sys.exit(1)
    else:
        run_repl(ReplConfig(
            url=backend.url,
            stream=stream,
            initial_conversation_id=args.conversation_id,
            initial_session_id=args.session_id,
            user_id=user_id,
            quiet=args.quiet,
            api_key=backend.api_key,
            ca_cert=args.ca_cert,
            query_timeout=cfg.timeout,
            health_timeout=cfg.health_timeout,
            namespace_timeout=cfg.namespace_timeout,
            startup_retry_timeout=cfg.startup_retry_timeout,
            startup_retry_interval=cfg.startup_retry_interval,
            skip_health_check=skip_health,
            user_name=args.user_name,
            agent_name=args.agent_name,
            model=backend.model,
            cost_prompt_override=cfg.cost_per_1k_prompt,
            cost_completion_override=cfg.cost_per_1k_completion,
            chat_path=backend.chat_path,
            auth_scheme=backend.auth_scheme,
            health_path=backend.health_path,
            backend_label=backend.label,
            kube_context=args.context,
            profile=os.environ.get("KUBE_Q_PROFILE"),
            auto_approve=args.auto_approve,
        ))


if __name__ == "__main__":
    main()
