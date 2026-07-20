# app/api_v1/routers.py
from fastapi import APIRouter
from app.api.v1.endpoints import chat_completions, kubernetes, tools

api_router = APIRouter()
api_router.include_router(chat_completions.router, tags=["Chat Completions"])
api_router.include_router(kubernetes.router, prefix="/k8s", tags=["Kubernetes"])
api_router.include_router(tools.router, tags=["Tools"])