"""Probe endpoints.

* ``GET /healthz`` — **liveness** + version identity (ADR-019 three axes). Checks nothing by
  design; a liveness probe that touches a dependency turns one blip into a restart loop.
* ``GET /readyz`` — **readiness**. Local state only: 200 while serving, 503 once shutdown has
  begun, so a rolling update drains instead of dropping requests. See ``app.core.readiness``
  for why this deliberately does not probe Postgres.
"""
from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from app.core.readiness import is_ready
from app.core.version import active_experimental_flags, arm, code_version

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    arm: str                                   # architecture generation (KI_VERSION, e.g. "v4")
    version: str                               # software SemVer (distinguishes v4 / v4.1 / v4.2)
    experimental_flags: list[str] = Field(default_factory=list)  # active default-off toggles


class ReadyResponse(BaseModel):
    status: str          # "ready" | "draining"


@router.get("/healthz", response_model=HealthResponse)
async def healthz():
    return HealthResponse(
        status="ok",
        arm=arm(),
        version=code_version(),
        experimental_flags=active_experimental_flags(),
    )


@router.get("/readyz", response_model=ReadyResponse)
async def readyz(response: Response):
    """200 while this replica should receive traffic, 503 once it is draining."""
    ready = is_ready()
    if not ready:
        response.status_code = 503
    return ReadyResponse(status="ready" if ready else "draining")
