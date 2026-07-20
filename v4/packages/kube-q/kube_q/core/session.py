"""
session.py — Pure session state and file attachment resolution for kube_q.core.

No UI dependencies. Consumed by both CLI and future web/IDE renderers.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from kube_q.core.config import CONFIG_DIR

# ── File attachment (@filename resolution) ───────────────────────────────────

_ATTACH_RE = re.compile(r'@((?:"[^"]*"|\'[^\']*\'|\S+))')
_MAX_ATTACH_BYTES = 100 * 1024  # 100 KB

_EXT_LANG: dict[str, str] = {
    ".yaml": "yaml",  ".yml": "yaml",
    ".json": "json",
    ".py":   "python",
    ".sh":   "bash",  ".bash": "bash", ".zsh": "bash",
    ".toml": "toml",
    ".go":   "go",
    ".js":   "javascript",  ".ts": "typescript",
    ".rs":   "rust",
    ".java": "java",
    ".xml":  "xml",
    ".html": "html",  ".htm": "html",
    ".tf":   "hcl",
    ".md":   "markdown",
    ".txt":  "",  ".log": "",  ".env": "",
}


def resolve_attachments(text: str) -> tuple[str, list[str], list[str]]:
    """Replace @filename tokens with their file contents as fenced code blocks.

    Supports bare paths (@file.yaml) and quoted paths (@"my file.yaml").
    Returns (expanded_text, attached_summaries, error_messages).
    """
    attached: list[str] = []
    errors: list[str] = []

    def _expand(match: re.Match) -> str:
        raw = match.group(1).strip("\"'")
        path = Path(raw).expanduser().resolve()

        if not path.exists():
            errors.append(f"[red]@{raw}:[/red] file not found")
            return match.group(0)
        if not path.is_file():
            errors.append(f"[red]@{raw}:[/red] not a regular file")
            return match.group(0)

        size = path.stat().st_size
        if size > _MAX_ATTACH_BYTES:
            errors.append(
                f"[red]@{raw}:[/red] file too large "
                f"({size // 1024} KB — limit is {_MAX_ATTACH_BYTES // 1024} KB)"
            )
            return match.group(0)

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            errors.append(f"[red]@{raw}:[/red] could not read: {e}")
            return match.group(0)

        lang = _EXT_LANG.get(path.suffix.lower(), "")
        attached.append(f"{path.name} ({size / 1024:.1f} KB)")
        return f"\n```{lang}\n# {path.name}\n{content.rstrip()}\n```\n"

    expanded = _ATTACH_RE.sub(_expand, text)
    return expanded, attached, errors


# ── User-ID persistence ───────────────────────────────────────────────────────

_USER_ID_FILE = str(CONFIG_DIR / "user-id")


def load_or_create_user_id(explicit: str | None = None) -> str:
    """Return user_id: explicit arg > persisted ~/.kube-q/user-id > generate+save new."""
    if explicit:
        with open(_USER_ID_FILE, "w") as f:
            f.write(explicit)
        os.chmod(_USER_ID_FILE, 0o600)
        return explicit
    if os.path.exists(_USER_ID_FILE):
        with open(_USER_ID_FILE) as f:
            uid = f.read().strip()
        if uid:
            return uid
    uid = f"cli-user-{str(uuid.uuid4())[:8]}"
    with open(_USER_ID_FILE, "w") as f:
        f.write(uid)
    os.chmod(_USER_ID_FILE, 0o600)
    return uid


# ── Session state ─────────────────────────────────────────────────────────────

@dataclass
class SessionState:
    """Holds all mutable state for one interactive session or demo run."""
    conversation_id: str
    user_id: str
    messages: list[dict] = field(default_factory=list)
    hitl_pending: bool = False
    pending_action_id: str | None = None
    current_namespace: str | None = None
    current_context: str | None = None
    last_request_id: str | None = None
