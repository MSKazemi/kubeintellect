"""V1 API router — aggregates all endpoints, and authenticates them by default.

Authentication used to be a per-endpoint convention: each handler called `get_user_role`
itself. It was therefore present exactly where somebody remembered it, and measured
2026-08-20 with auth enabled, ten of twelve routes answered with no Authorization header at
all — `/v1/digest`, `/v1/findings`, both episode replays, the postmortem narrative,
`/v1/namespaces`, `/v1/v5/status`, and the *read* halves of `/v1/detectors` and
`/v1/preferences`, whose write halves were correctly gated by `_require_writer`.

Authentication is now a property of the router, not of each handler: everything mounted here
is authenticated, and the two probe endpoints are mounted separately and deliberately public.
A route added tomorrow inherits the gate instead of needing to remember it.
"""
from fastapi import APIRouter, Depends, Request

from app.api.v1.auth import get_user_role

from app.api.v1.endpoints.auth import router as auth_router
from app.api.v1.endpoints.chat_completions import router as chat_router
from app.api.v1.endpoints.detectors import router as detectors_router
from app.api.v1.endpoints.digest import router as digest_router
from app.api.v1.endpoints.episodes import router as episodes_router
from app.api.v1.endpoints.events import router as events_router
from app.api.v1.endpoints.findings import router as findings_router
from app.api.v1.endpoints.health import router as health_router
from app.api.v1.endpoints.namespaces import router as namespaces_router
from app.api.v1.endpoints.postmortem import router as postmortem_router
from app.api.v1.endpoints.preferences import router as preferences_router
from app.api.v1.endpoints.v5_status import router as v5_status_router

def require_authenticated(request: Request) -> str:
    """Reject a request that carries no valid API key; return the caller's role.

    Delegates to `get_user_role`, so the four-tier role model and the documented
    backward-compatible open mode (no keys configured ⇒ every caller is "admin") behave
    exactly as before. This only decides *whether* a caller is known, never what they may do —
    the per-verb role checks in the tools are untouched.
    """
    return get_user_role(request)


# Liveness and readiness must answer an unauthenticated kubelet, so they are mounted outside
# the authenticated router rather than exempted from inside it.
public_router = APIRouter()
public_router.include_router(health_router)

api_router = APIRouter(dependencies=[Depends(require_authenticated)])

api_router.include_router(namespaces_router)
api_router.include_router(chat_router)
api_router.include_router(events_router)
api_router.include_router(episodes_router)
api_router.include_router(findings_router)
api_router.include_router(detectors_router)
api_router.include_router(digest_router)
api_router.include_router(postmortem_router)
api_router.include_router(preferences_router)
api_router.include_router(v5_status_router)
api_router.include_router(auth_router)
