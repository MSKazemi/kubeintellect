"""Detector predicate and Finding models (playbook schema v2, ADR-006)."""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field

from app.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class WatchPredicate:
    """One machine-checkable condition over the observation stream.

    kind "Pod"   — status_regex matched against the computed STATUS column.
    kind "Event" — reason/message regexes matched against Warning events;
                   involved_kind optionally narrows the involved object.
    kind "Node"  — status_regex matched against the node condition summary.
    """
    kind: str
    status_regex: re.Pattern | None = None
    reason_regex: re.Pattern | None = None
    message_regex: re.Pattern | None = None
    involved_kind: str | None = None

    def matches(self, obs) -> bool:
        if self.kind == "Pod" and obs.kind == "pod_status":
            return bool(self.status_regex and self.status_regex.search(obs.fields.get("status", "")))
        if self.kind == "Node" and obs.kind == "node_status":
            return bool(self.status_regex and self.status_regex.search(obs.fields.get("status", "")))
        if self.kind == "Event" and obs.kind == "event":
            if obs.fields.get("event_type") and obs.fields["event_type"] != "Warning":
                return False
            if self.involved_kind and obs.fields.get("involved_kind") != self.involved_kind:
                return False
            reason_ok = bool(
                self.reason_regex is None
                or self.reason_regex.search(obs.fields.get("reason", ""))
            )
            message_ok = bool(
                self.message_regex is None
                or self.message_regex.search(obs.fields.get("message", ""))
            )
            # Both present → both must match (the predicates doc relies on
            # message co-conditions to narrow broad reasons like "Failed").
            return reason_ok and message_ok
        return False


@dataclass(frozen=True)
class TrendPredicate:
    """A forecast condition (ADR-010): project a metric toward a threshold.

    Evaluated deterministically (least-squares slope over range PromQL, zero
    tokens). Fires a `severity="predicted"` Finding when the projected
    threshold-crossing ETA is within `fire_if_eta_within_minutes` and the fit is
    non-noisy (`r2 >= min_r2`).
    """
    metric: str                              # range PromQL — one scalar per series over time
    threshold: float                         # the ceiling/floor the metric approaches
    window_minutes: int = 30                 # lookback for the regression
    projection_horizon_minutes: int = 120    # don't trust extrapolation beyond this
    fire_if_eta_within_minutes: int = 30     # fire when projected crossing <= this
    direction: str = "rising"                # rising | falling
    min_r2: float = 0.5                      # ignore noisy/flat series
    object_label: str | None = None          # which series label is the object (default: pod/pvc/node)


@dataclass(frozen=True)
class DetectBlock:
    """The compiled `detect:` block of one playbook."""
    playbook: str
    watch_predicates: tuple[WatchPredicate, ...] = ()
    promql: tuple[str, ...] = ()
    debounce_seconds: int = 0
    trend_predicates: tuple[TrendPredicate, ...] = ()


@dataclass
class Finding:
    """A detector firing — produced with zero LLM tokens."""
    playbook: str
    cluster_id: str
    namespace: str
    object_name: str
    evidence: str                 # one-line summary of the triggering observation
    id: str = field(default_factory=lambda: f"fnd-{uuid.uuid4().hex[:12]}")
    first_seen: float = field(default_factory=time.time)
    fired_at: float = field(default_factory=time.time)
    source: str = "watch"         # watch | trend  ("promql" is reserved but
                                  # unreachable — promql is not evaluated)
    severity: str = "warning"     # warning (realized) | predicted (anticipatory)
    eta_minutes: float | None = None   # for predicted findings: projected time-to-failure

    def to_dict(self) -> dict:
        return {
            "type": "finding",
            "id": self.id,
            "playbook": self.playbook,
            "cluster_id": self.cluster_id,
            "namespace": self.namespace,
            "object": self.object_name,
            "evidence": self.evidence,
            "first_seen": self.first_seen,
            "fired_at": self.fired_at,
            "source": self.source,
            "severity": self.severity,
            "eta_minutes": self.eta_minutes,
        }


def parse_detect_block(playbook_name: str, raw: dict | None) -> DetectBlock | None:
    """Parse a playbook's `detect:` mapping. None/missing → not compiled (LLM-only)."""
    if not isinstance(raw, dict):
        return None

    def _compile(pattern: str | None) -> re.Pattern | None:
        return re.compile(pattern) if pattern else None

    predicates = []
    for entry in raw.get("watch_predicates") or []:
        if not isinstance(entry, dict) or not entry.get("kind"):
            continue
        predicates.append(
            WatchPredicate(
                kind=str(entry["kind"]),
                status_regex=_compile(entry.get("status_regex")),
                reason_regex=_compile(entry.get("reason_regex")),
                message_regex=_compile(entry.get("message_regex")),
                involved_kind=entry.get("involved_kind"),
            )
        )
    promql = tuple(str(q) for q in (raw.get("promql") or []))

    def _cfg(entry: dict, key: str, default, cast, valid, why: str):
        """One trend-predicate knob, distinguishing *absent* from *explicitly set to zero*.

        `entry.get(key) or default` cannot tell those apart, and the consequences run opposite
        ways. `min_r2: 0` deliberately disables the fit-quality gate (`engine.project_eta` does
        `if r2 < min_r2`), so silently restoring 0.5 makes a detector quieter than its author
        wrote it — a false negative nobody notices. The three interval knobs go the other way:
        the engine turns 0 into a predicate that can never fire, which is the dead-detector trap
        this module already refuses to ship for promql-only blocks. So zero is honoured where it
        means something and rejected *out loud* where it means a no-op — never swapped in silence.
        """
        if key not in entry or entry[key] is None:
            return default
        try:
            value = cast(entry[key])
        except (TypeError, ValueError):
            logger.warning(
                "detector %r: %s=%r is not a number — using the default %r",
                playbook_name, key, entry[key], default,
            )
            return default
        if not valid(value):
            logger.warning(
                "detector %r: %s=%r is invalid (%s) — using the default %r instead. The "
                "detector that loads is NOT the one that was written.",
                playbook_name, key, value, why, default,
            )
            return default
        return value

    trends = []
    for entry in raw.get("trend_predicates") or []:
        if not isinstance(entry, dict) or not entry.get("metric") or "threshold" not in entry:
            continue
        trends.append(
            TrendPredicate(
                metric=str(entry["metric"]),
                threshold=float(entry["threshold"]),
                window_minutes=_cfg(
                    entry, "window_minutes", 30, int, lambda v: v > 0,
                    "a window of zero minutes or less collects no samples to fit"),
                projection_horizon_minutes=_cfg(
                    entry, "projection_horizon_minutes", 120, int, lambda v: v > 0,
                    "no projected ETA can fall inside a horizon of zero"),
                fire_if_eta_within_minutes=_cfg(
                    entry, "fire_if_eta_within_minutes", 30, int, lambda v: v > 0,
                    "the engine already drops an ETA of zero or less, so this can never fire"),
                direction=str(entry.get("direction") or "rising"),
                # 0.0 is a real setting here: "fire regardless of fit quality".
                min_r2=_cfg(
                    entry, "min_r2", 0.5, float, lambda v: 0.0 <= v <= 1.0,
                    "r² only ever falls in [0, 1]"),
                object_label=entry.get("object_label"),
            )
        )

    # ⚠️ ``promql`` is DECLARATIVE ONLY — nothing evaluates it.
    #
    # `DetectorEngine.process()` matches `watch_predicates`; the periodic tick
    # evaluates `trend_predicates`. No code path has ever read `DetectBlock.promql`
    # (verified 2026-08-20 across the whole server package). It is parsed, stored,
    # exported to consolidation, advertised to the NL-authoring model as a valid
    # predicate type — and never run. So it must not, on its own, make a block
    # valid: a promql-only detector would load, count toward the detector total,
    # pass the schema check, and then never fire. That is the same trap the
    # `kind:` warning in docs/agent-behaviors.md documents.
    #
    # The 21 `promql:` queries in the shipped playbooks all sit alongside real
    # `watch_predicates`, so nothing that fires today stops firing; what is not
    # true is the extra coverage those queries appear to claim.
    if not predicates and not trends:
        if promql:
            logger.warning(
                "detector %r declares only promql predicates, which are not evaluated — "
                "it can never fire. Add watch_predicates or trend_predicates.",
                playbook_name,
            )
        return None
    return DetectBlock(
        playbook=playbook_name,
        watch_predicates=tuple(predicates),
        promql=promql,
        # 0 is the documented default ("fires immediately"); negative would make the
        # comparison in DetectorEngine always true, i.e. no debounce at all, silently.
        debounce_seconds=_cfg(
            raw, "debounce_seconds", 0, int, lambda v: v >= 0,
            "a negative debounce disables debouncing entirely rather than shortening it"),
        trend_predicates=tuple(trends),
    )
