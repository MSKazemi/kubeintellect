"""Natural-language detector authoring + review (ADR-012).

POST /v1/detectors                  compile NL → validate → stage as SHADOW
GET  /v1/detectors?status=          list the candidate / shadow / active queue
POST /v1/detectors/{name}/promote   shadow → active (reaches the watchtower)
POST /v1/detectors/{name}/demote    stop firing entirely
GET  /v1/detectors/{name}/shadow-findings   what a shadow detector has fired

Promotion/authoring are write actions — gated to operator/admin. Nothing reaches
the watchtower without an explicit human promote.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.v1.auth import get_user_role
from app.core.config import settings
from app.detectors import authoring, review
from app.detectors.service import get_engine

router = APIRouter()


class NewDetectorRequest(BaseModel):
    description: str
    name: str | None = None
    # Compile afresh even though this exact description was compiled before. Off by default: the
    # same prose then always answers with the same stored predicates (see `create_detector`).
    recompile: bool = False


def _require_enabled() -> None:
    if not settings.NL_DETECTOR_AUTHORING_ENABLED:
        raise HTTPException(status_code=404, detail="NL detector authoring is disabled.")


def _require_writer(request: Request) -> str:
    role = get_user_role(request)
    if role not in {"operator", "admin", "superadmin"}:
        raise HTTPException(status_code=403, detail="operator role required")
    return role


def _refused(status_code: int, detail: str, **body) -> JSONResponse:
    """A refusal that still carries what was compiled, so the author can see why.

    Not an HTTPException: the body keeps the `staged`/`compiled`/`errors` shape clients already
    read, plus `detail` in the one-string form `kq`'s `server_detail` renders.
    """
    return JSONResponse(status_code=status_code,
                        content={"staged": False, "detail": detail, **body})


async def _staging(name: str, status: str) -> tuple[bool, str]:
    """Is `name` loaded AND evaluated by this process's engine? Read off the engine, never assumed.

    An INSERT that succeeded says the row exists, not that anything watches with it: the engine
    may not run in this process (a leader-election standby, a failed start, the flag off), a
    refresh may fail and keep the old set, or the loader may refuse the row. Each of those used to
    answer `staged: true`. The engine is refreshed first so a healthy deployment answers truthfully
    *now* rather than one refresh interval later.
    """
    from app.detectors.service import reload_db_detectors, sensorium_absence

    def _no_engine() -> tuple[bool, str]:
        state, why = sensorium_absence()
        return False, (
            f"stored, but the detector engine is not running in this process (state={state}"
            f"{': ' + why if why else ''}), so nothing here has loaded it. A replica running the "
            f"engine loads stored detectors every {settings.DB_DETECTOR_REFRESH_SECONDS}s; "
            "resubmit the same description to re-check — the stored compilation is reused, not "
            "recompiled."
        )

    if get_engine() is None:
        return _no_engine()
    await reload_db_detectors()
    engine = get_engine()
    if engine is None:
        return _no_engine()
    candidates = engine.detectors if status == "active" else engine.shadow_detectors
    loaded = next((d for d in candidates if d.playbook == name), None)
    return _watching(loaded, name)


@router.post("/detectors")
async def create_detector(req: NewDetectorRequest, request: Request):
    """Compile → validate → store → confirm the engine loaded it. Every exit says which happened.

    | outcome                                              | HTTP | staged | stored |
    |------------------------------------------------------|------|--------|--------|
    | loaded and evaluated by this process's engine        | 200  | true   | true   |
    | stored, but not loaded/evaluated here (reason given) | 202  | false  | true   |
    | compiled detector refused by the validation gate     | 422  | false  | false  |
    | name taken, or identical prose already demoted       | 409  | false  | —      |
    | compiler model could not be called                   | 502  | false  | false  |
    | detector store unavailable                           | 503  | false  | false  |

    Identical prose (whitespace-insensitive) that was compiled before is answered from the STORED
    compilation, without calling the model: the same eight descriptions compiled 47 minutes apart
    produced different predicate counts on four of them, so "compile again" is not a neutral
    re-read. `recompile: true` asks for a fresh compilation; `compilation.source` says which one
    the response carries.
    """
    _require_enabled()
    author = _require_writer(request)
    description = req.description
    if not authoring.normalize_description(description):
        raise HTTPException(status_code=422, detail="description is empty — nothing to compile")

    # The store is read first: if it cannot be read, nothing could be staged anyway, and spending a
    # compilation on an answer that cannot be kept is how the same prose gets two compilations.
    try:
        previous = None if req.recompile else await authoring.find_compilation(description)
    except review.DetectorStoreUnavailable as exc:
        raise HTTPException(status_code=503,
                            detail=f"{exc} — nothing can be staged, so nothing was compiled") from exc
    if previous is not None:
        return await _reuse(previous, req)

    try:
        raw, provenance = await authoring.compile_nl_to_detect_block(description)
    except authoring.CompilerUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    compilation = {**provenance, "source": "fresh"}

    block, errors = authoring.validate_detect_block(raw, name=req.name or "nl")
    if block is not None:
        errors = authoring.deployment_errors(block)
    if block is not None and not errors and block.promql:
        # Prometheus is the PromQL parser: run each query once before anything is stored (#20).
        errors = await authoring.promql_probe_errors(block)
    if block is None or errors:
        return _refused(422, f"the compiled detector was refused: {errors[0]}", stored=False,
                        compiled=raw, errors=errors, compilation=compilation)

    name = req.name or f"nl:{block.playbook}"
    if name == "nl:nl":
        name = f"nl:{req.description[:40].strip().replace(' ', '-')}"
    try:
        created = await authoring.stage_candidate(name, description, raw, author=author,
                                                  compilation=provenance)
    except review.DetectorStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"{exc} — nothing was stored") from exc
    if not created:
        return _refused(
            409,
            f"a detector named {name!r} already exists — nothing was stored. Choose a different "
            "`name`.",
            stored=False, name=name, compiled=raw, errors=[], compilation=compilation,
        )

    staged, reason = await _staging(name, "shadow")
    return JSONResponse(status_code=200 if staged else 202, content={
        "staged": staged,
        "stored": True,
        "status": "shadow",
        "name": name,
        "compiled": raw,
        "errors": [],
        "staged_reason": reason,
        "compilation": compilation,
        "note": "Shadow detectors observe only — promote after reviewing precision.",
    })


async def _reuse(previous: dict, req: NewDetectorRequest) -> JSONResponse:
    """Answer identical prose from its stored compilation. The model is not called."""
    name, status, stored = previous["name"], previous["status"], previous["block"]
    # Rows staged before provenance was stored carry none; say so rather than invent it.
    compilation = {**(previous["compilation"] or {"provenance": "not recorded (staged before "
                                                                "compilations were stored)"}),
                   "source": "stored"}
    if status == "demoted":
        return _refused(
            409,
            f"this description was already compiled as {name!r}, which a reviewer demoted — "
            "nothing was staged. Resubmit with recompile=true and a new `name` to compile it "
            "afresh.",
            stored=True, name=name, status=status, compiled=stored, errors=[],
            compilation=compilation,
        )
    # Re-gated, not trusted: the row may predate the gate, and the gate must agree with itself.
    block, errors = authoring.validate_detect_block(stored, name=name)
    if block is not None:
        errors = authoring.deployment_errors(block)
    if block is not None and not errors and block.promql:
        # Prometheus is the PromQL parser: run each query once before anything is stored (#20).
        errors = await authoring.promql_probe_errors(block)
    if block is None or errors:
        return _refused(
            422,
            f"the stored compilation of this description ({name!r}) is refused by the validation "
            f"gate: {errors[0]}. Resubmit with recompile=true and a new `name`.",
            stored=True, name=name, status=status, compiled=stored, errors=errors,
            compilation=compilation,
        )
    staged, reason = await _staging(name, status)
    note = "Identical description: the stored compilation was reused, not recompiled."
    if req.name and req.name != name:
        note += (f" The requested name {req.name!r} was not used; pass recompile=true to stage a "
                 "separate detector under it.")
    return JSONResponse(status_code=200 if staged else 202, content={
        "staged": staged,
        "stored": True,
        "status": status,
        "name": name,
        "compiled": stored,
        "errors": [],
        "staged_reason": reason,
        "compilation": compilation,
        "reused": True,
        "note": note,
    })


@router.get("/detectors")
async def list_detectors(status: str | None = Query(default=None)):
    _require_enabled()
    try:
        detectors = await review.list_detectors(status=status)
    except review.DetectorStoreUnavailable as exc:
        # 503, not an empty 200. "I cannot answer" and "the answer is nothing" are different, and
        # for a detector inventory the difference is whether the operator believes their cluster is
        # unmonitored or merely unqueryable. Same reasoning as /findings reporting
        # `sensorium: disabled` instead of an innocent empty list.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"detectors": detectors}


@router.post("/detectors/{name}/promote")
async def promote_detector(name: str, request: Request):
    _require_enabled()
    reviewer = _require_writer(request)
    try:
        ok = await review.promote_candidate(name, reviewer=reviewer)
    except review.DetectorCannotFire as exc:
        # 409, not a cheerful 200. Flipping the row would make this endpoint answer
        # `status: active` about a detector that can never match anything.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail=f"detector '{name}' not found")
    return {"name": name, "status": "active", "reviewed_by": reviewer}


@router.post("/detectors/{name}/demote")
async def demote_detector(name: str, request: Request):
    _require_enabled()
    reviewer = _require_writer(request)
    ok = await review.demote_candidate(name, reviewer=reviewer)
    if not ok:
        raise HTTPException(status_code=404, detail=f"detector '{name}' not found")
    return {"name": name, "status": "demoted", "reviewed_by": reviewer}


@router.get("/detectors/{name}/shadow-findings")
async def shadow_findings(name: str):
    """What a shadow detector has fired — and what that count is worth.

    This number is the promote/reject decision, so an empty one has to say which kind of empty
    it is. Until 2026-08-24 it did not: a sensorium that is not running, a detector this process
    never loaded, and a detector that ran quietly all answered `200` with `findings: []`, and
    `kq detector shadow <name>` rendered all three as "0 shadow firing(s)" — a reviewer reading
    "quiet, no false positives" off a detector that was never evaluated.

    The 503 follows `list_detectors` above, which already draws this line: "'I cannot answer'
    and 'the answer is nothing' are different."
    """
    _require_enabled()
    engine = get_engine()
    if engine is None:
        raise HTTPException(
            status_code=503,
            detail=(
                f"the detector engine is not running in this process, so no shadow detector has "
                f"been evaluated. This is NOT the same as '{name}' having fired nothing, and it "
                f"is not a basis for promoting or rejecting it."
            ),
        )
    found = [f.to_dict() for f in engine.shadow_findings if f.playbook == name]
    ring = engine.shadow_findings
    loaded = next((d for d in engine.shadow_detectors if d.playbook == name), None)
    watching, watching_reason = _watching(loaded, name)
    return {
        "name": name,
        # False also covers "the DB was unreachable at the last refresh", which `load_db_detectors`
        # documents as silently disarming stored detectors — so this says "not evaluated here",
        # never "no such detector".
        "watching": watching,
        # Why, in one sentence. A bare False sent an operator to the predicate when the cause was
        # a flag, and a bare True hid a trend-only detector that nothing was evaluating.
        "watching_reason": watching_reason,
        "findings": found,
        # The ring is fixed-size and in-memory: it is emptied by a restart and, once saturated,
        # drops the OLDEST firing per new one. Either way `findings` is a floor, not a total.
        "buffer": {
            "held": len(ring),
            "capacity": ring.maxlen,
            "saturated": ring.maxlen is not None and len(ring) >= ring.maxlen,
        },
        "durable": False,
    }


def _watching(loaded, name: str) -> tuple[bool, str]:
    """Is this detector's predicate actually being *evaluated*, and if not, why not.

    `watching` used to mean "the engine loaded it", which is a weaker claim than it reads as and
    was wrong in both directions on a real deployment:

    * A trend-only detector on a server with `PREDICTIVE_DETECTION_ENABLED=false` is loaded and
      never evaluated — nothing calls `evaluate_trends`. `watching: true` told an operator the
      detector was on duty while its only predicate was unreachable, which is the same silence
      the whole F3 soak was void for.
    * `false` on its own sent a reviewer to inspect a predicate when the cause was a flag or an
      unreachable store, which is a different repair entirely.

    So the field now answers the question it is read as answering, and carries the reason.
    """
    from app.core.config import settings

    if loaded is None:
        return False, (
            f"{name} is not in the engine's shadow set — it was not loaded (refused at load as "
            "unable to fire, malformed, scoped to another cluster, or the detector store was "
            "unreachable at the last refresh). This is not a statement that no such detector "
            "exists; see the server log for `db_detector_can_never_fire` and `load_db_detectors`."
        )
    # A partially-loaded detector is evaluated, but not as it was authored, and the difference
    # matters to whoever reads its firing count: the predicate they see in the store is not the
    # predicate that ran. Said here rather than only in the log, because the log is on the lane
    # and the reviewer is not.
    dropped = getattr(loaded, "dropped_predicates", ()) or ()
    partial = (f" {len(dropped)} of its predicates were refused at load and are NOT evaluated "
               f"({dropped[0]});" if dropped else "")
    # `watching: true` on a detector that fires on every healthy pod is true and misleading in
    # the same breath — the reviewer reads a firing count as a fault count. `nl:soak-cpu-saturated`
    # produced 46 findings on `kube-system` coredns pods on an idle cluster, all of them
    # `evidence: "pod status=Running"`, and the reason was only ever in a server log on the lane.
    healthy = getattr(loaded, "fires_on_healthy", ()) or ()
    if healthy:
        partial += (f" WARNING: this detector fires on HEALTHY objects, so its findings are not "
                    f"evidence of a fault — {healthy[0]}")
    # A detector that carries trend predicates as well as watch ones is evaluated only in part
    # while predictive detection is off; say which part, or its trend half is silently absent.
    if loaded.trend_predicates and not settings.PREDICTIVE_DETECTION_ENABLED:
        partial += (" Its trend predicates are NOT evaluated (PREDICTIVE_DETECTION_ENABLED is "
                    "false), so it cannot fire on them here.")
    if loaded.watch_predicates:
        return True, (f"loaded, with watch predicates evaluated on every observation.{partial}"
                      if partial else
                      "loaded, with watch predicates evaluated on every observation")
    # PromQL (#20). `load_db_detectors` already dropped every query on a deployment that does
    # not evaluate PromQL, so a loaded query is one the sweep runs — but the sweep may be blind.
    if loaded.promql:
        from app.detectors.engine import promql_unavailable_reason

        if promql_unavailable_reason() is None:
            what = (f"loaded, with PromQL predicates evaluated every "
                    f"{settings.PROMQL_DETECTION_INTERVAL_SECONDS}s")
            engine = get_engine()
            blind = getattr(engine, "last_promql_error", None) if engine is not None else None
            if blind:
                what += (f" — but the last PromQL sweep was BLIND ({blind}), so its recent "
                         "silence is not evidence")
            return True, f"{what}.{partial}" if partial else what
    if loaded.trend_predicates:
        if settings.PREDICTIVE_DETECTION_ENABLED:
            what = ("loaded, with trend predicates evaluated on the predictive interval, "
                    "firing into the shadow buffer only")
            engine = get_engine()
            blind = getattr(engine, "last_trend_error", None) if engine is not None else None
            if blind:
                what += (f" — but the last trend sweep was BLIND ({blind}), so its recent "
                         "silence is not evidence")
            return True, f"{what}.{partial}" if partial else what
        return False, (
            f"{name} is loaded but has only trend predicates, and PREDICTIVE_DETECTION_ENABLED "
            "is false — nothing evaluates them, so it cannot fire on this deployment. Its zero "
            f"firings are not evidence about the predicate.{partial}"
        )
    return False, f"{name} compiled to no evaluable predicate"
