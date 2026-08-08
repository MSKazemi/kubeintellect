"""GET /healthz — liveness probe + version identity (ADR-019 three axes)."""
from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core.version import active_experimental_flags, arm, code_version

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    arm: str                                   # architecture generation (KI_VERSION, e.g. "v4")
    version: str                               # software SemVer (distinguishes v4 / v4.1 / v4.2)
    experimental_flags: list[str] = Field(default_factory=list)  # active default-off toggles


@router.get("/healthz", response_model=HealthResponse)
async def healthz():
    return HealthResponse(
        status="ok",
        arm=arm(),
        version=code_version(),
        experimental_flags=active_experimental_flags(),
    )
