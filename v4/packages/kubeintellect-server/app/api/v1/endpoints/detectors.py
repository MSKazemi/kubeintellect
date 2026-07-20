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
    return {"detectors": await review.list_detectors(status=status)}


@router.post("/detectors/{name}/promote")
async def promote_detector(name: str, request: Request):
    _require_enabled()
    reviewer = _require_writer(request)
    ok = await review.promote_candidate(name, reviewer=reviewer)
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
    _require_enabled()
    engine = get_engine()
    if engine is None:
        return {"sensorium": "disabled", "findings": []}
    found = [f.to_dict() for f in engine.shadow_findings if f.playbook == name]
    return {"name": name, "findings": found}
