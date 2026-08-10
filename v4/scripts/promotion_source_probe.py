"""Live promotion-loop probe (v5 P3, ADR-102) — record shadow outcomes → engine decides, on real PG.

Records per-action-class shadow-agreement outcomes to Postgres, then runs the statistical promotion
engine against the RECORDED data (not synthetic in-memory events): a strong record promotes, a
CUSUM failure burst demotes. Closes the promotion loop end-to-end on a real DB. Cleans up.

Run on n1:  DATABASE_URL=postgresql://... uv run python scripts/promotion_source_probe.py
"""
from __future__ import annotations

import asyncio
import os
import sys

import asyncpg
from app.autonomy.promotion_source import decide_from_store, record_outcome
from app.autonomy.promotion_stats import Event

FAIL: list[str] = []
AC = "probe-misconfig-fix"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


async def main() -> int:
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=3)
    await pool.execute(
        "CREATE TABLE IF NOT EXISTS promotion_outcomes (id BIGSERIAL PRIMARY KEY, action_class TEXT "
        "NOT NULL, ts_days DOUBLE PRECISION NOT NULL, success BOOLEAN NOT NULL, incident_id TEXT "
        "NOT NULL, incident_type TEXT NOT NULL DEFAULT 'generic', critical BOOLEAN NOT NULL DEFAULT false, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    await pool.execute("DELETE FROM promotion_outcomes WHERE action_class = $1", AC)

    # record a strong shadow record: 60 successes across 6 incidents over 10 days
    for i in range(60):
        await record_outcome(pool, AC, Event(ts_days=i / 59 * 10, success=True,
                                             incident_id=f"inc{i % 6}", incident_type="t0"))
    d = await decide_from_store(pool, AC, "L1->L2", "L1", now_days=10.0)
    check("strong RECORDED record ⇒ engine promotes L1->L2", d.action == "promote" and d.to_rung == "L2",
          f"lcb={d.lcb:.3f} n={d.n}")

    # now record a CUSUM failure burst (2 fails within 24h) ⇒ demotion at the current rung
    await record_outcome(pool, AC, Event(ts_days=10.0, success=False, incident_id="f1"))
    await record_outcome(pool, AC, Event(ts_days=10.5, success=False, incident_id="f2"))
    d2 = await decide_from_store(pool, AC, "L2->L3", "L3", now_days=11.0)
    check("recorded CUSUM failure burst ⇒ engine demotes", d2.action == "demote", d2.reasons and d2.reasons[0])

    await pool.execute("DELETE FROM promotion_outcomes WHERE action_class = $1", AC)
    await pool.close()
    print(f"\n==== {'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)} ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
