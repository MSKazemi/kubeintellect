"""Probe endpoints.

* ``GET /healthz`` — **liveness** + version identity (ADR-019 three axes). Checks nothing by
  design; a liveness probe that touches a dependency turns one blip into a restart loop.
* ``GET /readyz`` — **readiness**. Local state only: 200 while serving, 503 once shutdown has
  begun — though the 503 is unobservable in practice, because uvicorn stops listening before
  the shutdown hook runs. Draining a rolling update is the chart's ``preStop`` sleep, not this
  endpoint. See ``app.core.readiness`` for the measurement, and for why this deliberately does
  not probe Postgres.
"""
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from app.core.readiness import is_ready
from app.core.version import (
    active_experimental_flags,
    arm,
    code_version,
    set_but_unwired_flags,
)

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    arm: str                                   # architecture generation (KI_VERSION, e.g. "v4")
    version: str                               # software SemVer (distinguishes v4 / v4.1 / v4.2)
    experimental_flags: list[str] = Field(default_factory=list)  # active default-off toggles
    # ON but read by no code — an operator confirming a rollout must see this, not silence
    set_but_unwired_flags: list[str] = Field(default_factory=list)
    # Which replica runs the singleton workers. A STANDBY replica serves the API normally but
    # watches nothing, and that is indistinguishable from a broken sensorium unless it is
    # reported here. `enabled: false` means no election ran — one process by construction.
    leader: dict = Field(default_factory=dict)


class ReadyResponse(BaseModel):
    status: str          # "ready" | "draining"


@router.get("/healthz", response_model=HealthResponse)
async def healthz(request: Request):
    election = getattr(request.app.state, "leader_election", None)
    return HealthResponse(
        status="ok",
        arm=arm(),
        version=code_version(),
        experimental_flags=active_experimental_flags(),
        set_but_unwired_flags=set_but_unwired_flags(),
        leader=(
            election.status() if election is not None
            else {"enabled": False, "is_leader": True, "reason": "no election — single process"}
        ),
    )


@router.get("/readyz", response_model=ReadyResponse)
async def readyz(response: Response):
    """200 while this replica should receive traffic, 503 once it is draining."""
    ready = is_ready()
    if not ready:
        response.status_code = 503
    return ReadyResponse(status="ready" if ready else "draining")
