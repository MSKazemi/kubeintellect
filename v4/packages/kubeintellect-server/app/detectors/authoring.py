"""Natural-language detector authoring (ADR-012).

A human describes a failure in plain English; an LLM compiles it into a `detect:`
block, which is validated against the existing schema and then checked for liveness --
compiling is not the same as being able to fire (`predicate_shape`) -- and staged as a
SHADOW candidate in the `detectors` table. Shadow
detectors observe and accrue precision but never reach the watchtower until a
human promotes them (see `review.promote_candidate`).

Compilation is a one-time authoring-time LLM call; the resulting detector runs
zero-token like every other detector.
"""
from __future__ import annotations

import json
import re

from app.detectors.models import DetectBlock, parse_detect_block
from app.detectors.predicate_shape import predicate_liveness_errors
from app.utils.logger import get_logger

logger = get_logger(__name__)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

_AUTHORING_SYSTEM = """You compile a Kubernetes failure description into a detector `detect:` block.

Output ONLY a JSON object with any of these keys (omit those you don't need):
- "watch_predicates": list of {kind: "Pod"|"Event"|"Node",
    status_regex (Pod/Node, matched against the STATUS column),
    reason_regex, message_regex (Event; Warning events only),
    involved_kind (optional)}
- "promql": OPTIONAL context only — these queries are recorded but NOT evaluated,
  so a detector whose only predicate is promql can never fire. Never rely on it.
- "trend_predicates": list of {metric (range PromQL), threshold (number),
    window_minutes, projection_horizon_minutes, fire_if_eta_within_minutes,
    direction: "rising"|"falling", min_r2} — for forecasting a slow-burn failure
- "debounce_seconds": integer

Use anchored, specific RE2 regexes. Examples:
{"watch_predicates": [{"kind": "Pod", "status_regex": "^OOMKilled$"}]}
{"watch_predicates": [{"kind": "Event", "reason_regex": "^BackOff$",
  "message_regex": "Back-off restarting failed container", "involved_kind": "Pod"}]}

Return JSON only — no prose, no code fences."""


async def compile_nl_to_detect_block(description: str) -> dict:
    """LLM-compile a plain-English failure description into a `detect:` mapping.

    Fail-open: returns {} on any error so the caller surfaces a validation
    message rather than crashing.
    """
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.cortex.models import get_specialist_llm

        llm = get_specialist_llm()
        resp = await llm.ainvoke(
            [SystemMessage(content=_AUTHORING_SYSTEM), HumanMessage(content=description)]
        )
        return _parse_detect_json(getattr(resp, "content", "") or "")
    except Exception as exc:
        logger.warning(f"nl_authoring: compile failed: {exc}")
        return {}


def _parse_detect_json(text: str) -> dict:
    match = _JSON_RE.search(text)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def validate_detect_block(raw: dict, name: str = "nl") -> tuple[DetectBlock | None, list[str]]:
    """Validate a compiled block by running it through the real compiler.

    Returns (block, errors). block is None when nothing valid compiled, a predicate is
    malformed (e.g. an uncompilable regex), or a predicate compiles cleanly but provably
    cannot ever match — see `predicate_shape.predicate_liveness_errors`.
    """
    if not isinstance(raw, dict):
        return None, ["compiler did not return a JSON object"]
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
    if dead:
        return None, dead

    return block, []


async def stage_candidate(
    name: str, description: str, raw_block: dict, author: str, cluster_id: str = "global"
) -> bool:
    """Insert a validated block into the `detectors` table as a SHADOW candidate.

    The stored `predicate` is the raw `detect:` mapping so the engine can
    recompile it (see engine.load_db_detectors). Returns True iff a row was
    created. Fail-open: DB unavailable → False, never raises.
    """
    from app.memory import service

    pool = service._pool
    if pool is None:
        return False
    try:
        result = await pool.execute(
            """
            INSERT INTO detectors (cluster_id, name, source, predicate, status, created_from, reviewed_by)
            VALUES ($1, $2, 'nl', $3::jsonb, 'shadow', $4, $5)
            ON CONFLICT (cluster_id, name) DO NOTHING
            """,
            cluster_id,
            name,
            json.dumps(raw_block),
            description[:500],
            author,
        )
        return bool(result and result.endswith("1"))
    except Exception as exc:
        logger.warning(f"nl_authoring: stage failed: {exc}")
        return False
