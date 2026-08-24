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
    return {
        "name": name,
        # False also covers "the DB was unreachable at the last refresh", which `load_db_detectors`
        # documents as silently disarming stored detectors — so this says "not loaded here", never
        # "no such detector".
        "watching": any(d.playbook == name for d in engine.shadow_detectors),
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
