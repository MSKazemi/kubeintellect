"""Memory V5 real-Postgres SQL validation (v5 P1 + P7, live).

The memory unit tests use a FakePool + sqlglot — they validate SQL *shape* but never EXECUTE it.
This probe runs the real memory SQL against the real schema on real Postgres, with the V5 flags
ON, to catch anything a fake pool can't: column/type mismatches, the FTS/RRF hybrid recall query,
and the P7 audit hash chain end-to-end.

  P1 (MEMORY_HYBRID_RETRIEVAL): write episodes → recall_episodes returns the relevant one via the
     pg_trgm+ts_rank RRF query executing on the real idx_episodes_fts index.
  P7 (MEMORY_SECURITY_HARDENING): record_memory_audit builds a hash chain that verify_memory_chain
     confirms intact, and a direct DB tamper breaks.

Run: DATABASE_URL=... uv run --project <v4> python scripts/memory_pg_probe.py
"""

from __future__ import annotations

import asyncio
import os

import asyncpg

os.environ.setdefault("MEMORY_HYBRID_RETRIEVAL", "true")
os.environ.setdefault("MEMORY_SECURITY_HARDENING", "true")
os.environ.setdefault("MEMORY_IMPORTANCE", "true")

from app.memory import episodes, security  # noqa: E402

CLUSTER = "cl-live"
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


async def main() -> int:
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=3)
    episodes.init_episodes(pool)

    # Idempotency: the P7 tamper test below mutates memory_audit and the hash chain
    # is per-cluster and append-only, so a prior run's rows (incl. the tampered seq)
    # would poison verify_memory_chain on re-run. Reset only THIS probe's test cluster.
    await pool.execute("DELETE FROM memory_audit WHERE cluster_id=$1", CLUSTER)
    await pool.execute("DELETE FROM episodes WHERE cluster_id=$1", CLUSTER)

    # ── P1: hybrid recall on the real FTS index ──────────────────────────────
    id1 = await episodes.write_episode(
        cluster_id=CLUSTER, trigger_kind="detector",
        trigger_detail="pod crasher CrashLoopBackOff in demo",
        summary="OOMKilled: container memory limit too low; raised limits, pod recovered",
        namespace="demo", root_cause="memory-limit-too-low", outcome="resolved", verified=True)
    id2 = await episodes.write_episode(
        cluster_id=CLUSTER, trigger_kind="detector",
        trigger_detail="ingress 502 on checkout",
        summary="Bad upstream readiness probe caused 502s; fixed probe path",
        namespace="shop", root_cause="readiness-probe-misconfig", outcome="resolved", verified=True)
    check("write_episode returns ids on real PG", bool(id1) and bool(id2), f"{id1}, {id2}")

    hits = await episodes.recall_episodes("crashloop out of memory OOM", CLUSTER, k=3)
    check("hybrid recall_episodes executes on real PG", isinstance(hits, list) and len(hits) >= 1,
          f"{len(hits)} hits")
    top_summary = (hits[0].get("summary", "") if hits else "")
    check("hybrid recall ranks the OOM episode first", "OOMKilled" in top_summary, top_summary[:60])

    # ── P7: memory audit hash chain on real PG ───────────────────────────────
    for i in range(4):
        await security.record_memory_audit(pool, cluster_id=CLUSTER, kind="episode_write",
                                            ref_id=f"e{i}", payload={"i": i})
    ok = await security.verify_memory_chain(pool, CLUSTER)
    check("verify_memory_chain True over real audit rows", ok)

    # Direct DB tamper → chain must break.
    await pool.execute(
        "UPDATE memory_audit SET payload = '{\"i\": 999}'::jsonb "
        "WHERE cluster_id=$1 AND seq=1", CLUSTER)
    security._audit_chains.pop(CLUSTER, None)  # drop cache so verify re-reads DB
    broke = await security.verify_memory_chain(pool, CLUSTER)
    check("verify_memory_chain detects a direct DB tamper", broke is False)

    await pool.close()
    print(f"\n==== {'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)} ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
