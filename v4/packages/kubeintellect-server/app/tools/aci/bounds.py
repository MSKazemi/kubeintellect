"""Normalization & output-window helpers for the K8s-ACI v0 verbs (v5 specs/01).

These are pure functions (no cluster access) so they are fully unit-testable.
"""

from __future__ import annotations

import re

from app.tools.kubectl_tool import _READ_ONLY_VERBS, _extract_verb

# Noise keys stripped from KRM YAML/JSON before the model sees it (R-aci-bound-04).
_NOISE_LINE = re.compile(
    r"^\s*(managedFields:|resourceVersion:|uid:|generation:|creationTimestamp:|"
    r"kubectl\.kubernetes\.io/last-applied-configuration:)",
)
# A managedFields block is multi-line; drop until dedent. Handled line-wise below.


def is_read_only(command: str) -> bool:
    """True iff the kubectl subcommand is in run_kubectl's read-only verb set.

    ``diff`` and ``rollout`` are already read-only members of that set, so the
    ACI diff_change verb passes this guard (R-aci-wrap-02).
    """
    try:
        tokens = command.split()
        # _extract_verb expects "kubectl" at tokens[0] and returns tokens[1];
        # tolerate a command given without the leading "kubectl".
        if tokens and tokens[0] != "kubectl":
            tokens = ["kubectl", *tokens]
        verb = _extract_verb(tokens)
    except Exception:
        return False
    return verb in _READ_ONLY_VERBS


def normalize_krm(text: str, view: str) -> str:
    """Strip server-noise keys from KRM output and project per ``view``.

    ``summary`` and ``status`` currently apply the same noise-stripping; the
    finer status-only projection is deferred (kept honest — see spec non-goals).
    ``full`` returns the (still noise-stripped) object verbatim.
    """
    out_lines: list[str] = []
    skipping_block_indent: int | None = None
    for line in text.splitlines():
        indent = len(line) - len(line.lstrip(" "))
        if skipping_block_indent is not None:
            # Inside a dropped multi-line block (e.g. managedFields:). Keep
            # skipping blank lines, more-indented lines, and same-indent
            # sequence items ("- ..." under the dropped key). Stop at the next
            # sibling mapping key (same-or-lower indent, not a "- " item).
            stripped = line.lstrip(" ")
            if (
                line.strip() == ""
                or indent > skipping_block_indent
                or (indent == skipping_block_indent and stripped.startswith("- "))
            ):
                continue
            skipping_block_indent = None
        if _NOISE_LINE.match(line):
            # If this key opens a block (nothing after the colon), skip its body too.
            if line.rstrip().endswith(":"):
                skipping_block_indent = indent
            continue
        out_lines.append(line)
    return "\n".join(out_lines)


def window(
    body: str,
    max_lines: int,
    max_chars: int,
    offset: int = 0,
) -> tuple[str, int, int, int | None]:
    """Return ``(trimmed_body, total_lines, shown_lines, next_cursor)``.

    Enforces the line window first, then the char cap — never splitting a line
    mid-way (R-aci-bound-01..03). ``next_cursor`` is the offset to resume from
    when output was truncated, else ``None``.
    """
    lines = body.splitlines()
    total = len(lines)
    take = lines[offset : offset + max_lines]
    shown = len(take)
    trimmed = "\n".join(take)

    truncated_by_lines = offset + shown < total

    # Char cap: drop whole trailing lines until under the cap.
    if len(trimmed) > max_chars:
        while take and len("\n".join(take)) > max_chars:
            take.pop()
        shown = len(take)
        trimmed = "\n".join(take)
        truncated_by_lines = True

    next_cursor = offset + shown if truncated_by_lines and shown > 0 else None
    if truncated_by_lines and shown == 0:
        # The single line at the cursor exceeded the char cap: hard-cut THAT line
        # (not the document head — matters when paginating with offset>0) so we
        # never emit "". offset < total is guaranteed here (truncated_by_lines).
        trimmed = lines[offset][:max_chars]
        shown = 1
        next_cursor = None
    return trimmed, total, shown, next_cursor


def empty_message(verb: str, target: str) -> str:
    """Explicit never-silent message for a zero-result-but-successful run."""
    return f"[aci:{verb}] {target} — ran successfully, no matching results."
