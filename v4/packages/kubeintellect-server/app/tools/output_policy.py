"""The lines in a tool result that are *about* the result rather than part of it.

Two shapes, both written by the tools themselves: a `[Protected]` refusal or withheld-namespace
sentence from `namespace_guard`, and a truncation marker a tool appended after capping its own
output. Both live at the **end** of a listing — which is exactly where every downstream bound
cuts — and neither looks like an "important row" to any keep-pattern. So every layer between a
tool and a model has to carry them across its own trim; otherwise the guarantee the tool just
made is one that only its own return value keeps, and the model reads a short listing as a
complete one.

The regex is shared rather than copied because the two layers that need it (`coordinator.
_trim_tool_output` and `cortex.graph._bound_tool_content`) are eight hundred lines and one
package apart, and a pair of drifting copies of *this* predicate fails in the one direction
that is silent.
"""

import re

# A line that says something about the result: `namespace_guard` opens its notices with
# `[Protected]`, and a tool that capped itself writes `[truncated: N chars omitted …]`
# (`kubectl_tool` line 1716) with no `[Protected]` in it. Neither is a superset of the other.
POLICY_LINE_RE = re.compile(r"\[Protected\]|\[truncated|\[unavailable\]", re.IGNORECASE)


def split_policy_lines(content: str) -> tuple[str, str]:
    """Split `content` into (body, policy-lines).

    `policy` is stripped and may be empty; `body` keeps its line endings, so a result carrying
    no policy line comes back byte-identical and a caller that re-joins the two changes nothing
    about the ordinary case.
    """
    lines = content.splitlines(keepends=True)
    policy = "".join(ln for ln in lines if POLICY_LINE_RE.search(ln))
    body = "".join(ln for ln in lines if not POLICY_LINE_RE.search(ln))
    return body, policy.strip()

# ── The vocabulary ────────────────────────────────────────────────────────────
# The exact strings a model is told to read as "this output is incomplete". Everything that
# shortens output must emit one of them, and every prompt that receives shortened output must
# name them. Both halves live here because they drifted apart once already: on 2026-08-24 the
# coordinator's trimmer emitted "chars trimmed" while the instruction four hundred lines up the
# same file named "[truncated" and "chars omitted" — a warning nobody was looking for.
MARKER_PATTERNS = ("[truncated", "chars omitted")

# Handed to every model that reads tool output. One copy, so a route cannot get the vocabulary
# without the instruction, or the instruction without the vocabulary.
TRUNCATION_CLAUSE = """IMPORTANT — Truncated output:
  If any tool output contains a truncation marker (text like "[truncated" or "chars omitted"),
  you MUST include a visible warning in your response, for example:
  "> ⚠️ Output was truncated — use narrower filters (e.g. `-n <namespace>`, `-l <label>`, `--tail`) to see the full result."
  Never silently drop this warning. The user must know the list is incomplete."""


def truncation_marker(omitted: int, unit: str = "chars", hint: str = "") -> str:
    """The one shape a truncation marker takes anywhere in this codebase.

    `unit` is what was lost — chars, rows, lines — because "I see 30 of 200 rows" and "my last
    row is cut in half" are different losses, and a reader told the wrong one narrows the wrong
    thing. The result always contains `MARKER_PATTERNS[0]`, and with the default unit both of
    them, so a caller cannot produce a marker the instruction above does not cover.
    """
    tail = f" — {hint}" if hint else ""
    return f"[truncated: {omitted} {unit} omitted{tail}]"


# The same fact, for a tier that must not print anything. Triage answers in strict JSON, so
# telling it to "include a visible warning" would be an instruction to corrupt its own output.
# What it needs from a partial context is the inference rule, not the phrasing.
PARTIAL_CONTEXT_CLAUSE = """Context completeness:
  Text containing "[truncated" or "[Protected]" is PARTIAL. Absence of a resource from partial
  context is not evidence that it does not exist or that the cluster is healthy — treat it as
  unknown and prefer "investigate" over answering from the snapshot alone."""


# ── "Do not call me again" ────────────────────────────────────────────────────
# `_GATHER_SYSTEM` has told the model since V4 to stop calling a tool that "replies that it is
# not configured or unavailable". Measured 2026-08-24 by driving all eight paths: only the two
# "URL is unset" replies contained either word. A missing binary, an unreachable cluster and a
# refused backend connection — the three cases where a retry provably cannot succeed — carried
# neither, so the instruction had no trigger exactly where it mattered most.
UNAVAILABLE_MARKER = "[unavailable]"


def unavailable_notice(reason: str) -> str:
    """The one shape a "this tool cannot answer in this session" reply takes."""
    return f"{UNAVAILABLE_MARKER} {reason.rstrip()} Retrying this tool will not change that."


def mark_unavailable(text: str, reason: str = "") -> str:
    """Append the notice to a tool's own error text as a trailing line.

    A trailing line, never a wrapper — every other reader of these replies keys on how they
    *start*. The ACI read verbs decide "refusal, not content" from a leading `[Error]`, so a
    marker prepended to that text turned a refused read into a successful one: measured
    2026-08-24, when `test_read_verb_refusal_is_not_content` caught exactly that. It also puts
    this marker where `[Protected]` and `[truncated` already sit, which is where the trims that
    carry policy lines expect to find it.
    """
    return f"{text.rstrip()}\n{unavailable_notice(reason or text)}"


RETRY_CLAUSE = """Unavailable tools:
  A tool result containing "[unavailable]" means that tool cannot answer at all right now — its
  backend is not configured, its binary is missing, or the endpoint cannot be reached. Do NOT
  call it again this turn. Say plainly what is missing and answer from what the other tools
  provide; do not present its silence as evidence about the cluster."""
