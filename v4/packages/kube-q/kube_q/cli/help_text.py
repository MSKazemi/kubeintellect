"""
help_text.py — Structured, topic-based help for the kube-q REPL.

``/help`` shows a compact overview plus the list of topics; ``/help <topic>``
drills into one area. Content lives here as data (a dict of topics) rather than
one monolithic f-string, so each section is easy to find and edit.
"""

from rich.panel import Panel
from rich.table import Table

from kube_q.cli.renderer import console

# ── Topics ──────────────────────────────────────────────────────────────────
# Each entry: key → (short one-line summary for the overview, full body markup).
# Bodies use the shared console theme (see kube_q.cli.theme) for colour roles.

_TOPICS: dict[str, tuple[str, str]] = {
    "messages": (
        "Sending messages, multi-line input, paste",
        ("[accent.bold]Sending messages[/accent.bold]\n\n"
        "  [accent]Enter[/accent]            Send your message\n"
        "  [accent]Alt+Enter[/accent]        Insert a newline  [muted](hold Alt, press Enter)[/muted]\n"
        "  [accent]Esc → Enter[/accent]      Insert a newline  [muted](universal fallback)[/muted]\n\n"
        "  Paste multi-line text freely — newlines are preserved before you send."),
    ),
    "files": (
        "Attaching files with @path",
        ("[accent.bold]Attaching files[/accent.bold]\n\n"
        "  Type [accent]@path/to/file[/accent] anywhere in your message to attach a file.\n"
        "  Its contents are embedded as a code block and sent with your message.\n\n"
        "  [accent]@deployment.yaml[/accent]          Attach a file in the current directory\n"
        "  [accent]@~/configs/service.json[/accent]   Home-relative path\n"
        "  [accent]@\"/path/with spaces.txt\"[/accent]   Quote paths with spaces\n\n"
        "  Multiple files per message are fine: [muted]What's wrong? @pod.yaml @svc.yaml[/muted]\n"
        "  Supported: YAML, JSON, Python, Shell, Go, Terraform, text, logs… (100 KB/file)."),
    ),
    "editing": (
        "Keyboard shortcuts and auto-complete",
        ("[accent.bold]Editing[/accent.bold]\n\n"
        "  [accent]Tab[/accent]              Auto-complete slash commands, /context, /profile, /ns, /save\n"
        "  [accent]↑ / ↓[/accent]            Scroll through previous messages (history)\n"
        "  [accent]Ctrl+A / Ctrl+E[/accent]  Jump to start / end of line\n"
        "  [accent]Ctrl+W[/accent]           Delete previous word\n"
        "  [accent]Ctrl+U[/accent]           Clear the input buffer\n"
        "  [accent]Ctrl+C[/accent]           Cancel current input (keeps history)\n"
        "  [accent]Ctrl+D[/accent]           Exit the session"),
    ),
    "conversation": (
        "/new /id /state /title /save /clear /version",
        ("[accent.bold]Conversation[/accent.bold]\n\n"
        "  [accent]/new[/accent]             Start a fresh conversation (new ID, clears history)\n"
        "  [accent]/id[/accent]              Show the current conversation ID\n"
        "  [accent]/state[/accent]           Full session state (ID, namespace, tokens, HITL flag)\n"
        "  [accent]/title <text>[/accent]    Rename the current session\n"
        "  [accent]/save [file][/accent]     Save conversation to markdown  [muted](Tab completes paths)[/muted]\n"
        "  [accent]/clear[/accent]           Clear the terminal screen\n"
        "  [accent]/version[/accent]         Print the installed kube-q version\n"
        "  [accent]/quit[/accent]  [muted]/exit /q[/muted]   Exit kube-q"),
    ),
    "namespace": (
        "/ns — set the active namespace",
        ("[accent.bold]Namespace[/accent.bold]\n\n"
        "  [accent]/ns <name>[/accent]       Set active namespace — prepended to every query\n"
        "  [accent]/ns[/accent]              Clear the active namespace\n\n"
        "  [muted]Tab-completes from the cluster (cached after first use).[/muted]"),
    ),
    "context": (
        "/context — set the kubectl context",
        ("[accent.bold]Kubernetes context[/accent.bold]\n\n"
        "  [accent]/context <name>[/accent]  Set active kubectl context — prepended to every query\n"
        "  [accent]/context[/accent]         List / clear the active context\n\n"
        "  [muted]Tab-completes from your kubeconfig.[/muted]"),
    ),
    "sessions": (
        "/sessions /list /history /forget /resume",
        ("[accent.bold]Session history[/accent.bold]\n\n"
        "  [accent]/sessions[/accent]        Interactive picker — ↑/↓ navigate, Enter resume, Esc cancel\n"
        "  [accent]/resume[/accent]          Alias for /sessions\n"
        "  [accent]/list[/accent]            Print recent sessions as a table (no picker)\n"
        "  [accent]/history[/accent]         Replay all messages in the current session\n"
        "  [accent]/history N[/accent]       Last N messages\n"
        "  [accent]/history X-Y[/accent]     Messages X through Y  [muted](1-indexed, inclusive)[/muted]\n"
        "  [accent]/history #N[/accent]      Just message #N\n"
        "  [accent]/forget[/accent]          Delete current session from local history  [muted](server untouched)[/muted]"),
    ),
    "search": (
        "/search and /branch — find & fork",
        ("[accent.bold]Search & branching[/accent.bold]\n\n"
        "  [accent]/search <query>[/accent]  Full-text search across all past sessions\n"
        "  [muted]    /search \"crash loop\" AND production[/muted]\n"
        "  [muted]    /search \"oom killed\" OR \"memory limit\"[/muted]\n"
        "  [accent]/branch[/accent]          Fork this conversation at the current point\n"
        "  [accent]/branches[/accent]        List all forks (and siblings) of this session"),
    ),
    "profiles": (
        "/profile and /plugins",
        ("[accent.bold]Profiles & plugins[/accent.bold]\n\n"
        "  [accent]/profile[/accent]              List profiles in ~/.kube-q/profiles/\n"
        "  [accent]/profile new <name>[/accent]   Create a profile .env from template\n"
        "  [accent]/profile show <name>[/accent]  Print a profile's contents\n"
        "  [accent]/profile delete <name>[/accent] Delete a profile file\n"
        "  [muted]Switching profiles requires a restart: [accent]kq --profile <name>[/accent][/muted]\n\n"
        "  [accent]/plugins[/accent]              List loaded plugins from ~/.kube-q/plugins/"),
    ),
    "tokens": (
        "/tokens — usage and cost",
        ("[accent.bold]Token usage[/accent.bold]\n\n"
        "  [accent]/tokens[/accent]  [muted]/cost[/muted]    Token counts and estimated cost for this session\n\n"
        "  Override rates via [muted]KUBE_Q_COST_PER_1K_PROMPT[/muted] / "
        "[muted]KUBE_Q_COST_PER_1K_COMPLETION[/muted]."),
    ),
    "hitl": (
        "Human-in-the-loop approvals",
        ("[accent.bold]Human-in-the-Loop (HITL)[/accent.bold]\n\n"
        "  When kube-q proposes a [warn]write action[/warn] (deploy, delete, scale…) it pauses\n"
        "  and shows the command. The prompt changes to [warn]HITL>[/warn].\n\n"
        "  [accent]/approve[/accent]         Execute the pending action\n"
        "  [accent]/deny[/accent]            Cancel it — nothing is applied"),
    ),
    "config": (
        "/config and ~/.kube-q/.env keys",
        ("[accent.bold]Connection & config[/accent.bold]\n\n"
        "  [accent]/config[/accent]                Print all keys, values, and their sources\n"
        "  [accent]/config set KEY=VALUE[/accent]  Write a key to ~/.kube-q/.env  [muted](takes effect now)[/muted]\n"
        "  [muted]    /config set url=https://api.kubeintellect.com[/muted]\n"
        "  [muted]    /config set api_key=your-key-here[/muted]\n"
        "  [muted]    /config set model=kubeintellect-v2[/muted]\n"
        "  [accent]/config reset KEY[/accent]      Remove a single key\n"
        "  [accent]/config reset[/accent]          Wipe ~/.kube-q/.env entirely\n\n"
        "  [muted]Common keys: KUBE_Q_URL, KUBE_Q_API_KEY, KUBE_Q_MODEL, KUBE_Q_STREAM,[/muted]\n"
        "  [muted]KUBE_Q_OUTPUT, KUBE_Q_USER_NAME, KUBE_Q_AGENT_NAME, KUBE_Q_CONTEXT.[/muted]\n"
        "  [muted]Logs: ~/.kube-q/kube-q.log[/muted]"),
    ),
    "flags": (
        "Useful launch flags (kq --...)",
        ("[accent.bold]Useful launch flags[/accent.bold]\n\n"
        "  [muted]kq --query \"...\"[/muted]            One-shot query, then exit\n"
        "  [muted]kq --url <URL>[/muted]              Connect to a specific API server\n"
        "  [muted]kq --api-key <key>[/muted]          Authenticate with an API key\n"
        "  [muted]kq --output plain[/muted]           Plain text output (no markdown)\n"
        "  [muted]kq --no-stream[/muted]              Wait for the full response\n"
        "  [muted]kq --no-banner[/muted]              Suppress logo (screen recordings)\n"
        "  [muted]kq --session-id <id>[/muted]        Resume a previous session by ID\n"
        "  [muted]kq --model <name>[/muted]           Override the model name\n"
        "  [muted]kq --profile <name>[/muted]         Load ~/.kube-q/profiles/<name>.env\n"
        "  [muted]kq --backend kube-q|openai|azure[/muted]  Pick the LLM backend\n"
        "  [muted]kq --debug[/muted]                  Show raw HTTP request/response log"),
    ),
}

# Order in which topics appear in the overview.
_ORDER = [
    "messages", "files", "editing", "conversation", "namespace", "context",
    "sessions", "search", "profiles", "tokens", "hitl", "config", "flags",
]


def _overview() -> None:
    """Print the compact help overview: how to drill in + the topic list."""
    table = Table.grid(padding=(0, 2))
    table.add_column(style="accent", no_wrap=True)
    table.add_column()
    for key in _ORDER:
        summary = _TOPICS[key][0]
        table.add_row(f"/help {key}", summary)

    console.print(Panel(
        "[accent.bold]kube-q[/accent.bold] — chat with your Kubernetes cluster.\n\n"
        "[muted]Type your question and press [accent]Enter[/accent]. "
        "Use [accent]/help <topic>[/accent] for details on any area below,\n"
        "or [accent]@file[/accent] to attach manifests. [accent]/quit[/accent] to exit.[/muted]\n",
        title="[accent.bold]kube-q Help[/accent.bold]",
        border_style="accent",
        padding=(1, 2),
        expand=False,
    ))
    console.print(table)
    console.print()


def render_help(topic: str = "") -> None:
    """Render help — overview when ``topic`` is empty, else the topic panel.

    Unknown topics fall back to the overview with a hint.
    """
    topic = (topic or "").strip().lower()
    if not topic:
        _overview()
        return
    # Friendly aliases.
    alias = {
        "ns": "namespace", "ctx": "context", "session": "sessions",
        "cost": "tokens", "plugin": "profiles", "plugins": "profiles",
        "profile": "profiles", "attach": "files", "file": "files",
        "keys": "editing", "shortcuts": "editing", "approve": "hitl",
        "branch": "search", "env": "config",
    }
    topic = alias.get(topic, topic)
    entry = _TOPICS.get(topic)
    if entry is None:
        console.print(
            f"[warn]Unknown help topic '[bold]{topic}[/bold]'.[/warn] "
            "Try [accent]/help[/accent] to see all topics."
        )
        return
    _title, body = entry
    console.print(Panel(
        body,
        border_style="border",
        padding=(1, 2),
        expand=False,
    ))
