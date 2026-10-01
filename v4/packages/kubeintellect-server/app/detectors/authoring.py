"""Natural-language detector authoring (ADR-012).

A human describes a failure in plain English; an LLM compiles it into a `detect:`
block, which is validated against the existing schema and then checked for liveness --
compiling is not the same as being able to fire (`predicate_shape`) -- and staged as a
SHADOW candidate in the `detectors` table. Shadow
detectors observe and accrue precision but never reach the watchtower until a
human promotes them (see `review.promote_candidate`).

Compilation is a one-time authoring-time LLM call; the resulting detector runs
zero-token like every other detector.

Two field-campaign defects shaped what follows (ADR-012 implementation notes):

* **A compiled detector that could not fire was reported as a success.** One run returned a
  detector with zero predicates and `errors: []`; another kept a PromQL predicate that is recorded
  but never evaluated. `validate_detect_block` therefore checks the model's output against the
  engine's own schema *before* parsing -- an entry the parser would silently drop, a field or key
  the engine has no reader for, a knob the parser would replace with its default -- and refuses
  `promql` outright, because nothing evaluates it.
* **Compilation was not repeatable.** The same eight descriptions compiled 47 minutes apart gave
  different predicate counts on four of them. Every compilation is now requested at temperature
  0, and stored with its provenance (`compilation`: description digest, model, temperature, time)
  next to the predicates. Re-reading, loading or promoting a detector never recompiles -- the
  engine parses the stored block -- and resubmitting identical prose reuses the stored compilation
  unless the caller explicitly asks for a fresh one, in which case the response says so.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import time

from app.detectors.models import (
    EVALUATED_PREDICATE_KEYS,
    DetectBlock,
    TrendPredicate,
    WatchPredicate,
    parse_detect_block,
)
from app.detectors.predicate_shape import (
    predicate_health_errors,
    predicate_liveness_errors,
    trend_liveness_errors,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

_AUTHORING_SYSTEM = """You compile a Kubernetes failure description into a detector `detect:` block.

Output ONLY a JSON object with any of these keys (omit those you don't need):
- "watch_predicates": list of {kind: "Pod"|"Event"|"Node",
    status_regex (Pod/Node, matched against the STATUS column),
    reason_regex, message_regex (Event; Warning events only),
    involved_kind (optional)}
- "trend_predicates": list of {metric (range PromQL), threshold (number),
    window_minutes, projection_horizon_minutes, fire_if_eta_within_minutes,
    direction: "rising"|"falling", min_r2} — for forecasting a slow-burn failure
- "debounce_seconds": integer
- Do NOT emit "promql". PromQL queries are recorded but NOT evaluated by the engine, so a
  detector carrying one is refused. Express a metric condition as a trend_predicate instead.

Hard rules, each one written because a model broke it and the result was stored as a live
detector that could never fire:
- NEVER leave a placeholder in a PromQL selector. If the description does not name a concrete
  deployment/service/namespace, omit the label matcher entirely rather than writing
  {deployment="your-deployment-name"} — an unmatched selector returns no series and the
  detector is silently dead.
- "direction" must be exactly "rising" or "falling". Anything else is read as "rising", so a
  typo asks the opposite question instead of failing.
- "min_r2" is a squared correlation coefficient: it must be in [0, 1].
- A Pod "status_regex" is matched against the STATUS column `kubectl get pods` prints — a
  waiting reason (CrashLoopBackOff, ImagePullBackOff), a terminated reason (OOMKilled, Error,
  Completed), Init:<reason>, Evicted, Terminating, or a bare phase. It is NOT the pod's phase
  alone and NOT an arbitrary word: "NotReady" in particular does NOT mean "the readiness probe
  is failing" (kubectl prints "Running" for that pod) — use an Event predicate on Unhealthy.
- A Pod "status_regex" must NEVER match a HEALTHY status: Running, Completed or Succeeded (nor
  a Node "status_regex" matching Ready). A predicate has no namespace or label scope — it is
  matched against the status and nothing else — so "^Running$" means "fire on every pod on the
  cluster", not "fire on the pod I described". Adding a trend_predicate does not narrow it: the
  two are evaluated by separate loops and OR'd, never AND'd. If the condition is a resource level
  ("pinned at its CPU limit", "memory climbing"), express it as a trend_predicate ALONE and emit
  NO watch_predicates.
- Emit NO other keys and NO other fields than the ones listed above. The output is checked
  against the engine's schema and a key the engine has no reader for is refused, not ignored —
  an ignored field is a condition the detector silently stops checking.

Use anchored, specific RE2 regexes. Examples:
{"watch_predicates": [{"kind": "Pod", "status_regex": "^OOMKilled$"}]}
{"watch_predicates": [{"kind": "Event", "reason_regex": "^BackOff$",
  "message_regex": "Back-off restarting failed container", "involved_kind": "Pod"}]}

Return JSON only — no prose, no code fences."""

#: Every compilation is requested at this sampling temperature, whatever `LLM_TEMPERATURE` says.
#: The rest of the product may run warmer (some models reject 0.0 -- see `config.LLM_TEMPERATURE`),
#: but a compiler whose output changes between two submissions of the same sentence is not a
#: compiler: the same eight descriptions compiled 47 minutes apart gave different predicate counts
#: on four of them. A model that refuses 0.0 makes authoring fail loudly (502) rather than
#: silently compile at a temperature nobody chose. Temperature 0 narrows the variance; it does not
#: guarantee identical output across calls or provider-side model updates, which is why the
#: compilation is also STORED and reused (`find_compilation`).
COMPILE_TEMPERATURE = 0.0

#: The key under which a stored detector carries its compilation provenance. It sits beside the
#: predicates in the `predicate` column; `parse_detect_block` ignores it, and the schema gate
#: refuses it in model output, so a model cannot forge one.
COMPILATION_KEY = "compilation"


class CompilerUnavailable(RuntimeError):
    """The compiler model could not be called at all. Retryable; says nothing about the prose.

    This used to be swallowed into an empty block, which the validator then reported as "no valid
    predicates" -- an LLM outage presented to the author as a fault in their description.
    """


def _authoring_llm():
    """(model, model pinned to `COMPILE_TEMPERATURE` for this call only)."""
    from app.cortex.models import get_specialist_llm

    llm = get_specialist_llm()
    bind = getattr(llm, "bind", None)
    if not callable(bind):
        # Every LangChain chat model has `bind`. Refusing here rather than calling the model
        # unpinned keeps the provenance stored with the detector true.
        raise CompilerUnavailable(
            f"the compiler model {type(llm).__name__} cannot be pinned to "
            f"temperature={COMPILE_TEMPERATURE}"
        )
    return llm, bind(temperature=COMPILE_TEMPERATURE)


def _model_name(llm) -> str | None:
    for attr in ("model_name", "model", "deployment_name"):
        value = getattr(llm, attr, None)
        if isinstance(value, str) and value:
            return value
    return None


def normalize_description(description: str) -> str:
    """Whitespace-insensitive form of the prose: the unit "identical description" is judged on."""
    return " ".join((description or "").split())


def description_digest(description: str) -> str:
    return hashlib.sha256(normalize_description(description).encode("utf-8")).hexdigest()


async def compile_nl_to_detect_block(description: str) -> tuple[dict | None, dict]:
    """LLM-compile a plain-English failure description into a `detect:` mapping.

    Returns `(raw, provenance)`. `raw` is the JSON object the model produced, or None when it
    produced none -- `validate_detect_block` turns either into a precise refusal. Raises
    `CompilerUnavailable` when the model could not be called. That is deliberately NOT folded
    into an empty block any more: "the compiler is down" and "your description compiled to
    nothing" need different answers, and only the first is worth retrying.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from app.utils.redact import redact_secrets

    try:
        base, llm = _authoring_llm()
        resp = await llm.ainvoke(
            [SystemMessage(content=_AUTHORING_SYSTEM), HumanMessage(content=description)]
        )
    except CompilerUnavailable:
        raise
    except Exception as exc:
        reason = redact_secrets(str(exc), max_chars=300)
        logger.warning(f"nl_authoring: compile failed: {reason}")
        raise CompilerUnavailable(f"the compiler model could not be called: {reason}") from exc
    provenance = {
        "description_sha256": description_digest(description),
        "model": _model_name(base),
        "temperature": COMPILE_TEMPERATURE,
        "compiled_at": time.time(),
    }
    return _parse_detect_json(getattr(resp, "content", "") or ""), provenance


def _parse_detect_json(text: str) -> dict | None:
    match = _JSON_RE.search(text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


# ── The schema gate: what the model wrote vs. what the engine would load ─────────────────────────
# `parse_detect_block` is forgiving by design -- it is also the loader for hand-written playbooks
# and for rows written before any gate existed, so it skips an entry it cannot use and swaps an
# invalid knob for its default, with at most a log line. On the authoring path that forgiveness is
# how a detector is reported as compiled while the thing that loads is not the thing that was
# written: a predicate with no `kind` vanishes, a `namespace` field nobody reads narrows nothing,
# `min_r2: 1.5` quietly becomes 0.5. So the gate reads the model's output against the very
# dataclasses the engine builds -- their field names ARE the schema -- and refuses each of those
# differences by name.

_WATCH_FIELDS = tuple(f.name for f in dataclasses.fields(WatchPredicate))
_TREND_FIELDS = tuple(f.name for f in dataclasses.fields(TrendPredicate))
_WATCH_STRING_FIELDS = ("kind", "status_regex", "reason_regex", "message_regex", "involved_kind")
_TREND_KNOBS = ("window_minutes", "projection_horizon_minutes", "fire_if_eta_within_minutes",
                "min_r2")
_KNOWN_KEYS = (*EVALUATED_PREDICATE_KEYS, "debounce_seconds", "promql")


def _schema_errors(raw: dict) -> list[str]:
    errors: list[str] = []
    for key in raw:
        if key not in _KNOWN_KEYS:
            errors.append(
                f"unknown key {key!r} — the engine reads only "
                f"{', '.join(EVALUATED_PREDICATE_KEYS)} and debounce_seconds, so it would be "
                "silently ignored"
            )
    if raw.get("promql"):
        errors.append(
            "promql is recorded but never evaluated — no evaluator is wired for it, so those "
            "queries can never fire; express the condition as a watch or trend predicate"
        )

    for key, allowed, required in (
        ("watch_predicates", _WATCH_FIELDS, ("kind",)),
        ("trend_predicates", _TREND_FIELDS, ("metric", "threshold")),
    ):
        entries = raw.get(key)
        if entries is None:
            continue
        if not isinstance(entries, list):
            errors.append(f"{key} must be a list, got {type(entries).__name__}")
            continue
        for i, entry in enumerate(entries):
            where = f"{key}[{i}]"
            if not isinstance(entry, dict):
                errors.append(f"{where} is not an object, so the engine would drop it")
                continue
            for name in required:
                value = entry.get(name)
                if value is None or (isinstance(value, str) and not value.strip()):
                    errors.append(
                        f"{where} has no {name}, so the engine would drop it and the detector "
                        "would load without it"
                    )
            for name in entry:
                if name not in allowed:
                    errors.append(
                        f"{where} has unknown field {name!r} — the engine has no such field "
                        f"(it reads {', '.join(allowed)}), so that condition would be silently "
                        "dropped"
                    )
            if key == "watch_predicates":
                for name in _WATCH_STRING_FIELDS:
                    if entry.get(name) is not None and not isinstance(entry[name], str):
                        errors.append(f"{where}.{name} must be a string, got {entry[name]!r}")
            elif entry.get("threshold") is not None:
                try:
                    float(entry["threshold"])
                except (TypeError, ValueError):
                    errors.append(f"{where}.threshold={entry['threshold']!r} is not a number")
    return errors


def _fidelity_errors(raw: dict, block: DetectBlock) -> list[str]:
    """Differences between what was compiled and what `parse_detect_block` actually built."""
    errors: list[str] = []
    for key, built in (("watch_predicates", block.watch_predicates),
                       ("trend_predicates", block.trend_predicates)):
        written = raw.get(key) or []
        if len(built) != len(written):
            errors.append(
                f"{key}: {len(written)} written but the engine would load {len(built)} — "
                "the detector that runs would not be the one compiled"
            )
    trends = raw.get("trend_predicates") or []
    if len(trends) == len(block.trend_predicates):
        for i, (entry, loaded) in enumerate(zip(trends, block.trend_predicates, strict=True)):
            for knob in _TREND_KNOBS:
                if entry.get(knob) is None:
                    continue
                wanted: float | None
                try:
                    wanted = float(entry[knob]) if knob == "min_r2" else int(entry[knob])
                except (TypeError, ValueError):
                    wanted = None
                if wanted is None or wanted != getattr(loaded, knob):
                    errors.append(
                        f"trend_predicates[{i}].{knob}={entry[knob]!r} is invalid and the engine "
                        f"would load {getattr(loaded, knob)!r} instead — the detector that runs "
                        "would not be the one compiled"
                    )
    if raw.get("debounce_seconds") is not None:
        try:
            debounce: int | None = int(raw["debounce_seconds"])
        except (TypeError, ValueError):
            debounce = None
        if debounce is None or debounce != block.debounce_seconds:
            errors.append(
                f"debounce_seconds={raw['debounce_seconds']!r} is invalid and the engine would "
                f"load {block.debounce_seconds!r} instead"
            )
    return errors


def deployment_errors(block: DetectBlock) -> list[str]:
    """Reasons this deployment would never evaluate `block`, although the block itself is sound.

    Kept out of `validate_detect_block` because it is a fact about the server's flags, not the
    predicate: the same block is fine where predictive detection is on. The authoring endpoint
    still refuses on it -- staging a detector that nothing evaluates is the silence the F3 soak was
    void for -- and says which flag to change. Same rule as the `watching` field of
    `GET /v1/detectors/{name}/shadow-findings`.
    """
    from app.core.config import settings

    if (block.trend_predicates and not block.watch_predicates
            and not settings.PREDICTIVE_DETECTION_ENABLED):
        return [
            "this detector has only trend predicates and PREDICTIVE_DETECTION_ENABLED is false — "
            "nothing on this deployment evaluates them, so it could never fire here. Enable "
            "predictive detection, or describe a condition a watch predicate can observe."
        ]
    return []


def validate_detect_block(
    raw: dict | None, name: str = "nl"
) -> tuple[DetectBlock | None, list[str]]:
    """Validate a compiled block by running it through the real compiler.

    Returns (block, errors). block is None when the output is empty or not an object, carries
    anything the engine would silently drop, ignore or replace (`_schema_errors`), carries
    `promql` (recorded, never evaluated), nothing valid compiled, a predicate is malformed (e.g. an
    uncompilable regex), a predicate compiles cleanly but provably cannot ever match
    (`predicate_shape.predicate_liveness_errors`), one matches a healthy object and so fires on the
    whole cluster (`predicate_shape.predicate_health_errors`), or the block that would load differs
    from the one written (`_fidelity_errors`). A non-None block always carries at least one
    evaluated predicate.
    """
    if not isinstance(raw, dict):
        return None, ["compiler did not return a JSON object"]
    if not raw:
        return None, ["the compiler returned an empty object — zero predicates, so this detector "
                      "could never fire"]
    # The engine's schema first: anything the parser would silently drop, ignore or replace is
    # refused by name here, because after parsing it is invisible.
    schema = _schema_errors(raw)
    if schema:
        return None, schema
    try:
        block = parse_detect_block(name, raw)
    except (re.error, ValueError, TypeError) as exc:
        return None, [f"invalid predicate: {exc}"]
    if block is None:
        return None, ["no valid predicates (need watch_predicates or trend_predicates; "
                      "promql is recorded but never evaluated, so it cannot fire)"]

    # Compiling is not the same as being able to fire. A model writing a regex from prose
    # reproduces #114's mistake (a space inside an anchored alternation) more readily than a
    # person reading the schema does, and the compiler has nothing to say about it. Reject a
    # predicate that provably can never match rather than staging a candidate whose zero
    # firings will be read as "the condition never occurred".
    dead = [msg for p in block.watch_predicates for msg in predicate_liveness_errors(p)]

    # Trend predicates were exempt from this until 2026-08-25, and the exemption shipped dead
    # detectors: two of the eight staged on the F3 soak cluster forecast a metric selector still
    # holding the model's own template (`deployment="your-deployment-name"`). They validated,
    # stored, listed as `shadow`, and matched no series for 24 hours. A forecast that can never
    # fire is worse than a missing one — its silence reads as a clean bill of health.
    dead += [msg for t in block.trend_predicates for msg in trend_liveness_errors(t)]

    # And the mirror image: a predicate that matches a HEALTHY object. `nl:soak-cpu-saturated`
    # was authored from "a workload is pinned at its CPU limit" and compiled to
    # `{kind: Pod, status_regex: '^Running$'}`, which fires on every healthy pod on the cluster —
    # 46 of them on an idle soak cluster before any fault was injected. A `WatchPredicate` has no
    # namespace or label scope, so the author cannot narrow it afterwards; the only place to stop
    # it is here. This is refused for the same reason a dead predicate is: its output does not
    # mean what the operator will read it to mean.
    dead += [msg for p in block.watch_predicates for msg in predicate_health_errors(p)]

    if dead:
        return None, dead

    # Last: the block that would load must be the block that was written, predicate for
    # predicate and knob for knob.
    drift = _fidelity_errors(raw, block)
    if drift:
        return None, drift

    return block, []


async def find_compilation(description: str, cluster_id: str = "global") -> dict | None:
    """The stored NL compilation of this exact description (whitespace-insensitive), or None.

    Matched on the digest stored in the row's provenance; rows staged before provenance existed
    are matched on `created_from`, which holds the description verbatim when it is at most 500
    characters. Returns `{name, status, block, compilation}`, `block` being the stored `detect:`
    mapping minus its provenance. Raises `review.DetectorStoreUnavailable` when the store cannot be
    read -- "I could not look" must not become "never compiled before" and buy a recompilation.
    """
    from app.detectors.review import DetectorStoreUnavailable
    from app.memory import service

    pool = service._pool
    if pool is None:
        raise DetectorStoreUnavailable("no memory pool — the detector store is not configured")
    verbatim = description if len(description) <= 500 else None
    try:
        row = await pool.fetchrow(
            "SELECT name, predicate, status FROM detectors"
            " WHERE cluster_id = $1 AND source = 'nl'"
            " AND (predicate->'compilation'->>'description_sha256' = $2"
            "      OR (predicate->'compilation' IS NULL AND created_from = $3))"
            " ORDER BY created_at DESC LIMIT 1",
            cluster_id, description_digest(description), verbatim,
        )
    except Exception as exc:
        logger.warning(f"nl_authoring: compilation lookup failed: {exc}")
        raise DetectorStoreUnavailable(f"detector store query failed: {exc}") from exc
    if not row:
        return None
    pred = row["predicate"]
    if isinstance(pred, str):
        try:
            pred = json.loads(pred)
        except json.JSONDecodeError:
            pred = None
    block = dict(pred) if isinstance(pred, dict) else {}
    compilation = block.pop(COMPILATION_KEY, None)
    return {"name": row["name"], "status": row["status"], "block": block,
            "compilation": compilation if isinstance(compilation, dict) else None}


async def stage_candidate(
    name: str, description: str, raw_block: dict, author: str, cluster_id: str = "global",
    compilation: dict | None = None,
) -> bool:
    """Insert a validated block into the `detectors` table as a SHADOW candidate.

    The stored `predicate` is the raw `detect:` mapping -- plus its `compilation` provenance when
    given -- so the engine recompiles it from the store (see engine.load_db_detectors) and nothing
    downstream ever asks the model again. Returns True iff a row was created, False iff a detector
    with this name already exists. Raises `review.DetectorStoreUnavailable` when the store cannot
    be written: that used to be a silent False, reported to the author as `not-staged` with no
    reason, indistinguishable from a name clash.
    """
    from app.detectors.review import DetectorStoreUnavailable
    from app.memory import service

    pool = service._pool
    if pool is None:
        raise DetectorStoreUnavailable("no memory pool — the detector store is not configured")
    stored = {**raw_block, COMPILATION_KEY: compilation} if compilation else raw_block
    try:
        result = await pool.execute(
            """
            INSERT INTO detectors (cluster_id, name, source, predicate, status, created_from, reviewed_by)
            VALUES ($1, $2, 'nl', $3::jsonb, 'shadow', $4, $5)
            ON CONFLICT (cluster_id, name) DO NOTHING
            """,
            cluster_id,
            name,
            json.dumps(stored),
            description[:500],
            author,
        )
    except Exception as exc:
        logger.warning(f"nl_authoring: stage failed: {exc}")
        raise DetectorStoreUnavailable(f"detector store write failed: {exc}") from exc
    return bool(result and result.endswith("1"))
