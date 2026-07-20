"""Operator-preference memory API (MemoryAgent).

GET    /v1/preferences?user=            list active preferences for a user
PUT    /v1/preferences                  set an explicit preference (operator+)
DELETE /v1/preferences/{key}?user=      forget a preference (operator+)

Preferences are per-user (the OpenAI-compatible `user` field, default "default"),
matching how chat scopes identity. Explicit preferences set here have confidence
1.0 and are never overwritten by behaviour-inferred ones. Reads are open; writes
require the operator role.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.api.v1.auth import get_user_role
from app.core.config import settings
from app.memory import preferences
from app.memory.service import memory_active

router = APIRouter()


class SetPreferenceRequest(BaseModel):
    key: str
    value: str
    user: str = "default"


def _require_enabled() -> None:
    if not settings.PREFERENCE_MEMORY_ENABLED:
        raise HTTPException(status_code=404, detail="Preference memory is disabled.")
    if not memory_active():
        raise HTTPException(
            status_code=503,
            detail="Memory hierarchy is not active (needs PostgreSQL + MEMORY_HIERARCHY_ENABLED).",
        )


def _require_writer(request: Request) -> None:
    if get_user_role(request) not in {"operator", "admin", "superadmin"}:
        raise HTTPException(status_code=403, detail="operator role required")


@router.get("/preferences")
async def list_preferences(user: str = Query(default="default")):
    _require_enabled()
    prefs = await preferences.recall_preferences(user, k=50)
    return {"user": user, "preferences": prefs}


@router.put("/preferences")
async def set_preference(req: SetPreferenceRequest, request: Request):
    _require_enabled()
    _require_writer(request)
    ok = await preferences.set_preference(req.user, req.key, req.value, source="explicit")
    if not ok:
        raise HTTPException(status_code=500, detail="failed to store preference")
    return {"user": req.user, "key": req.key, "value": req.value, "source": "explicit"}


@router.delete("/preferences/{key}")
async def forget_preference(key: str, request: Request, user: str = Query(default="default")):
    _require_enabled()
    _require_writer(request)
    ok = await preferences.forget_preference(user, key)
    if not ok:
        raise HTTPException(status_code=500, detail="failed to forget preference")
    return {"user": user, "key": key, "forgotten": True}
