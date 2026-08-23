"""GET /v5/status — v5 trust-plane observability.

Operators need to SEE the trust plane's live state: which v5 slices are active, and whether the
fail-closed brakes (kill switch, change freeze) are engaged. Surfaces version identity + active
experimental flags + the blast-radius gate state in one call, reusing the modules already built.
Read-only; safe.
"""
from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.autonomy.budget import change_freeze_active, kill_switch_engaged
from app.core.config import settings
from app.core.config_audit import unenforceable_guard_config
from app.core.version import (
    active_experimental_flags,
    arm,
    code_version,
    set_but_unwired_flags,
)

router = APIRouter()


class V5Status(BaseModel):
    arm: str                                        # architecture generation (KI_VERSION)
    version: str                                    # package SemVer
    cortex_v5_enabled: bool                         # the master v5 switch
    active_flags: list[str] = Field(default_factory=list)
    # flags the operator turned ON that no code reads — reported so a rollout is not
    # confirmed against a switch that does nothing (audited 2026-08-19)
    set_but_unwired_flags: list[str] = Field(default_factory=list)
    # guard settings that parse cleanly and protect nothing — a blocked-namespace entry that
    # cannot match any namespace, or an autonomy override the parser drops (audited 2026-08-20)
    unenforceable_guard_config: list[str] = Field(default_factory=list)
    # blast-radius / write-authority brakes (P3)
    kill_switch_engaged: bool                       # ⇒ all autonomous writes denied
    change_freeze: bool                             # ⇒ deny-by-default change window
    spend_cap_usd: float                            # 0 = unlimited


@router.get("/v5/status", response_model=V5Status)
async def v5_status():
    return V5Status(
        arm=arm(),
        version=code_version(),
        cortex_v5_enabled=settings.CORTEX_V5_ENABLED,
        active_flags=active_experimental_flags(),
        set_but_unwired_flags=set_but_unwired_flags(),
        unenforceable_guard_config=unenforceable_guard_config(),
        kill_switch_engaged=kill_switch_engaged(),
        change_freeze=change_freeze_active(),
        spend_cap_usd=settings.KI_V5_SPEND_CAP_USD,
    )
