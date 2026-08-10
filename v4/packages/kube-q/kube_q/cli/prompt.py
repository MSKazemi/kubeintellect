"""
prompt.py — prompt_toolkit input layer for the kube-q REPL.

Owns the slash-command catalogue, the tab-completer, and the ``PromptSession``
factory (key bindings + history). Kept separate from the REPL loop so the input
machinery can be understood and tested on its own.
"""

from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion, PathCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings

from kube_q.core.config import CONFIG_DIR

# ── Slash-command catalogue ───────────────────────────────────────────────────
# Shared by the completer (suggestions) and the REPL loop (typo detection).
_SLASH_COMMANDS: dict[str, str] = {
    "/new": "start a new conversation",
    "/id": "show current conversation ID",
    "/state": "show current session state",
    "/clear": "clear the terminal screen",
    "/save": "save conversation to a markdown file",
    "/approve": "approve a pending HITL action",
    "/deny": "deny a pending HITL action",
    "/help": "show all commands (/help <topic> to drill in)",
    "/ns": "set or clear the active namespace",
    "/sessions": "pick a past session to resume (arrow keys)",
    "/resume": "alias for /sessions",
    "/list": "table of recent sessions (no picker)",
    "/history": "show messages in the current session (optional: N | X-Y | #N)",
    "/forget": "delete current session from local history",
    "/tokens": "show token counts and estimated cost",
    "/cost": "alias for /tokens",
    "/search": "full-text search across past sessions",
    "/branch": "fork this conversation at the current point",
    "/branches": "list all forks of this session",
    "/title": "rename the current session",
    "/context": "set or clear the kubectl context",
    "/profile": "profile management (list / new / show / delete)",
    "/config": "show / set / reset ~/.kube-q/.env keys",
    "/version": "show kube-q version",
    "/plugins": "list loaded plugin commands",
    "/quit": "exit kube-q",
    "/exit": "exit kube-q",
    "/q": "exit kube-q",
}
_HISTORY_FILE = str(CONFIG_DIR / "history")


def _list_profiles() -> list[str]:
    """Return stem names of .env files in ~/.kube-q/profiles/."""
    from kube_q.core.config import PROFILES_DIR
    if not PROFILES_DIR.exists():
        return []
    return sorted(p.stem for p in PROFILES_DIR.glob("*.env"))


class _KqCompleter(Completer):
    """Tab-completer for slash commands with argument-aware suggestions.

    Shows command names with inline descriptions, then switches to a
    context-specific list once an argument is being typed:
        /context <TAB>  → kubectl contexts
        /profile <TAB>  → ~/.kube-q/profiles/*.env
        /ns <TAB>       → cluster namespaces (lazily fetched, cached)
        /save <TAB>     → filesystem path completion
    Unknown commands fall through to no suggestions. Argument matching is
    case-insensitive and accepts substrings.
    """

    def __init__(
        self,
        contexts: list[str] | None = None,
        profiles: list[str] | None = None,
        extra_commands: dict[str, str] | None = None,
        namespaces_provider: Any = None,
    ) -> None:
        self._commands: dict[str, str] = dict(_SLASH_COMMANDS)
        if extra_commands:
            for name, desc in extra_commands.items():
                self._commands.setdefault(name, desc or "plugin command")
        self._contexts = sorted(contexts or [])
        self._profiles = sorted(profiles or [])
        self._namespaces_provider = namespaces_provider
        self._ns_cache: list[str] | None = None
        self._path_completer = PathCompleter(expanduser=True)

    def _namespaces(self) -> list[str]:
        if self._ns_cache is None:
            if self._namespaces_provider is None:
                self._ns_cache = []
            else:
                try:
                    self._ns_cache = sorted(self._namespaces_provider() or [])
                except Exception:
                    self._ns_cache = []
        return self._ns_cache

    def get_completions(self, document: Any, complete_event: Any) -> Any:
        text = document.text_before_cursor
        # Only trigger completion when the line starts with a slash command.
        if not text.startswith("/"):
            return
        parts = text.split(maxsplit=1)
        if len(parts) == 1 and not text.endswith(" "):
            # Still typing the command name itself.
            prefix = text.lower()
            for name, desc in self._commands.items():
                if name.startswith(prefix):
                    yield Completion(
                        name,
                        start_position=-len(text),
                        display=name,
                        display_meta=desc,
                    )
            return
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        if cmd == "/save":
            sub = Document(text=arg, cursor_position=len(arg))
            yield from self._path_completer.get_completions(sub, complete_event)
            return
        if cmd == "/context":
            choices, label = self._contexts, "context"
        elif cmd == "/profile":
            choices, label = self._profiles, "profile"
        elif cmd == "/ns":
            choices, label = self._namespaces(), "namespace"
        else:
            return
        arg_l = arg.lower()
        # Prefix matches first, then substring matches (so typing-as-you-go feels right).
        seen: set[str] = set()
        for choice in choices:
            if choice.lower().startswith(arg_l):
                seen.add(choice)
                yield Completion(choice, start_position=-len(arg), display_meta=label)
        if arg_l:
            for choice in choices:
                if choice in seen:
                    continue
                if arg_l in choice.lower():
                    yield Completion(choice, start_position=-len(arg), display_meta=label)


def _make_prompt_session(
    contexts: list[str] | None = None,
    profiles: list[str] | None = None,
    extra_commands: dict[str, str] | None = None,
    namespaces_provider: Any = None,
) -> PromptSession:
    """Return a PromptSession with chat-style key bindings.

    Enter           = send message  (like Slack / ChatGPT).
    Alt+Enter       = insert newline (hold Alt, press Enter).
    Esc → Enter     = insert newline (universal fallback: press Esc, release, press Enter).
    Paste           = multi-line paste always preserved regardless of newlines.
    """
    kb = KeyBindings()

    @kb.add("enter")  # Enter → send
    def _enter_sends(event: Any) -> None:
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")  # Alt+Enter / Esc+Enter → newline
    def _alt_enter_newline(event: Any) -> None:
        event.current_buffer.insert_text("\n")

    completer = _KqCompleter(
        contexts=contexts,
        profiles=profiles,
        extra_commands=extra_commands,
        namespaces_provider=namespaces_provider,
    )
    return PromptSession(
        history=FileHistory(_HISTORY_FILE),
        completer=completer,
        complete_while_typing=True,
        multiline=True,
        key_bindings=kb,
    )
