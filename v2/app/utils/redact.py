"""Secret/PII redaction for stored reflexion outcomes.

K8s manifests and kubectl commands routinely contain credentials, tokens,
internal hostnames, and bearer tokens. Anything that lands in the database
must pass through this redactor. The goal is defensive: we'd rather lose
useful context than leak a credential into a learned pattern.

Heuristic-based — full enumeration of secret formats is impossible. This
catches the common cases. Reviewers should still grep stored data for
patterns we missed.
"""
from __future__ import annotations

import re

# Lines containing any of these tokens (case-insensitive) are dropped wholesale.
_SECRET_LINE_KEYWORDS = (
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "token",
    "bearer",
    "authorization",
    "credentials",
    "client_secret",
    "private_key",
    "ssh_key",
)

# Internal hostnames in URLs are replaced with a sentinel. Keeps the
# protocol+path so the structural shape is preserved for diffing.
_URL_RE = re.compile(r"\b(https?)://[a-zA-Z0-9.\-_]+(?::\d+)?", re.IGNORECASE)

# Common bearer/JWT/long-base64 token shapes — replaced inline.
_TOKEN_RE = re.compile(
    r"\b(?:eyJ[a-zA-Z0-9_\-\.]{20,}|[a-zA-Z0-9_\-]{32,})\b"
)

# Field-style assignments inside strings: `password: foo`, `token=foo`.
_KV_RE = re.compile(
    r"(?i)\b(password|passwd|secret|api[_-]?key|token|bearer|authorization|client[_-]?secret|private[_-]?key)"
    r"\s*[:=]\s*\S+",
)


def redact_secrets(text: str | None, *, max_chars: int = 1500) -> str:
    """Return a redacted copy of `text`. Deterministic; safe on all inputs.

    - Lines containing any keyword in _SECRET_LINE_KEYWORDS are dropped.
    - Inline `key: value` patterns are replaced with `key: <redacted>`.
    - Long token-shaped strings are replaced with `<redacted-token>`.
    - URLs are stripped of host and port.
    - Output is hard-capped at max_chars with a `[...]` truncation marker.
    """
    if not text:
        return ""

    out_lines: list[str] = []
    for line in text.splitlines():
        lower = line.lower()
        if any(kw in lower for kw in _SECRET_LINE_KEYWORDS):
            # Try to keep the line shape but redact the value.
            redacted = _KV_RE.sub(lambda m: f"{m.group(1)}: <redacted>", line)
            if redacted == line:
                # Couldn't redact in place — drop the line entirely.
                out_lines.append("# <redacted-line>")
                continue
            line = redacted
        line = _URL_RE.sub(lambda m: f"{m.group(1)}://<redacted-host>", line)
        line = _TOKEN_RE.sub("<redacted-token>", line)
        out_lines.append(line)

    result = "\n".join(out_lines)
    if len(result) > max_chars:
        result = result[: max_chars - 5] + "[...]"
    return result
