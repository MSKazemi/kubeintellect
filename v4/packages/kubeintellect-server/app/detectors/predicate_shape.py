r"""Liveness checks for a compiled watch predicate — can it fire on a real cluster?

`parse_detect_block` answers *"is this a well-formed predicate"*; it compiles the regexes and
stops there. That is not the same question as *"can this predicate ever match an observation"*,
and the gap between them is where dead detectors live. #114 shipped one by hand:
`"^(FailedGetResourceMetric | FailedComputeMetricsReplicas)$"` compiles, loads, counts toward
the detector total and passes the schema check, but an event `reason` never contains a space,
so it was a permanent no-op. Three more shapes fail the same way -- a `kind` the engine does
not handle, a `kind` in the wrong case, and a Pod/Node predicate with no `status_regex` (see
`WatchPredicate.matches`, which returns False for all of them, always).

That matters most on the NL-authoring path (ADR-012), where the predicate is written from prose
by a model rather than by a person reading the schema.

`enumerate_samples` expands a pattern into every string it can produce -- every branch, not
just one. Asserting that *some* string matches is useless: the string is generated from the
pattern, so a stray space is in the language and the sample carries it. The useful question is
whether every string it can produce is a value the cluster actually emits.
"""
from __future__ import annotations

import itertools
import re

try:                                    # 3.11+ exposes the parser here
    from re import _parser as _re_parser  # type: ignore[attr-defined]
except ImportError:                     # pragma: no cover - older interpreters
    import sre_parse as _re_parser      # type: ignore[no-redef]

# The engine's `matches()` handles exactly these, case-sensitively.
SUPPORTED_KINDS = ("Pod", "Event", "Node")

# What the two identifier-shaped fields may actually contain on a real cluster.
# `reason` is a CamelCase identifier; a pod `status` adds the `Init:0/1` form.
# `message_regex` has no shape rule — an event message is free prose.
LEGAL_REASON = re.compile(r"^[A-Za-z0-9._-]+$")
LEGAL_STATUS = re.compile(r"^[A-Za-z0-9:/._-]+$")

_SAMPLE_CAP = 500


class UnsupportedPattern(RuntimeError):
    """A construct `enumerate_samples` cannot expand.

    Raised rather than silently returning a harmless answer: a caller that wants to tolerate
    exotic patterns must say so by catching this, so the decision is visible at the call site.
    """


def enumerate_samples(pattern: re.Pattern) -> list[str]:
    """Every string `pattern` can produce, or raise UnsupportedPattern."""

    def _product_size(parts: list[list[str]]) -> int:
        total = 1
        for part in parts:
            total *= max(len(part), 1)
        return total

    def _class_members(items) -> list[str]:
        out: list[str] = []
        for op, arg in items:
            name = str(op)
            if name == "LITERAL":
                out.append(chr(arg))
            elif name == "RANGE":
                out.append(chr(arg[0]))               # one representative per range
            elif name == "CATEGORY":
                member = {"CATEGORY_DIGIT": "1", "CATEGORY_WORD": "a",
                          "CATEGORY_SPACE": " "}.get(str(arg))
                if member is None:
                    raise UnsupportedPattern(f"class {arg} in {pattern.pattern!r}")
                out.append(member)
            elif name == "NEGATE":
                raise UnsupportedPattern(f"negated class in {pattern.pattern!r}")
            else:
                raise UnsupportedPattern(f"class {name} in {pattern.pattern!r}")
        if not out:
            raise UnsupportedPattern(f"empty class in {pattern.pattern!r}")
        return out

    def expand(parsed) -> list[str]:
        parts: list[list[str]] = []
        for op, arg in parsed:
            name = str(op)
            if name == "AT":                          # ^ $ \b — contribute nothing
                continue
            if name == "LITERAL":
                parts.append([chr(arg)])
            elif name == "ANY":
                parts.append(["x"])
            elif name == "IN":
                parts.append(_class_members(arg))
            elif name == "BRANCH":
                _, branches = arg
                parts.append([s for b in branches for s in expand(b)])
            elif name == "SUBPATTERN":
                parts.append(expand(arg[3]))
            elif name in ("MAX_REPEAT", "MIN_REPEAT"):
                lo, _hi, sub = arg
                once = [s * max(lo, 1) for s in expand(sub)]
                # An optional group can also contribute nothing — check both worlds.
                parts.append(["", *once] if lo == 0 else once)
            else:
                raise UnsupportedPattern(f"{name} in {pattern.pattern!r}")
            if _product_size(parts) > _SAMPLE_CAP:
                raise UnsupportedPattern(f"over {_SAMPLE_CAP} samples for {pattern.pattern!r}")
        return ["".join(combo) for combo in itertools.product(*parts)] or [""]

    return expand(_re_parser.parse(pattern.pattern))


def predicate_liveness_errors(pred, *, strict: bool = False) -> list[str]:
    """Reasons `pred` can never match an observation. Empty list ⇒ it can fire.

    `strict=False` (the validator's setting) treats a pattern the enumerator cannot expand as
    *unknown*, not dead — refusing an author's valid-but-exotic regex would be worse than the
    gap it closes. `strict=True` re-raises, for a gate over the shipped playbooks where an
    exotic pattern deserves a human look rather than a silent pass.
    """
    errors: list[str] = []

    if pred.kind not in SUPPORTED_KINDS:
        return [
            f"kind {pred.kind!r} is never matched by the engine (it handles "
            f"{', '.join(SUPPORTED_KINDS)}, case-sensitively), so this predicate can never fire"
        ]

    if pred.kind in ("Pod", "Node") and pred.status_regex is None:
        errors.append(
            f"a {pred.kind} predicate without status_regex can never fire — "
            "matches() has nothing to test"
        )

    for field, regex, legal in (("status", pred.status_regex, LEGAL_STATUS),
                                ("reason", pred.reason_regex, LEGAL_REASON)):
        if regex is None:
            continue
        try:
            samples = enumerate_samples(regex)
        except UnsupportedPattern:
            if strict:
                raise
            continue
        illegal = [s for s in samples if not legal.match(s)]
        if illegal:
            errors.append(
                f"{field}_regex {regex.pattern!r} can only be satisfied by {illegal[0]!r}, "
                f"which is not a legal Kubernetes {field} (identifier-shaped, no spaces) — "
                "this predicate can never fire"
            )

    return errors
