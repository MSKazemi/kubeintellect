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
from pydantic import BaseModel

from app.api.v1.auth import get_user_role
from app.core.config import settings
from app.detectors import authoring, review
from app.detectors.service import get_engine

router = APIRouter()


class NewDetectorRequest(BaseModel):
    description: str
    name: str | None = None


def _require_enabled() -> None:
    if not settings.NL_DETECTOR_AUTHORING_ENABLED:
        raise HTTPException(status_code=404, detail="NL detector authoring is disabled.")


def _require_writer(request: Request) -> str:
    role = get_user_role(request)
    if role not in {"operator", "admin", "superadmin"}:
        raise HTTPException(status_code=403, detail="operator role required")
    return role


@router.post("/detectors")
async def create_detector(req: NewDetectorRequest, request: Request):
    _require_enabled()
    author = _require_writer(request)
    raw = await authoring.compile_nl_to_detect_block(req.description)
    block, errors = authoring.validate_detect_block(raw, name=req.name or "nl")
    if block is None:
        return {"staged": False, "compiled": raw, "errors": errors}
    name = req.name or f"nl:{block.playbook}"
    if name == "nl:nl":
        name = f"nl:{req.description[:40].strip().replace(' ', '-')}"
    staged = await authoring.stage_candidate(name, req.description, raw, author=author)
    return {
        "staged": staged,
        "status": "shadow" if staged else "not-staged",
        "name": name,
        "compiled": raw,
        "errors": errors,
        "note": "Shadow detectors observe only — promote after reviewing precision.",
    }


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
    if loaded.watch_predicates:
        return True, (f"loaded, with watch predicates evaluated on every observation.{partial}"
                      if partial else
                      "loaded, with watch predicates evaluated on every observation")
    if loaded.trend_predicates:
        if settings.PREDICTIVE_DETECTION_ENABLED:
            return True, ("loaded, with trend predicates evaluated on the predictive "
                          f"interval.{partial}" if partial else
                          "loaded, with trend predicates evaluated on the predictive interval")
        return False, (
            f"{name} is loaded but has only trend predicates, and PREDICTIVE_DETECTION_ENABLED "
            "is false — nothing evaluates them, so it cannot fire on this deployment. Its zero "
            f"firings are not evidence about the predicate.{partial}"
        )
    return False, f"{name} compiled to no evaluable predicate"
