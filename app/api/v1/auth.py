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

AUTH_BACKEND modes:
  static — admin/operator/readonly keys all checked against the static lists in config
  hmac   — admin/operator keys still use static lists; readonly "ki-ro-*" keys are
           validated via HMAC-SHA256 so new keys are valid instantly without a restart.
           Key format: ki-ro-<base64url(email:exp_unix)>.<hmac_sha256_hex[:32]>
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

from fastapi import HTTPException, Request

from app.core.config import settings


def _verify_hmac_demo_key(token: str) -> bool:
    """Return True if token is a valid, unexpired HMAC-signed demo key."""
    secret = settings.DEMO_KEY_HMAC_SECRET
    if not secret or not token.startswith("ki-ro-"):
        return False

    rest = token[6:]  # strip "ki-ro-"
    if "." not in rest:
        return False

    payload, sig = rest.rsplit(".", 1)

    expected = hmac.digest(secret.encode(), payload.encode(), hashlib.sha256).hex()[:32]
    if not hmac.compare_digest(expected, sig):
        return False

    # Decode payload and check expiry
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload + padding).decode()
        exp_str = decoded.rsplit(":", 1)[-1]
        return int(exp_str) > int(time.time())
    except Exception:
        return False


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

    # Readonly: static list first, then HMAC (when AUTH_BACKEND=hmac)
    if token in settings.readonly_keys:
        return "readonly"
    if settings.AUTH_BACKEND == "hmac" and _verify_hmac_demo_key(token):
        return "readonly"

    raise HTTPException(status_code=401, detail="Invalid API key")
