"""V1 API router — aggregates all endpoints."""
from fastapi import APIRouter

from app.api.v1.endpoints.chat_completions import router as chat_router
from app.api.v1.endpoints.events import router as events_router
from app.api.v1.endpoints.health import router as health_router
from app.api.v1.endpoints.namespaces import router as namespaces_router

api_router = APIRouter()

api_router.include_router(health_router)
api_router.include_router(namespaces_router)
api_router.include_router(chat_router)
api_router.include_router(events_router)
