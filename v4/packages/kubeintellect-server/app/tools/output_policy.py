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
POLICY_LINE_RE = re.compile(r"\[Protected\]|\[truncated", re.IGNORECASE)


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
