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
    degraded_experimental_flags,
    set_but_unwired_flags,
)
from app.memory import service as memory_service
from app.memory.service import memory_status

router = APIRouter()


class V5Status(BaseModel):
    arm: str                                        # architecture generation (KI_VERSION)
    version: str                                    # package SemVer
    cortex_v5_enabled: bool                         # the master v5 switch
    active_flags: list[str] = Field(default_factory=list)
    # flags the operator turned ON that no code reads — reported so a rollout is not
    # confirmed against a switch that does nothing (audited 2026-08-19)
    set_but_unwired_flags: list[str] = Field(default_factory=list)
    # flags that ARE read by code but sit inside a subsystem that is not running — the same
    # "you set it, nothing happened" outcome one level out (audited 2026-08-24)
    degraded_experimental_flags: list[str] = Field(default_factory=list)
    # the memory hierarchy's own state and reason. Every MEMORY_* slice runs inside it, so
    # without this the flag list above says "active" and this surface cannot show the outage.
    memory: dict = Field(default_factory=dict)
    # guard settings that parse cleanly and protect nothing — a blocked-namespace entry that
    # cannot match any namespace, or an autonomy override the parser drops (audited 2026-08-20)
    unenforceable_guard_config: list[str] = Field(default_factory=list)
    # blast-radius / write-authority brakes (P3)
    kill_switch_engaged: bool                       # ⇒ all autonomous writes denied
    change_freeze: bool                             # ⇒ deny-by-default change window
    spend_cap_usd: float                            # 0 = unlimited
    # the fourth brake on the same path (ADR-102). `enabled` is the flag; `operating` is
    # whether it can actually act — they differ whenever the flag is on and there is no
    # outcome store to read, which is a brake reported as on that is not in the path.
    autonomy_promotion: dict = Field(default_factory=dict)


async def _autonomy_promotion() -> dict:
    """The A3 statistical brake's live state, or why it is not acting. Never raises.

    A read failure is reported as *not operating* with the error in ``reason`` — the same
    distinction the watchtower makes when it decides whether to revoke. What this must never do
    is answer "clean" for a store it could not read: this is the surface an operator checks to
    confirm a brake, and `promotion_engine.read_outcomes` already records what an unreadable
    source reading as a clean one does to fast-down-slow-up.
    """
    import time

    from app.autonomy.promotion_source import autofix_status, autofix_status_unavailable

    if not settings.KI_V5_STATISTICAL_PROMOTION:
        return autofix_status_unavailable("flag off")
    pool = memory_service._pool
    if pool is None:
        return autofix_status_unavailable(
            "no outcome store — the brake is not operating; A3 is governed by the allowlist alone")
    try:
        return await autofix_status(pool, time.time() / 86400.0)
    except Exception as exc:
        return autofix_status_unavailable(f"outcome store unreadable: {exc}")


@router.get("/v5/status", response_model=V5Status)
async def v5_status():
    return V5Status(
        arm=arm(),
        version=code_version(),
        cortex_v5_enabled=settings.CORTEX_V5_ENABLED,
        active_flags=active_experimental_flags(),
        set_but_unwired_flags=set_but_unwired_flags(),
        degraded_experimental_flags=degraded_experimental_flags(),
        memory=memory_status(),
        unenforceable_guard_config=unenforceable_guard_config(),
        kill_switch_engaged=kill_switch_engaged(),
        change_freeze=change_freeze_active(),
        spend_cap_usd=settings.KI_V5_SPEND_CAP_USD,
        autonomy_promotion=await _autonomy_promotion(),
    )
