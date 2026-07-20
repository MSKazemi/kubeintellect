"""OTel-spans-on-real-Postgres validation (v5 spec 02, live).

Drives the actual flight recorder against a real Postgres:
  1. create the decision_log table (+ v5 projection columns),
  2. init_recorder() → emit a realistic span tree (hypothesis→evidence→mutation +
     a chat + a tool span) via the real record() path,
  3. close_recorder() to flush the drain queue,
  4. read the rows straight back from Postgres and assert:
       - verify_chain() is True over the persisted rows (tamper-evident),
       - the projection columns (trace_id/span_id/parent_span_id) are populated,
       - a single-byte tamper of a persisted payload BREAKS verify_chain,
       - build_provenance_chain() reconstructs the mutation's provenance as complete.

Run:  DATABASE_URL=postgresql://... uv run --project <v4> python scripts/otel_pg_probe.py
"""

from __future__ import annotations

import asyncio
import json
import os

import asyncpg

os.environ.setdefault("FLIGHT_RECORDER_ENABLED", "true")
os.environ.setdefault("CORTEX_V5_ENABLED", "true")
os.environ.setdefault("KI_V5_OTEL_SPANS_ENABLED", "true")

from app.db import flight_recorder as fr  # noqa: E402
from app.db import otel_spans  # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS decision_log (
    id BIGSERIAL PRIMARY KEY,
    episode_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload JSONB NOT NULL,
    prev_hash TEXT NOT NULL DEFAULT '',
    hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (episode_id, seq)
);
ALTER TABLE decision_log ADD COLUMN IF NOT EXISTS trace_id TEXT;
ALTER TABLE decision_log ADD COLUMN IF NOT EXISTS span_id TEXT;
ALTER TABLE decision_log ADD COLUMN IF NOT EXISTS parent_span_id TEXT;
"""

EP = "ep-otel-live"
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


async def main() -> int:
    dsn = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(dsn)
    await conn.execute("DROP TABLE IF EXISTS decision_log")
    await conn.execute(DDL)
    await conn.close()

    await fr.init_recorder()
    check("recorder connected to Postgres", fr._pool is not None)

    # A realistic investigation span tree.
    chat = otel_spans.chat_span_payload(EP, 0, system="anthropic", model="opus", input_tokens=1200, output_tokens=300)
    hyp = otel_spans.tool_span_payload(EP, 1, tool_name="inspect", ok=True)
    hyp["gen_ai.operation.name"] = otel_spans.OP_HYPOTHESIS
    ev = otel_spans.tool_span_payload(EP, 2, tool_name="logs", ok=True)
    ev["gen_ai.operation.name"] = otel_spans.OP_EVIDENCE
    mut = otel_spans.mutation_span_payload(
        EP, 3, action="restart-deployment",
        hypothesis_span_ids=[hyp["span_id"]], evidence_span_ids=[ev["span_id"]],
    )
    for p in (chat, hyp, ev, mut):
        otel_spans.emit(EP, p)

    await fr.close_recorder()  # flushes the queue

    # Read straight back from Postgres.
    conn = await asyncpg.connect(dsn)
    rows = await conn.fetch(
        "SELECT episode_id, seq, kind, payload, prev_hash, hash, trace_id, span_id, parent_span_id "
        "FROM decision_log WHERE episode_id=$1 ORDER BY seq", EP)
    dict_rows = [dict(r) for r in rows]

    check("all 4 spans persisted", len(dict_rows) == 4, f"{len(dict_rows)} rows")
    check("verify_chain() True over persisted rows", fr.verify_chain(dict_rows))

    span_rows = [r for r in dict_rows if r["kind"] == "ki_otel_span"]
    check("projection trace_id populated", all(r["trace_id"] for r in span_rows))
    check("projection span_id populated", all(r["span_id"] for r in span_rows))
    check("all spans share one trace_id", len({r["trace_id"] for r in span_rows}) == 1)

    # Tamper test: flip a byte in a persisted payload → chain must break.
    tampered = [dict(r) for r in dict_rows]
    p = json.loads(tampered[0]["payload"]) if isinstance(tampered[0]["payload"], str) else dict(tampered[0]["payload"])
    p["attributes"]["gen_ai.usage.input_tokens"] = 999999
    tampered[0]["payload"] = json.dumps(p)
    check("verify_chain() detects a tampered payload", fr.verify_chain(tampered) is False)

    # Provenance reconstruction from what's actually in the DB.
    payloads = [json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"] for r in dict_rows]
    prov = otel_spans.build_provenance_chain(payloads)
    check("provenance chain reconstructed", prov["total_mutations"] == 1)
    check("mutation provenance is complete", prov["chains"] and prov["chains"][0]["complete"])

    await conn.close()
    print(f"\n==== {'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)} ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
