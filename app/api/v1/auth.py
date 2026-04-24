"""
API key verification — extracts and validates the Bearer token from the
Authorization header and returns the caller's role.

Role model (three tiers):
  "admin"    — high + medium risk ops allowed, always HITL-gated
  "operator" — medium risk ops allowed (create, apply, scale, exec…), HITL-gated;
               high-risk ops (delete, drain, replace, taint) are blocked
  "readonly" — read-only ops only; all write ops rejected before reaching the agent

When auth is disabled (no keys configured), all requests are treated as
"admin" to preserve backward compatibility with unauthenticated deployments.
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from app.core.config import settings


def get_user_role(request: Request) -> str:
    """Return "admin", "operator", or "readonly" for the request, or raise HTTP 401."""
    if not settings.auth_enabled:
        return "admin"

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization: Bearer <api_key> required",
        )

    token = auth.removeprefix("Bearer ").strip()
    if token in settings.admin_keys:
        return "admin"
    if token in settings.operator_keys:
        return "operator"
    if token in settings.readonly_keys:
        return "readonly"
    raise HTTPException(status_code=401, detail="Invalid API key")
