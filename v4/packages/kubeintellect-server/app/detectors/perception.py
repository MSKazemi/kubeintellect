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
    # Why `sensorium` is `disabled`, when it is. Four unrelated situations end at "no engine" —
    # switched off, nothing compiled to watch with, a start that raised, and a leader-election
    # standby — and only the first is a configuration choice. `""` while perceiving. Last and
    # defaulted so the positional constructions that predate it keep working.
    sensorium_reason: str = ""
    # Observations the watch queue dropped before any detector saw them. A THIRD way to be
    # blind while looking connected, and the only one that leaves every field above healthy:
    # the stream is up, Prometheus answers, and the events were thrown away in between.
    # Defaulted for the same reason as `sensorium_reason`.
    shed_total: int = 0
    queue_high_water: int = 0

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
    from app.sensorium.k8s_watcher import any_stream_connected, queue_stats, stream_health

    # Read once, and on BOTH return paths: a sensorium that has since gone away can still
    # have shed observations while it was up, and reporting 0 there would be a claim rather
    # than the absence of one.
    queue = queue_stats()
    shed = int(queue.get("shed_total") or 0)
    high_water = int(queue.get("high_water") or 0)

    if engine is None:
        engine = get_engine()
    if engine is None:
        from app.detectors.service import sensorium_absence
        reason, detail = sensorium_absence()
        return PerceptionState(
            DISABLED, 0, OFF, 0, None, [], sensorium_reason=_absence_phrase(reason, detail),
            shed_total=shed, queue_high_water=high_water,
        )

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
        sensorium_reason="",
        detectors=len(engine.detectors),
        predictive=predictive,
        predictive_detectors=trend_detectors,
        predictive_error=getattr(engine, "last_trend_error", None),
        streams=streams,
        shed_total=shed,
        queue_high_water=high_water,
    )


def perception_gaps(state: PerceptionState) -> list[str]:
    """One sentence per reason a finding could not have been produced.

    Empty exactly when every instrument was able to look **and** what it saw reached the
    detectors. `predictive == "off"` is a deliberate configuration
    (`PREDICTIVE_DETECTION_ENABLED` is `false` by default), not an outage, and is not a gap —
    the same rule `kq findings` applies.

    Three independent ways to fail, and the third is not an instrument: the watch stream can
    be down, Prometheus can be unreachable, and the queue between the stream and the
    detectors can have dropped what the stream did see.
    """
    gaps: list[str] = []
    if state.sensorium == DISABLED:
        # The fallback is not decoration: an empty reason would append an empty gap, which
        # reads as "an instrument could not look, and we will not say which" — worse than the
        # single sentence this replaced.
        gaps.append(state.sensorium_reason or _absence_phrase("", ""))
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

    # Independent of both instruments above, and the reason this belongs in the shared
    # classifier rather than in one surface: the watch queue sheds the OLDEST observation on
    # overflow instead of applying backpressure to `kubectl`, so a fully connected sensorium
    # can be losing perception with every field above reading healthy. `kq findings` learned
    # to say so on 2026-08-24; putting the rule only there recreated exactly the divergence
    # this module was written to end — the digest calling a window quiet that `kq findings`
    # refuses to call clear.
    if state.shed_total > 0:
        gaps.append(
            f"the observation queue dropped {state.shed_total} event(s) before any detector "
            f"saw them (queue high-water {state.queue_high_water}) — findings are incomplete "
            "for this window")
    return gaps


def _absence_phrase(reason: str, detail: str) -> str:
    """One truthful clause per way the engine can be missing.

    The point of the split: a **standby** replica is behaving correctly and a **failed start**
    is an outage, and the sentence these surfaces printed named neither — it asserted
    `SENSORIUM_ENABLED=false, or no compiled detectors loaded`, which on those two replicas is
    simply not true.
    """
    from app.detectors import service as _s

    if reason == _s.DISABLED_BY_FLAG:
        return ("the sensorium is switched off (SENSORIUM_ENABLED=false) — no detector "
                "finding could have been produced")
    if reason == _s.NO_DETECTORS:
        return ("the sensorium loaded no compiled detectors, so it did not start — no "
                "detector finding could have been produced")
    if reason == _s.START_FAILED:
        return (f"the sensorium FAILED to start ({detail or 'no reason recorded'}) — this "
                f"replica has been perceiving nothing since, and this is an outage rather "
                f"than a setting")
    if reason == _s.STANDBY:
        return ("this replica is a leader-election standby and watches nothing by design; "
                "the replica holding the singleton lock is the one that perceives, so read "
                "its findings, not this replica's silence")
    if reason == _s.STOPPED:
        return ("the sensorium has been stopped (the process is shutting down) — no detector "
                "finding could have been produced")
    return ("the sensorium has not started yet — no detector finding could have been produced")


def _stream_reasons(streams: list[dict]) -> str:
    parts = [
        f"{s.get('name', '?')}: {s.get('last_error') or 'no reason recorded'}"
        for s in streams
    ]
    return "; ".join(parts) if parts else "no reason recorded"
