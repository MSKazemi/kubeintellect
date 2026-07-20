"""Predictive-detection fusion (v5 P4).

ADR-010 anticipatory detection already fires `severity="predicted"` findings before a failure
realizes, but today they cap at an A1 finding (advise only). P4 fuses them into the agentic loop: a
prediction LAUNCHES a bounded, READ-ONLY investigation of the leading indicators *now*, so the
operator gets grounded evidence before the incident — never a mutation (a prediction is
lower-confidence than a realized failure; roadmap §9 / ADR-010 safety contract).

Reuses the watchdog investigation machinery (a prediction is just another reason to look). Pure
task-building is unit-tested; dispatch is fire-and-forget via the harness fan-out.
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.sensorium.change_watchdog import WatchdogTask
from app.sensorium.watchdog_dispatch import investigate


def is_prediction(finding: Any) -> bool:
    return getattr(finding, "severity", "warning") == "predicted"


def objective_for(finding: Any) -> str:
    """A read-only investigation objective for a predicted finding."""
    eta = getattr(finding, "eta_minutes", None)
    eta_txt = f" (ETA ~{eta:.0f}m)" if isinstance(eta, (int, float)) else ""
    return (f"PREDICTED {getattr(finding, 'playbook', 'issue')} for "
            f"{getattr(finding, 'object_name', '?')} in {getattr(finding, 'namespace', '?')}{eta_txt}"
            " — investigate the leading indicators NOW (read-only) and report whether it is realizing;"
            " do not take any mutating action, a prediction never drives a fix.")


def task_for(finding: Any) -> WatchdogTask:
    """Build a read-only investigation task from a predicted finding."""
    obj = getattr(finding, "object_name", "?")
    ns = getattr(finding, "namespace", "?")
    return WatchdogTask(
        target=obj, kind="predicted", objective=objective_for(finding),
        created_epoch=0.0, ttl_seconds=settings.KI_V5_WATCHDOG_TTL_SECONDS,
        dedup_key=f"predicted/{ns}/{obj}",
    )


async def fuse(finding: Any, *, runner=None) -> Any:
    """If ``finding`` is a prediction and fusion is on, launch its read-only investigation.

    Returns the investigation result, or None when skipped (not a prediction / flag off / failure).
    """
    if not (settings.CORTEX_V5_ENABLED and settings.KI_V5_PREDICTIVE_FUSION and is_prediction(finding)):
        return None
    return await investigate(task_for(finding), runner=runner)
