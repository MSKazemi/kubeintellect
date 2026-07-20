"""GET /healthz — liveness probe."""
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    version: str = "2.0.0"


@router.get("/healthz", response_model=HealthResponse)
async def healthz():
    return HealthResponse(status="ok")
