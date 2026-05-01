"""POST /v1/auth/demo-keys — mint a readonly HMAC demo key (admin-gated).

Mint flow:
  1. Caller authenticates with an admin or superadmin Bearer key.
  2. Server signs a ki-ro-<payload>.<sig> key for the requested email + TTL.
  3. Caller (typically the website's backend, after its own email verification)
     hands the minted key to the end user.

The HMAC secret never leaves this server — keys are validated stateless by
the same secret in app/api/v1/auth.py::_verify_hmac_demo_key.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from app.api.v1.auth import get_user_role, mint_demo_key
from app.core.config import settings

router = APIRouter()

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class DemoKeyMintRequest(BaseModel):
    email: str
    ttl_hours: int | None = Field(default=None, ge=1)

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        if not _EMAIL_RE.match(v):
            raise ValueError("invalid email")
        return v


class DemoKeyMintResponse(BaseModel):
    api_key: str
    email: str
    expires_at: int
    expires_in_seconds: int


@router.post("/auth/demo-keys", response_model=DemoKeyMintResponse)
def mint(req: DemoKeyMintRequest, request: Request) -> DemoKeyMintResponse:
    role = get_user_role(request)
    if role not in {"admin", "superadmin"}:
        raise HTTPException(status_code=403, detail="admin role required")
    if not settings.DEMO_KEY_HMAC_SECRET:
        raise HTTPException(status_code=503, detail="DEMO_KEY_HMAC_SECRET not configured")

    ttl_h = req.ttl_hours or settings.DEMO_KEY_DEFAULT_TTL_HOURS
    if ttl_h > settings.DEMO_KEY_MAX_TTL_HOURS:
        raise HTTPException(
            status_code=400,
            detail=f"ttl_hours exceeds max ({settings.DEMO_KEY_MAX_TTL_HOURS})",
        )

    key, exp = mint_demo_key(req.email, ttl_h * 3600)
    return DemoKeyMintResponse(
        api_key=key,
        email=req.email,
        expires_at=exp,
        expires_in_seconds=ttl_h * 3600,
    )
