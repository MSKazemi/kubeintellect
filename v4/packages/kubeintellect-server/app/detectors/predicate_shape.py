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


# ── Predicates that fire on a healthy object ────────────────────────────────────────────────────
# The mirror image of a dead predicate, and it went unrefused for exactly as long. A dead
# predicate contributes silence; one that matches a HEALTHY status contributes a finding about
# every object of its kind on the cluster, for ever.
#
# `nl:soak-cpu-saturated`, authored from the prose "a workload is pinned at its CPU limit",
# compiled to `{kind: Pod, status_regex: '^Running$'}`. `WatchPredicate` has no namespace or
# label scope — `matches()` tests the status and nothing else — so that predicate matches every
# healthy pod on the cluster, and the trend predicate that carried the actual CPU condition is
# evaluated on a separate loop and OR'd, never AND'd. On the F3 soak cluster its ring held 46
# findings before any fault was injected, every one of them `kube-system/coredns-…` with
# `evidence: "pod status=Running"`.
#
# The authoring and review gates refuse this. The ENGINE deliberately does not: refusing at load
# would delete the evidence that the detector is wrong, which is a mistake this codebase has
# already made once — the round-two liveness gate dropped whole detectors and improved the
# measured result by removing the rows that falsified it. The engine records it instead.

#: What the observer emits for an object in a normal steady state.
#: Pod: `pod_display_status` returns `Running` for a healthy pod, `Completed` for a container that
#: exited cleanly, and `Succeeded` for a finished pod with no container statuses.
#: Node: `Ready`.
HEALTHY_STATUS = {
    "Pod": ("Running", "Completed", "Succeeded"),
    "Node": ("Ready",),
}


def predicate_health_errors(pred) -> list[str]:
    """Reasons `pred` fires on objects that are FINE. Empty list ⇒ it does not.

    Deliberately narrow, and deliberately not a guess: it asks the predicate the same question
    the engine will — `status_regex.search(status)` — against the statuses the observer emits for
    a healthy object. Nothing here reasons about whether a detector is a *good* one.
    """
    if pred.kind not in HEALTHY_STATUS or pred.status_regex is None:
        return []
    hits = [s for s in HEALTHY_STATUS[pred.kind] if pred.status_regex.search(s)]
    if not hits:
        return []
    return [
        f"status_regex {pred.status_regex.pattern!r} matches {', '.join(hits)}, which is what "
        f"the observer emits for a HEALTHY {pred.kind} — this predicate fires on every "
        f"{pred.kind.lower()} on the cluster, not on a fault. A {pred.kind} predicate has no "
        f"namespace or label scope, so there is no way to narrow it."
    ]


# ── Trend predicates ────────────────────────────────────────────────────────────────────────────
# `predicate_liveness_errors` covers watch predicates only, and that gap shipped dead detectors.
# Two of the eight NL-authored detectors on the F3 soak cluster were forecasts over
# `kube_deployment_status_replicas{deployment="your-deployment-name"}` and
# `{deployment="your_service_name"}` — the model returned the *template* rather than filling it
# in, and the template was accepted, stored, listed as `shadow` and offered for promotion. A
# PromQL selector pinned to a series name that does not exist returns no samples, `project_eta`
# gets fewer than two points, and the detector's zero firings read exactly like "the condition
# never occurred".

# Deliberately narrow. A false positive here refuses an author's *valid* detector, which is worse
# than the gap it closes, so this matches only strings that cannot plausibly be a real Kubernetes
# object name: the `your-`/`your_` template form both live cases used, and the four templating
# syntaxes. Words like `example`, `foo` or `test` are NOT listed — they are perfectly ordinary
# namespace and deployment names, and guessing at intent is how a validator starts lying.
_PLACEHOLDER_RE = re.compile(
    r"^(?:"
    r"your[-_].*"                                   # your-deployment-name, your_service_name
    r"|<[^>]*>"                                     # <name>
    r"|\{\{.*\}\}"                                 # {{ name }}
    r"|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?"              # $NAME / ${NAME}
    r"|(?:CHANGE_?ME|REPLACE_?ME|PLACEHOLDER|TODO|FIXME)"
    r")$",
    re.IGNORECASE,
)

# Label matchers inside a PromQL selector: name, operator, quoted value.
_LABEL_MATCHER_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*\"([^\"]*)\"")

VALID_DIRECTIONS = ("rising", "falling")


def trend_liveness_errors(trend) -> list[str]:
    """Reasons `trend` can never fire. Empty list ⇒ it can.

    Every check here is a *provable* impossibility read off `engine.project_eta` and its caller,
    which fire only when `r2 >= min_r2` and `0 < eta_minutes <= min(projection_horizon_minutes,
    fire_if_eta_within_minutes)`. Nothing here guesses at whether a forecast is a *good* one —
    a counter used as a level, say — because that depends on runtime values this cannot see.
    """
    errors: list[str] = []

    metric = (trend.metric or "").strip()
    if not metric:
        errors.append("trend predicate has no metric — there is nothing to project")
        return errors

    for label, _op, value in _LABEL_MATCHER_RE.findall(metric):
        if _PLACEHOLDER_RE.match(value):
            errors.append(
                f"trend metric pins {label}={value!r}, which is an unfilled template rather than "
                f"a cluster object — the selector matches no series, so this predicate can never "
                f"fire ({metric!r})"
            )

    # r2 is a squared correlation coefficient: it lies in [0, 1] by construction.
    if trend.min_r2 > 1.0:
        errors.append(
            f"min_r2={trend.min_r2} is above 1.0 and r2 cannot exceed 1.0, so the fit check "
            "rejects every series — this predicate can never fire"
        )

    # The caller requires eta_minutes > 0 AND <= both bounds; a non-positive bound excludes
    # every value eta can take.
    for field_name, value in (("fire_if_eta_within_minutes", trend.fire_if_eta_within_minutes),
                              ("projection_horizon_minutes", trend.projection_horizon_minutes)):
        if value <= 0:
            errors.append(
                f"{field_name}={value} excludes every projected ETA (the engine requires "
                "0 < eta <= this) — this predicate can never fire"
            )

    if trend.window_minutes <= 0:
        errors.append(
            f"window_minutes={trend.window_minutes} asks for an empty lookback, so the "
            "regression never gets the two samples it needs — this predicate can never fire"
        )

    # Not an impossibility — it is worse. `project_eta` treats anything that is not exactly
    # "falling" as rising, so a typo does not fail, it silently inverts the author's intent.
    if trend.direction not in VALID_DIRECTIONS:
        errors.append(
            f"direction={trend.direction!r} is not one of {', '.join(VALID_DIRECTIONS)}; the "
            "engine would silently treat it as 'rising', which is the opposite condition half "
            "the time — say which one you mean"
        )

    return errors
