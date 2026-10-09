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

    An Event predicate with neither `reason_regex` nor `message_regex` is the Event-channel form
    of the same mistake: `WatchPredicate.matches` treats an absent regex as "matches anything",
    so it fires on EVERY Warning event (of `involved_kind`, when set) — routine BackOff, Unhealthy
    and FailedScheduling noise included. No shipped playbook has this shape; every shipped Event
    predicate names a reason. It is not an intended catch-all, so it is named here.
    """
    if pred.kind == "Event" and pred.reason_regex is None and pred.message_regex is None:
        scope = (f"every Warning event about a {pred.involved_kind}" if pred.involved_kind
                 else "every Warning event on the cluster")
        return [
            f"an Event predicate with no reason_regex and no message_regex matches {scope} — "
            "it fires on routine noise, not on a fault. Name the reason (and, if needed, the "
            "message) the failure produces."
        ]
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


# ── Instant PromQL predicates (#20) ─────────────────────────────────────────────────────────────
# A `promql:` entry fires on every element of its instant result vector (Prometheus alerting-rule
# semantics, see `engine.DetectorEngine.evaluate_promql`). There is no PromQL parser in this
# workspace, so this is a static pre-check, not a grammar: it refuses the shapes that provably
# cannot fire, fire on everything, or cost an unbounded query on every evaluation interval. The
# authoritative syntax check is Prometheus itself — the authoring endpoint runs each query once
# (`authoring.promql_probe_errors`) before anything is stored.

#: Longest expression accepted. The longest shipped query is under 200 characters.
MAX_PROMQL_LENGTH = 2000
#: Longest range selector accepted. The query runs on every evaluation interval; a `[365d]`
#: window is a load test on Prometheus, not a detector. The shipped queries use at most 15m.
MAX_PROMQL_RANGE_SECONDS = 24 * 3600

_PROMQL_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`[^`]*`')
_PROMQL_RANGE_RE = re.compile(r"\[([^\]]*)\]")
_PROMQL_DURATION_RE = re.compile(r"^(?:\d+(?:ms|[smhdwy]))+$")
_PROMQL_DURATION_PART_RE = re.compile(r"(\d+)(ms|[smhdwy])")
_PROMQL_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800,
                        "y": 31536000}
_PROMQL_BOOL_RE = re.compile(r"(?:==|!=|>=|<=|>|<)\s*bool\b")
_PROMQL_BARE_RANGE_RE = re.compile(r"\]\s*(?:offset\s+-?\S+\s*)?$")


def _promql_duration_seconds(text: str) -> float | None:
    text = text.strip()
    if not _PROMQL_DURATION_RE.match(text):
        return None
    return sum(int(n) * _PROMQL_UNIT_SECONDS[u]
               for n, u in _PROMQL_DURATION_PART_RE.findall(text))


def promql_shape_errors(expr: object) -> list[str]:
    """Reasons the instant PromQL predicate `expr` cannot work as a detector. Empty ⇒ it can.

    Structural checks run on the expression with its string literals blanked out, so a label
    VALUE containing `[`, `bool` or a bracket never trips them.
    """
    if not isinstance(expr, str):
        return [f"promql entry must be a string, got {type(expr).__name__}"]
    text = expr.strip()
    if not text:
        return ["promql entry is empty — there is no query to run"]
    if len(text) > MAX_PROMQL_LENGTH:
        return [f"promql entry is {len(text)} characters; the limit is {MAX_PROMQL_LENGTH}"]

    errors: list[str] = []
    for label, _op, value in _LABEL_MATCHER_RE.findall(text):
        if _PLACEHOLDER_RE.match(value):
            errors.append(
                f"promql pins {label}={value!r}, which is an unfilled template rather than a "
                f"cluster object — the selector matches no series, so it can never fire"
            )

    # Quotes must close before anything structural can be read.
    stripped = _PROMQL_STRING_RE.sub('""', text)
    if any(q in stripped.replace('""', "") for q in ('"', "'", "`")):
        return [*errors, f"promql {text[:80]!r} has an unterminated string literal"]

    stack: list[str] = []
    opener = {")": "(", "]": "[", "}": "{"}
    for ch in stripped:
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}" and (not stack or stack.pop() != opener[ch]):
            return [*errors, f"promql {text[:80]!r} has unbalanced brackets"]
    if stack:
        return [*errors, f"promql {text[:80]!r} has unbalanced brackets"]

    for inner in _PROMQL_RANGE_RE.findall(stripped):
        # `[5m]` is a range, `[5m:1m]` a subquery (range:resolution). Either way the range part
        # must be a duration: `[]` is not a range at all.
        seconds = _promql_duration_seconds(inner.split(":", 1)[0])
        if seconds is None:
            errors.append(
                f"promql range selector [{inner}] has no valid duration (write e.g. [5m]) — "
                "Prometheus would reject the query"
            )
        elif seconds > MAX_PROMQL_RANGE_SECONDS:
            errors.append(
                f"promql range selector [{inner}] spans more than 24h — it is re-run on every "
                "evaluation interval; narrow it"
            )

    # `metric[5m]` with no function around it answers an instant query with a MATRIX: one window
    # of samples per series, not a current condition. Nothing could fire on it.
    if _PROMQL_BARE_RANGE_RE.search(stripped):
        errors.append(
            "promql ends in a bare range selector, so an instant query returns a range vector "
            "with no current value to fire on — wrap it in a function such as max_over_time(...)"
        )

    # `x > bool 1` keeps EVERY series (value 0 or 1) instead of filtering, and a detector fires
    # on every element of the result — so this fires on every object the metric covers.
    if _PROMQL_BOOL_RE.search(stripped):
        errors.append(
            "promql uses a `bool` comparison modifier, which returns every series (0 or 1) "
            "instead of only the matching ones — a detector fires on every element of the "
            "result, so it would fire on every object the metric covers. Drop `bool`."
        )
    return errors
