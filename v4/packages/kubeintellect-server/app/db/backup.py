"""Backup manifests — proof that a restore actually restored (enterprise A12).

`docs/operations.md` already gives the right `pg_dump` / `psql` commands and the right warning
(`ON_ERROR_STOP=1`, `--single-transaction`, because psql's default is to print an error, continue,
and exit 0). What it could not give an operator was an answer to the question that matters after
the restore has run: **did everything come back?**

For most tables a wrong answer is merely lost data. For two of them it is worse. `decision_log`
and `memory_audit` are hash chains, and a restore that silently drops the *newest* rows of a chain
breaks no link — the remaining rows verify perfectly. `verify_chain` returns True, the postmortem
prints its intact-chain banner, and the record is quietly short. The anchors `decision_log_head`
and `memory_chain_head` exist precisely to make that visible, and they only work if something
compares them. Nothing did.

So a manifest, taken with the dump, records what the database held: the schema version and
fingerprint, exact row counts for the tables whose loss is a data-loss event, and — the part no
row count can replace — how far each hash chain got. :func:`verify` re-measures a restored
database against it and reports every discrepancy rather than the first.

Deliberately dependency-free and driver-agnostic: both functions take a ``query(sql) -> rows``
callable, so the CLI can drive them with psycopg while the tests drive them with a dict. Nothing
here writes to the database or shells out to `pg_dump`; taking the dump stays the operator's
standard PostgreSQL tooling, which is the right place for it.

⚠️ **Why A12 is not green, dated 2026-08-28.** This is the *verification* half. There is still no
scheduled backup in the Helm chart, no off-site copy, and no automated restore rehearsal — so the
RPO and RTO in `docs/operations.md` are the ones an operator's own schedule produces, not ones
this product enforces. A backup nobody takes is not improved by a manifest nobody writes.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

#: Bumped if the manifest shape changes in a way an older `verify` cannot read.
MANIFEST_VERSION = 1

QueryFn = Callable[[str], list[Any]]

#: Tables whose loss is a data-loss event, counted exactly. Deliberately not every table in the
#: schema: a mismatch has to mean something, and `schema_migrations` (one row per version) or a
#: projection that regenerates itself would only add noise.
COUNTED_TABLES: tuple[str, ...] = (
    "episodes", "decision_log", "memory_audit", "kg_entities", "kg_edges",
    "semantic_rules", "detectors", "runbooks", "failure_patterns", "rca_outcomes",
    "prospective_memory", "promotion_outcomes", "user_prefs",
)

#: ``(chain table, anchor table, the column both key on)``. The anchor records how far the chain
#: got; comparing the two is the only way to see a truncated tail, because a shorter chain is
#: still a valid chain.
CHAINS: tuple[tuple[str, str, str], ...] = (
    ("decision_log", "decision_log_head", "episode_id"),
    ("memory_audit", "memory_chain_head", "cluster_id"),
)


def _scalar(query: QueryFn, sql: str) -> int:
    rows = query(sql)
    if not rows:
        return 0
    row = rows[0]
    value = row[0] if isinstance(row, (list, tuple)) else list(row.values())[0]
    return int(value or 0)


def _chain_sql(chain: str, anchor: str, key: str) -> tuple[str, str]:
    """``(anchor row count, count of anchors the chain does not reach)``."""
    return (
        f"SELECT count(*) FROM {anchor}",
        f"SELECT count(*) FROM {anchor} h LEFT JOIN "
        f"(SELECT {key} AS k, max(seq) AS s FROM {chain} GROUP BY {key}) c "
        f"ON c.k = h.{key} WHERE c.s IS NULL OR c.s <> h.seq",
    )


def build_manifest(query: QueryFn, *, taken_at: str, note: str = "") -> dict[str, Any]:
    """Measure a live database. Run this against the source, beside the `pg_dump`."""
    from app.db.schema_version import SCHEMA_VERSION, schema_fingerprint

    counts = {t: _scalar(query, f"SELECT count(*) FROM {t}") for t in COUNTED_TABLES}
    chains = {}
    for chain, anchor, key in CHAINS:
        anchor_sql, broken_sql = _chain_sql(chain, anchor, key)
        chains[chain] = {
            "anchors": _scalar(query, anchor_sql),
            # Recorded so a manifest taken from an ALREADY-damaged source is not silently used as
            # the definition of "correct" — verify reports it rather than comparing against it.
            "unreached_at_backup": _scalar(query, broken_sql),
        }
    return {
        "manifest_version": MANIFEST_VERSION,
        "taken_at": taken_at,
        "note": note,
        "schema_version": SCHEMA_VERSION,
        "schema_fingerprint": schema_fingerprint(),
        "row_counts": counts,
        "chains": chains,
    }


def verify(query: QueryFn, manifest: dict[str, Any]) -> dict[str, Any]:
    """Re-measure a restored database against a manifest. Reports every problem, not the first.

    Returns ``{"ok": bool, "problems": [...], "checked": int}``. A missing table, a short table
    and a truncated hash chain are three different sentences, because they need three different
    responses from whoever is reading them mid-incident.
    """
    from app.db.schema_version import SCHEMA_VERSION, schema_fingerprint

    problems: list[str] = []
    checked = 0

    if int(manifest.get("manifest_version", 0)) > MANIFEST_VERSION:
        problems.append(
            f"manifest is version {manifest.get('manifest_version')} but this build reads "
            f"v{MANIFEST_VERSION} — verify with the build that took it"
        )
        return {"ok": False, "problems": problems, "checked": 0}

    checked += 1
    if manifest.get("schema_version") != SCHEMA_VERSION:
        problems.append(
            f"schema version differs: backup v{manifest.get('schema_version')}, this build "
            f"v{SCHEMA_VERSION}. Row counts below are still meaningful; a shape difference is not "
            f"data loss, but the restore target is not the shape the dump came from."
        )
    elif manifest.get("schema_fingerprint") != schema_fingerprint():
        problems.append(
            "schema fingerprint differs at the same version — the DDL changed without a version "
            "bump somewhere between taking this backup and restoring it"
        )

    for table, expected in (manifest.get("row_counts") or {}).items():
        checked += 1
        try:
            actual = _scalar(query, f"SELECT count(*) FROM {table}")
        except Exception as exc:
            problems.append(f"{table}: cannot be read ({exc}) — the restore did not create it")
            continue
        if actual != expected:
            direction = "MISSING" if actual < expected else "extra"
            problems.append(
                f"{table}: {actual} rows, manifest says {expected} "
                f"({abs(actual - expected)} {direction})"
            )

    for chain, anchor, key in CHAINS:
        recorded = (manifest.get("chains") or {}).get(chain)
        if recorded is None:
            continue
        anchor_sql, broken_sql = _chain_sql(chain, anchor, key)
        checked += 2
        try:
            anchors = _scalar(query, anchor_sql)
            unreached = _scalar(query, broken_sql)
        except Exception as exc:
            problems.append(f"{chain}: chain check could not run ({exc})")
            continue
        if anchors != recorded.get("anchors"):
            problems.append(
                f"{anchor}: {anchors} anchor rows, manifest says {recorded.get('anchors')}"
            )
        if unreached > recorded.get("unreached_at_backup", 0):
            problems.append(
                f"{chain}: {unreached} chain(s) do not reach their recorded head — a truncated "
                f"tail. This is the failure `verify_chain` CANNOT see: the surviving rows still "
                f"hash correctly, so the record verifies while being short."
            )

    return {"ok": not problems, "problems": problems, "checked": checked}
