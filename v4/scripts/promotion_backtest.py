"""ADR-102 Validation-plan simulation (arms 1, 2, 4) on the promotion + demotion rules.

Deterministic (fixed seeds), pure Python, no cluster/DB. Discharges the studies that gate the
constants, isolating each rule so the measured property is attributable to it:

  arm 1(a) false-promotion ≤5% for p ≤ θ            — the LCB coverage guarantee
  arm 1(b) median events-to-promotion at p = θ+0.03 — the rule is not so strict it never promotes
  arm 1(c) demotion latency ≤20 events after a p→p−0.15 collapse (CUSUM/hysteresis)
  arm 2    correlated-burst diversity block + class-drift stale
  arm 4    SPRT comparison arm (adopt only if ≥30% faster; display-only, non-binding)

OUT OF SCOPE (needs unbuilt feature / live data, flagged in the review, not faked):
  arm 3 offline-vs-live M2 calibration — requires the offline-shadow weighting feature AND real
        live-shadow samples; cannot be discharged from synthetic data alone.
"""

from __future__ import annotations

import random
import statistics

from app.autonomy.promotion_stats import (
    Event,
    evaluate_demotion,
    evaluate_promotion,
    rule_for,
)

TRIALS = 4000
TRANSITION = "L2->L3"
RULE = rule_for(TRANSITION)
THETA = RULE.theta
TYPES = ["net", "cpu", "mem", "disk", "cfg"]
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} — {detail}")


def _stream(p: float, n: int, rng: random.Random, *, spacing: float = 1.0) -> list[Event]:
    return [
        Event(ts_days=i * spacing, success=(rng.random() < p),
              incident_id=f"inc-{i}", incident_type=TYPES[i % len(TYPES)], critical=False)
        for i in range(n)
    ]


# ── arm 1(a): false-promotion ≤5% for p ≤ θ ───────────────────────────────────
def arm_1a() -> None:
    n = max(RULE.n_min, 40)
    worst = 0.0
    for p in (0.80, 0.85, 0.88, 0.90):
        rng = random.Random(1000 + int(p * 100))
        rate = sum(1 for _ in range(TRIALS)
                   if evaluate_promotion(TRANSITION, _stream(p, n, rng), now_days=float(n)).promote) / TRIALS
        worst = max(worst, rate)
    record("arm 1(a) false-promotion ≤5% for p≤θ", worst <= 0.05 + 1e-9,
           f"worst={worst:.2%} at n={n}")


# ── arm 1(b): median events-to-promotion at p = θ+0.03 ─────────────────────────
def arm_1b() -> None:
    p = THETA + 0.03
    rng = random.Random(4242)
    firsts = []
    for _ in range(1500):
        events: list[Event] = []
        promoted_at = None
        for i in range(400):
            events.append(Event(ts_days=float(i), success=(rng.random() < p),
                                incident_id=f"inc-{i}", incident_type=TYPES[i % len(TYPES)]))
            if evaluate_promotion(TRANSITION, events, now_days=float(i)).promote:
                promoted_at = i + 1
                break
        if promoted_at:
            firsts.append(promoted_at)
    frac = len(firsts) / 1500
    med = statistics.median(firsts) if firsts else float("inf")
    # A good class (p=θ+0.03) should promote in the vast majority of runs within the window.
    record("arm 1(b) events-to-promotion at p=θ+0.03", frac >= 0.90 and med < 200,
           f"median={med:.0f} events, promoted in {frac:.0%} of runs")


# ── arm 1(c): demotion latency ≤20 events after a p→p−0.15 collapse ────────────
def arm_1c() -> None:
    p_collapse = THETA - 0.15  # 0.75
    rng = random.Random(9001)
    lats = []
    for _ in range(1500):
        # 50 healthy events (window starts clean), then a collapse; sub-daily spacing so CUSUM can bite.
        healthy = [Event(ts_days=i * 0.25, success=True, incident_id=f"h{i}") for i in range(50)]
        base_t = 50 * 0.25
        events = list(healthy)
        latency = None
        for k in range(60):
            events.append(Event(ts_days=base_t + k * 0.25,
                                success=(rng.random() < p_collapse), incident_id=f"c{k}"))
            d = evaluate_demotion("L4", THETA, events, now_days=base_t + k * 0.25)
            if d.demote:
                latency = k + 1
                break
        if latency:
            lats.append(latency)
    med = statistics.median(lats) if lats else float("inf")
    caught = len(lats) / 1500
    record("arm 1(c) demotion latency ≤20 events after collapse", med <= 20 and caught >= 0.95,
           f"median={med:.0f} events, demoted in {caught:.0%} of runs")


# ── arm 2: correlated-burst diversity block + class-drift stale ────────────────
def arm_2() -> None:
    rng = random.Random(303)
    # A high-success BURST: 60 events, all one incident, one type → diversity D must block.
    burst = [Event(ts_days=i * 0.01, success=True, incident_id="storm", incident_type="net")
             for i in range(60)]
    burst_blocked = not evaluate_promotion(TRANSITION, burst, now_days=1.0).promote
    # class drift flags stale (re-qualify), never a silent pass-through.
    drift = evaluate_demotion("L3", 0.95, _stream(0.99, 40, rng), now_days=40.0, class_drift=True)
    record("arm 2 correlated-burst diversity block", burst_blocked,
           "60 same-incident successes did NOT promote")
    record("arm 2 class-drift flags stale", drift.stale and not drift.demote,
           "drift → stale/re-qualify, not silent")


# ── arm 4: SPRT comparison (display-only; adopt only if ≥30% faster) ───────────
def _sprt_decision(events: list[Event], p0: float, p1: float, alpha: float = 0.05, beta: float = 0.05):
    """Wald SPRT log-likelihood ratio; returns 'accept'|'reject'|None (continue)."""
    import math
    A = math.log((1 - beta) / alpha)
    B = math.log(beta / (1 - alpha))
    llr = 0.0
    for e in events:
        llr += math.log(p1 / p0) if e.success else math.log((1 - p1) / (1 - p0))
    if llr >= A:
        return "accept"
    if llr <= B:
        return "reject"
    return None


def arm_4() -> None:
    p = THETA + 0.03
    rng = random.Random(555)
    wilson_firsts, sprt_firsts = [], []
    for _ in range(1500):
        events: list[Event] = []
        w_at = s_at = None
        for i in range(400):
            events.append(Event(ts_days=float(i), success=(rng.random() < p),
                                incident_id=f"inc-{i}", incident_type=TYPES[i % len(TYPES)]))
            if w_at is None and evaluate_promotion(TRANSITION, events, now_days=float(i)).promote:
                w_at = i + 1
            if s_at is None and _sprt_decision(events, THETA - 0.05, THETA) == "accept":
                s_at = i + 1
            if w_at and s_at:
                break
        if w_at:
            wilson_firsts.append(w_at)
        if s_at:
            sprt_firsts.append(s_at)
    wm = statistics.median(wilson_firsts)
    sm = statistics.median(sprt_firsts)
    speedup = (wm - sm) / wm
    # This arm never fails the ADR — it only decides whether SPRT is worth showing.
    verdict = "adopt-as-display" if speedup >= 0.30 else "not-worth-adopting"
    record("arm 4 SPRT comparison (non-binding display)", True,
           f"Wilson median={wm:.0f}, SPRT median={sm:.0f}, speedup={speedup:.0%} → {verdict}")


def main() -> int:
    print(f"ADR-102 validation sim — {TRANSITION}, θ={THETA}, trials={TRIALS}\n")
    arm_1a()
    arm_1b()
    arm_1c()
    arm_2()
    arm_4()
    binding = [r for r in RESULTS if "SPRT" not in r[0]]  # arm 4 is non-binding
    passed = all(ok for _, ok, _ in binding)
    print(f"\n==== binding arms: {'ALL PASS' if passed else 'FAILURES'} "
          f"({sum(ok for _, ok, _ in binding)}/{len(binding)}) ; arm 3 (offline-vs-live) OUT OF SCOPE ====")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
