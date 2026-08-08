"""V1 API router — aggregates all endpoints."""
from fastapi import APIRouter

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

api_router = APIRouter()

api_router.include_router(health_router)
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
