"""Detector engine — evaluates compiled predicates against the observation stream.

Semantics (per design/v4/detector-predicates.md):

- **Debounce**: a matching observation *arms* the (playbook, namespace, object)
  key; the detector fires only if the condition is still armed after
  debounce_seconds (debounce 0 → fires immediately).
- **Transition dedup**: a fired key does not re-fire while the condition
  persists. A pod_status observation that *stops* matching clears the key
  (state transition), allowing a future re-fire. Event-armed keys clear after
  a TTL without new matches (events are discrete — there is no "recovered"
  event to observe).
- **Zero tokens**: nothing in this module calls an LLM. Firings become
  Findings: an in-memory ring (served by /v1/findings) plus a flight-recorder
  entry under episode "findings:<cluster_id>".
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from app.core.config import settings
from app.db import flight_recorder
from app.detectors.models import DetectBlock, Finding, TrendPredicate
from app.sensorium.observations import Observation
from app.tools.prometheus_tool import query_prometheus_series
from app.utils.logger import get_logger

logger = get_logger(__name__)

_EVENT_ARM_TTL = 600.0   # seconds an event-armed key survives without a new match
_TICK_INTERVAL = 1.0
_TREND_INTERVAL = 60.0       # range queries are expensive — poll once a minute
_PREDICTED_REFIRE_TTL = 1800.0   # don't re-emit the same prediction within this window

_FINDINGS_RING_SIZE = 500


@dataclass
class _KeyState:
    armed_at: float
    last_match: float
    fired: bool = False
    evidence: str = ""
    source: str = "watch"


@dataclass
class DetectorEngine:
    detectors: tuple[DetectBlock, ...]
    cluster_id: str = "unknown"
    # Called with each Finding as it fires (sync, must not raise) — the
    # watchtower subscribes here (ADR-003).
    on_finding: Callable[[Finding], None] | None = None
    # Shadow detectors (ADR-012): NL-authored / unpromoted candidates. They are
    # evaluated for precision but NEVER reach on_finding (the watchtower).
    shadow_detectors: tuple[DetectBlock, ...] = ()
    findings: deque = field(default_factory=lambda: deque(maxlen=_FINDINGS_RING_SIZE))
    shadow_findings: deque = field(default_factory=lambda: deque(maxlen=_FINDINGS_RING_SIZE))
    _states: dict[tuple[str, str, str], _KeyState] = field(default_factory=dict)
    _shadow_states: dict[tuple[str, str, str], _KeyState] = field(default_factory=dict)
    _predicted_fired: dict[tuple[str, str, str], float] = field(default_factory=dict)
    _tick_task: asyncio.Task | None = None
    # Predictive-detection visibility (ADR-010). None ⇒ the last trend sweep reached Prometheus.
    # Set ⇒ it did not, and every "no prediction" since then is an absence of evidence, not
    # evidence of absence. Surfaced by GET /v1/findings as `predictive: blind`.
    trend_blind_since: float | None = None
    last_trend_error: str | None = None

    # ── Stream input ──────────────────────────────────────────────────────────

    def process(self, obs: Observation) -> list[Finding]:
        """Feed one observation; returns any findings fired synchronously."""
        now = obs.ts
        fired: list[Finding] = []
        deleted = obs.kind == "pod_status" and obs.fields.get("watch_type") == "DELETED"
        for det in self.detectors:
            key = (det.playbook, obs.namespace, obs.name)
            matched = any(p.matches(obs) for p in det.watch_predicates)
            state = self._states.get(key)

            if deleted:
                # A DELETED watch event proves the object is gone — its
                # condition can no longer be pending/stuck. Disarm any armed key
                # so a *normal* termination (Terminating → removed within the
                # grace period, whose final DELETED object still computes
                # status=Terminating and therefore still "matches") never fires.
                # A genuinely stuck pod is never DELETED, so it still fires on
                # debounce. This is the fix for the churn false-positives.
                if state is not None and any(
                    p.kind in ("Pod", "Node") for p in det.watch_predicates
                ):
                    del self._states[key]
                continue
            if matched:
                if state is None:
                    state = _KeyState(armed_at=now, last_match=now, evidence=_summarise(obs))
                    self._states[key] = state
                else:
                    state.last_match = now
                if not state.fired and now - state.armed_at >= det.debounce_seconds:
                    fired.append(self._fire(det, obs.namespace, obs.name, state))
            elif state is not None and obs.kind == "pod_status":
                # Contradicting steady-state signal — the condition cleared.
                # (Only pod/node status observations are continuous; a
                # non-matching *event* says nothing about recovery.)
                if any(p.kind in ("Pod", "Node") for p in det.watch_predicates):
                    del self._states[key]
        if self.shadow_detectors:
            self._process_shadow(obs)
        return fired

    def _process_shadow(self, obs: Observation) -> None:
        """Evaluate shadow detectors — fire into the candidate buffer ONLY.

        The safety boundary of ADR-012: a shadow detector accrues precision but
        the watchtower callback (on_finding) is never invoked for it.
        """
        now = obs.ts
        deleted = obs.kind == "pod_status" and obs.fields.get("watch_type") == "DELETED"
        for det in self.shadow_detectors:
            key = (det.playbook, obs.namespace, obs.name)
            matched = any(p.matches(obs) for p in det.watch_predicates)
            state = self._shadow_states.get(key)
            if deleted:
                if state is not None and any(
                    p.kind in ("Pod", "Node") for p in det.watch_predicates
                ):
                    del self._shadow_states[key]
                continue
            if matched:
                if state is None:
                    state = _KeyState(armed_at=now, last_match=now, evidence=_summarise(obs))
                    self._shadow_states[key] = state
                else:
                    state.last_match = now
                if not state.fired and now - state.armed_at >= det.debounce_seconds:
                    self._emit_shadow(det, obs.namespace, obs.name, state)
            elif state is not None and obs.kind == "pod_status":
                if any(p.kind in ("Pod", "Node") for p in det.watch_predicates):
                    del self._shadow_states[key]

    def _emit_shadow(self, det: DetectBlock, namespace: str, name: str, state: _KeyState) -> Finding:
        state.fired = True
        finding = Finding(
            playbook=det.playbook,
            cluster_id=self.cluster_id,
            namespace=namespace,
            object_name=name,
            evidence=state.evidence,
            first_seen=state.armed_at,
            source="shadow",
        )
        self.shadow_findings.append(finding)
        flight_recorder.record(
            f"shadow-findings:{self.cluster_id}", "finding", finding.to_dict()
        )
        logger.info(
            f"shadow_detector_fired playbook={det.playbook} ns={namespace} object={name}"
            f" evidence={state.evidence[:120]!r}"
        )
        # Deliberately NEVER calls self.on_finding — shadow detectors cannot act.
        return finding

    def tick(self, now: float | None = None) -> list[Finding]:
        """Fire armed-and-debounced keys; expire stale event arms."""
        now = now if now is not None else time.time()
        fired: list[Finding] = []
        for key, state in list(self._states.items()):
            playbook, namespace, name = key
            det = self._by_name(playbook)
            if det is None:
                del self._states[key]
                continue
            if not state.fired and now - state.armed_at >= det.debounce_seconds:
                fired.append(self._fire(det, namespace, name, state))
            elif state.fired and now - state.last_match > _EVENT_ARM_TTL:
                del self._states[key]  # stale — allow future re-fire
        return fired

    # ── Internals ─────────────────────────────────────────────────────────────

    def _by_name(self, playbook: str) -> DetectBlock | None:
        for det in self.detectors:
            if det.playbook == playbook:
                return det
        return None

    def _fire(self, det: DetectBlock, namespace: str, name: str, state: _KeyState) -> Finding:
        state.fired = True
        finding = Finding(
            playbook=det.playbook,
            cluster_id=self.cluster_id,
            namespace=namespace,
            object_name=name,
            evidence=state.evidence,
            first_seen=state.armed_at,
            source=state.source,
        )
        return self._emit(finding)

    def _emit(self, finding: Finding) -> Finding:
        """Common publish path: ring + flight recorder + watchtower callback."""
        self.findings.append(finding)
        flight_recorder.record(
            f"findings:{self.cluster_id}", "finding", finding.to_dict()
        )
        logger.info(
            f"detector_fired playbook={finding.playbook} ns={finding.namespace}"
            f" object={finding.object_name} severity={finding.severity}"
            f" evidence={finding.evidence[:120]!r}"
        )
        if self.on_finding is not None:
            try:
                self.on_finding(finding)
            except Exception as exc:
                logger.warning(f"on_finding callback error: {exc}")
        return finding

    # ── Anticipatory / predictive detection (ADR-010) ──────────────────────────

    async def evaluate_trends(self, now: float | None = None) -> list[Finding]:
        """Project each trend predicate's metric and fire `predicted` findings.

        Zero-token: the only work is a range PromQL fetch + a least-squares slope.
        Fail-open: a Prometheus outage or malformed series yields no findings, never an
        exception. Predicted findings are capped at A1 in the watchtower.

        Fail-open, but **not fail-silent**. A layer whose entire job is to warn before a failure
        is worthless if it stops warning without saying so, and that is what happened: the
        outage arrived as the discarded half of a tuple, so `trend_blind_since` /
        `last_trend_error` now hold it and `GET /v1/findings` reports `predictive: blind`.
        """
        now = now if now is not None else time.time()
        fired: list[Finding] = []
        for det in self.detectors:
            for tp in det.trend_predicates:
                try:
                    series, query_error = await asyncio.to_thread(
                        query_prometheus_series, tp.metric, tp.window_minutes
                    )
                except Exception as exc:
                    query_error, series = str(exc), []
                if query_error is not None:
                    # Not "nothing is trending" — "nobody asked". `_query_raw` never raises, so
                    # the exception handler above could not see a Prometheus outage at all: the
                    # error came back as the discarded half of a tuple, the loop saw `[]`, and
                    # the predictive layer went silent with no finding and no log line.
                    self.trend_blind_since = self.trend_blind_since or now
                    self.last_trend_error = query_error
                    logger.warning(
                        f"trend_query_unavailable playbook={det.playbook} "
                        f"metric={tp.metric[:80]!r}: {query_error}"
                    )
                    continue
                self.trend_blind_since = None
                self.last_trend_error = None
                for s in series or []:
                    finding = self._project_series(det, tp, s, now)
                    if finding is not None:
                        fired.append(finding)
        return fired

    def _project_series(
        self, det: DetectBlock, tp: TrendPredicate, series: dict, now: float
    ) -> Finding | None:
        samples = _extract_samples(series)
        if len(samples) < 3:
            return None
        eta_s, _r2 = project_eta(samples, tp.threshold, tp.direction, tp.min_r2)
        if eta_s is None:
            return None
        eta_min = eta_s / 60.0
        if eta_min <= 0 or eta_min > tp.projection_horizon_minutes:
            return None
        if eta_min > tp.fire_if_eta_within_minutes:
            return None
        namespace, name = _series_target(series, tp.object_label)
        key = (det.playbook, namespace, name)
        last = self._predicted_fired.get(key)
        if last is not None and now - last < _PREDICTED_REFIRE_TTL:
            return None
        self._predicted_fired[key] = now
        evidence = (
            f"predicted {det.playbook}: {tp.metric[:80]} {tp.direction} toward "
            f"{tp.threshold} — crossing in ~{eta_min:.0f}m"
        )
        return self._emit(
            Finding(
                playbook=det.playbook,
                cluster_id=self.cluster_id,
                namespace=namespace,
                object_name=name,
                evidence=evidence,
                first_seen=now,
                fired_at=now,
                source="trend",
                severity="predicted",
                eta_minutes=round(eta_min, 1),
            )
        )

    async def run(self) -> None:
        """Background tick loop (debounce expiry + stale-arm cleanup)."""
        while True:
            await asyncio.sleep(_TICK_INTERVAL)
            try:
                self.tick()
            except Exception as exc:
                logger.warning(f"detector_tick_error: {exc}")

    async def run_trends(self, interval: float = _TREND_INTERVAL) -> None:
        """Background trend-projection loop (ADR-010); separate from the 1s tick."""
        while True:
            await asyncio.sleep(interval)
            try:
                await self.evaluate_trends()
            except Exception as exc:
                logger.warning(f"detector_trend_error: {exc}")

    def recent_findings(self, limit: int = 100, since: float = 0.0) -> list[dict]:
        out = [f.to_dict() for f in self.findings if f.fired_at >= since]
        return out[-limit:]


def project_eta(
    samples: list[tuple[float, float]],
    threshold: float,
    direction: str = "rising",
    min_r2: float = 0.5,
) -> tuple[float | None, float]:
    """Least-squares projection of the threshold-crossing ETA, in seconds.

    Returns (eta_seconds, r2). eta is None when: <2 samples, the series is flat
    (zero variance), the fit is noisier than min_r2, the slope moves *away* from
    the threshold, the metric is already past it, or the crossing is in the past.
    Pure + deterministic — no numpy, no LLM.
    """
    n = len(samples)
    if n < 2:
        return None, 0.0
    x0 = samples[0][0]
    xs = [s[0] - x0 for s in samples]
    ys = [s[1] for s in samples]
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None, 0.0
    sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    slope = sxy / sxx
    r2 = (sxy * sxy) / (sxx * syy)
    if r2 < min_r2:
        return None, r2
    current = ys[-1]
    if direction == "falling":
        if slope >= 0 or current <= threshold:
            return None, r2
    else:  # rising
        if slope <= 0 or current >= threshold:
            return None, r2
    eta = (threshold - current) / slope
    if eta <= 0:
        return None, r2
    return eta, r2


def _extract_samples(series: dict) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for v in series.get("values") or []:
        try:
            ts = float(v[0])
            val = float(v[1])
        except (TypeError, ValueError, IndexError):
            continue
        if val != val or val in (float("inf"), float("-inf")):  # NaN / Inf
            continue
        out.append((ts, val))
    return out


def _series_target(series: dict, object_label: str | None) -> tuple[str, str]:
    labels = series.get("metric", {}) or {}
    namespace = labels.get("namespace") or "cluster"
    if object_label:
        name = labels.get(object_label) or "unknown"
    else:
        name = (
            labels.get("pod")
            or labels.get("persistentvolumeclaim")
            or labels.get("node")
            or labels.get("container")
            or labels.get("instance")
            or "unknown"
        )
    return namespace, name


# A Kubernetes event `message` is arbitrary cluster text: mount failures name Secrets, image
# pulls name registries and auth errors, probe failures quote URLs and payloads. Every other
# field a finding carries is an enum or an object name; this one is not.
_WITHHELD_MESSAGE = "<withheld: protected namespace>"


def _summarise(obs: Observation) -> str:
    """One line of evidence for a finding.

    **The sensorium watches cluster-wide and the blocklist does not reach it** — by design: it
    runs `kubectl get pods -A --watch` as a raw subprocess, not through `run_kubectl`, and it has
    to, or the watchtower could not tell a quiet cluster from an unwatched one (pass 80). But
    until 2026-08-20 the *free text* came with it: a finding in `kube-system`, `monitoring` or
    `kubeintellect` carried up to 140 characters of raw event message, and `GET /v1/findings`
    returns it to every caller without consulting a role at all — while the RBAC table in
    `docs/security.md` said infrastructure-namespace reads were **Blocked** for admin, operator
    and readonly alike.

    The signal is kept, because an operator does need to know coredns is crash-looping and
    dropping the finding would blind the digest. The arbitrary text is not: `reason` is a
    Kubernetes-defined enum and survives, the message does not.
    """
    if obs.kind == "pod_status":
        return f"pod status={obs.fields.get('status', '')}"
    if obs.kind == "event":
        reason = obs.fields.get("reason", "")
        if obs.namespace.strip().lower() in settings.kubectl_blocked_namespaces:
            return f"event reason={reason} message={_WITHHELD_MESSAGE}"
        message = obs.fields.get("message", "")[:140]
        return f"event reason={reason} message={message}"
    if obs.kind == "node_status":
        return f"node status={obs.fields.get('status', '')}"
    return obs.kind


def load_detectors() -> tuple[DetectBlock, ...]:
    """Build detector set from the playbook registry's compiled detect blocks."""
    from app.agent.playbooks import list_playbooks

    blocks = []
    for pb in list_playbooks():
        det = getattr(pb, "detect", None)
        if det is not None:
            blocks.append(det)
    return tuple(blocks)


def _is_detect_block(predicate: object) -> bool:
    """A DB detector row holds a recompilable detect block iff it has predicates.

    (Consolidation 'learned' rows store {derived_from_playbooks, pattern} — those
    are not detect blocks and are skipped.)

    ``promql`` is not in the list: nothing evaluates it, so a row carrying only
    PromQL compiles to a detector that can never fire. See ``parse_detect_block``.
    """
    return isinstance(predicate, dict) and any(
        k in predicate for k in ("watch_predicates", "trend_predicates")
    )


async def load_db_detectors(
    cluster_id: str = "global",
) -> tuple[tuple[DetectBlock, ...], tuple[DetectBlock, ...]]:
    """Compile NL-authored / promoted detectors from the DB → (active, shadow).

    Only rows whose `predicate` is a real detect block are compiled. No pool, or a query that
    returns nothing, means exactly one thing: **there are no DB detectors**, and empty tuples
    say so.

    A query that FAILED means something else entirely, and raises `DetectorStoreUnavailable`
    rather than returning empty tuples. It used to return them, and the only caller assigns the
    result straight into the live engine — so a transient DNS blip silently unloaded every
    promoted detector until the next successful refresh, measured 2026-08-24:

        DB reachable, 3 promoted   engine.detectors = 23  (playbooks 20 + db 3)
        next refresh, query raises engine.detectors = 20  (playbooks 20 + db 0)   ← disarmed

    `_refresh_db_detectors` already had the correct handler for this; it was unreachable.

    **`global` rows are loaded on every cluster.** The write path — `stage_candidate`,
    `promote_candidate`, `demote_candidate`, `list_detectors` — all default `cluster_id="global"`,
    while this reader is called with the deployment's own `get_cluster_id()`. Nothing reconciled
    the two, so on any deployment that sets `CLUSTER_ID` (which the Helm chart does) an
    NL-authored detector was stored, listed as `shadow`, promotable and demotable — and never
    loaded, never evaluated, by design of the query alone.

    Measured on the campaign's soak cluster 2026-08-25: 8 detectors in the DB under
    `cluster_id='global'`, engine loading for `f3-shadow-soak-r2`, zero rows matched. The 24-hour
    F3 shadow soak that ran against it reported `false_positive_rate: 0.0` — a flawless score
    produced by evaluating nothing, which is exactly the outcome its own driver was written to
    refuse. The driver checked the DB status, because that is the only thing the API exposed.

    `global` is not a magic cluster name here; it is the write path's word for "everywhere", and
    this is where that word finally means something. A row stored under a specific cluster id
    still loads only there.
    """
    from app.detectors.models import parse_detect_block
    from app.detectors.predicate_shape import (
        predicate_health_errors,
        predicate_liveness_errors,
        trend_liveness_errors,
    )
    from app.detectors.review import DetectorStoreUnavailable
    from app.memory import service

    pool = service._pool
    if pool is None:
        return (), ()
    try:
        rows = await pool.fetch(
            "SELECT name, predicate, status FROM detectors"
            " WHERE cluster_id IN ($1, 'global') AND status IN ('active', 'shadow')",
            cluster_id,
        )
    except Exception as exc:
        logger.warning(f"load_db_detectors: query failed: {exc}")
        raise DetectorStoreUnavailable(f"detector store query failed: {exc}") from exc
    active: list[DetectBlock] = []
    shadow: list[DetectBlock] = []
    # Four of the five ways a row is dropped below used to `continue` in complete silence, so a
    # table full of malformed rows was indistinguishable from an empty table. Counted, then
    # reported once — a per-row warning on every refresh would be its own kind of noise.
    skipped: list[str] = []
    for r in rows:
        pred = r["predicate"]
        if isinstance(pred, str):
            try:
                pred = json.loads(pred)
            except json.JSONDecodeError:
                skipped.append(f"{r['name']}: predicate is not valid JSON")
                continue
        if not _is_detect_block(pred):
            skipped.append(f"{r['name']}: predicate is not a detect block")
            continue
        try:
            block = parse_detect_block(r["name"], pred)
        except Exception as exc:
            skipped.append(f"{r['name']}: predicate did not compile ({exc})")
            continue
        if block is None:
            skipped.append(f"{r['name']}: predicate compiled to nothing")
            continue
        # Compiling is not the same as being able to fire. `validate_detect_block` gates the
        # authoring endpoint, but it is not the only way a row reaches this table —
        # consolidation writes here too, promotion only flips `status`, and any row stored
        # before that gate existed is still here. This is the one point every DB detector
        # passes through, so it is where the check has to be. A dead SHADOW row is the worst
        # case: it accrues zero firings, and zero firings are read as precision evidence by
        # the human deciding whether to promote it.
        #
        # The refusal is PER PREDICATE, not per row, and that distinction is load-bearing.
        # `nl:soak-replicas-short` carries a trend predicate pinned to the authoring model's
        # own `{deployment="your-deployment-name"}` template AND a Pod predicate `^Running$`.
        # The template can never match a series; `^Running$` matches every healthy pod on the
        # cluster. Refusing the whole row for the first would have deleted the second — and the
        # second is a false-positive source on a lane whose endpoint IS the false-positive rate.
        # Dropping it would have moved that rate toward the pre-registered direction by
        # removing evidence, which is the worst way to be wrong here.
        live_watch = tuple(p for p in block.watch_predicates
                           if not predicate_liveness_errors(p))
        # Trend predicates were exempt until 2026-08-25. Two of the eight rows on the F3 soak
        # cluster forecast `kube_deployment_status_replicas{deployment="your-deployment-name"}`
        # — the authoring model's own template, stored verbatim. They loaded, matched no series,
        # and their silence was indistinguishable from a healthy cluster.
        live_trend = tuple(tp for tp in block.trend_predicates
                           if not trend_liveness_errors(tp))
        dropped = [msg for p in block.watch_predicates for msg in predicate_liveness_errors(p)]
        dropped += [msg for tp in block.trend_predicates for msg in trend_liveness_errors(tp)]
        if dropped and not (live_watch or live_trend):
            logger.warning(
                "db_detector_can_never_fire name=%r status=%r reason=%s — not loaded",
                r["name"], r["status"], dropped[0],
            )
            continue
        if dropped:
            logger.warning(
                "db_detector_predicate_can_never_fire name=%r status=%r reason=%s — that "
                "predicate is dropped; the detector still loads with %d live predicate(s)",
                r["name"], r["status"], dropped[0], len(live_watch) + len(live_trend),
            )
            block = replace(block, watch_predicates=live_watch, trend_predicates=live_trend,
                            dropped_predicates=tuple(dropped))
        # A live predicate that matches a healthy object is NOT dropped — see
        # `DetectBlock.fires_on_healthy`. It is loaded, evaluated, and named, so an operator
        # reading the detector's findings can see why there are so many of them.
        unhealthy = [msg for p in block.watch_predicates for msg in predicate_health_errors(p)]
        if unhealthy:
            logger.warning(
                "db_detector_fires_on_healthy_objects name=%r status=%r reason=%s — LOADED "
                "anyway; its findings are not evidence of a fault",
                r["name"], r["status"], unhealthy[0],
            )
            block = replace(block, fires_on_healthy=tuple(unhealthy))
        (active if r["status"] == "active" else shadow).append(block)
    if skipped:
        logger.warning(
            f"load_db_detectors: {len(skipped)} of {len(rows)} stored detector(s) were not "
            f"loaded — {'; '.join(skipped[:5])}"
            + (f" (+{len(skipped) - 5} more)" if len(skipped) > 5 else "")
        )
    return tuple(active), tuple(shadow)
