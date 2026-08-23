"""Perception state — one answer to "is anything actually watching?".

Two surfaces make the same claim about the same window and used to compute it
separately: `GET /v1/findings` (which `kq findings` renders) and the morning
digest (`kq digest`). They disagreed. Measured 2026-08-20 with no watch stream
connected and every source readable, `/v1/findings` reported
`{"sensorium": "starting", "findings": []}` and `kq findings` refused to call it
clear — while the digest over the same period said
*"Quiet watch: no findings in the last 24h."*

An all-clear is a claim about **perception**, not about the record. The recorder
can be perfectly healthy and hold nothing because nothing was ever looking. So
the classification lives here once, and both surfaces read it.

**What this does not claim.** These are the states *now*, not a history of the
window. A watch stream that died an hour ago and has since reconnected reads as
`active`; the digest's window is not reconstructed from stream history, because
none is kept. Reported gaps are therefore a lower bound on the blindness in the
window, never an upper one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.detectors.engine import DetectorEngine

# `sensorium` values
DISABLED = "disabled"
STARTING = "starting"
ACTIVE = "active"
STOPPED = "stopped"
RECONNECTING = "reconnecting"

# `predictive` values
OFF = "off"
BLIND = "blind"


@dataclass(frozen=True)
class PerceptionState:
    """What each instrument can currently see. `streams` is the raw per-stream health."""

    sensorium: str
    detectors: int
    predictive: str
    predictive_detectors: int
    predictive_error: str | None
    streams: list[dict]

    @property
    def watching(self) -> bool:
        """True only when a watch stream is connected — the one state in which an
        empty findings list is evidence rather than an absence of looking."""
        return self.sensorium == ACTIVE


def perception_state(engine: "DetectorEngine | None" = None) -> PerceptionState:
    """Classify both instruments. Never raises: perception is not a request path.

    `engine` is looked up from `detectors.service` when not supplied; a caller that
    already holds it passes it so there is exactly one lookup per request. Either way
    a missing engine means `disabled` — there is nothing to perceive with.
    """
    from app.core.config import settings
    from app.detectors.service import get_engine
    from app.sensorium.k8s_watcher import any_stream_connected, stream_health

    if engine is None:
        engine = get_engine()
    if engine is None:
        return PerceptionState(DISABLED, 0, OFF, 0, None, [])

    streams = stream_health()
    # "active" is a claim about perception, not about object lifetime. An engine
    # exists regardless; what decides the word is whether any `kubectl --watch`
    # stream is actually connected. The watch loop returns permanently on a
    # missing kubectl, so "not watching" can hold for the whole process lifetime.
    if not streams:
        sensorium = STARTING
    elif any_stream_connected():
        sensorium = ACTIVE
    elif all(s["stopped"] for s in streams):
        sensorium = STOPPED
    else:
        sensorium = RECONNECTING

    # Same rule one layer over: the predictive (trend) detectors are only watching
    # while Prometheus answers them.
    trend_detectors = sum(1 for d in engine.detectors if getattr(d, "trend_predicates", None))
    if not settings.PREDICTIVE_DETECTION_ENABLED or not trend_detectors:
        predictive = OFF
    elif getattr(engine, "trend_blind_since", None) is not None:
        predictive = BLIND
    else:
        predictive = ACTIVE

    return PerceptionState(
        sensorium=sensorium,
        detectors=len(engine.detectors),
        predictive=predictive,
        predictive_detectors=trend_detectors,
        predictive_error=getattr(engine, "last_trend_error", None),
        streams=streams,
    )


def perception_gaps(state: PerceptionState) -> list[str]:
    """One sentence per instrument that could not have produced a finding.

    Empty exactly when every instrument was able to look. `predictive == "off"` is a
    deliberate configuration (`PREDICTIVE_DETECTION_ENABLED` is `false` by default),
    not an outage, and is not a gap — the same rule `kq findings` applies.
    """
    gaps: list[str] = []
    if state.sensorium == DISABLED:
        gaps.append(
            "the sensorium is not running (SENSORIUM_ENABLED=false, or no compiled "
            "detectors loaded) — no detector finding could have been produced")
    elif state.sensorium == STARTING:
        gaps.append(
            "no kubectl watch stream has started — the sensorium was not perceiving, "
            "so no detector finding could have been produced")
    elif state.sensorium == STOPPED:
        reasons = _stream_reasons(state.streams)
        gaps.append(
            "every kubectl watch stream has stopped — no detector finding could have "
            f"been produced ({reasons})")
    elif state.sensorium == RECONNECTING:
        reasons = _stream_reasons(state.streams)
        gaps.append(
            f"no kubectl watch stream is connected (reconnecting: {reasons}) — "
            "detector findings are incomplete for this window")

    if state.predictive == BLIND:
        gaps.append(
            "predictive detection is blind — Prometheus could not be queried "
            f"({state.predictive_error or 'no reason recorded'}), so no predicted "
            "finding could have fired")
    return gaps


def _stream_reasons(streams: list[dict]) -> str:
    parts = [
        f"{s.get('name', '?')}: {s.get('last_error') or 'no reason recorded'}"
        for s in streams
    ]
    return "; ".join(parts) if parts else "no reason recorded"
