"""Signed archives of a hash chain — the half that must exist before anything may be pruned.

`memory/retention.py` refuses to touch `decision_log` and `memory_audit`, and the refusal is
correct: they are tamper-evidence, deleting their newest rows breaks no link, and a retention
pass that pruned them would make the install's own housekeeping indistinguishable from an
attack. Its written reason ends *"needs a signed export-then-truncate flow, not a `DELETE`"* —
and until 2026-08-28 no such flow existed, so the two fastest-growing tables in the schema were
the two nothing could ever bound. That is the open half of enterprise-readiness **A10**.

This module is the **export** half, and it is deliberately shipped on its own, because it is
useful before anything is ever deleted: today there is no way to take a *verifiable* copy of
either chain off the box at all. An archive here is self-checking — it carries the rows
verbatim, the anchor as it stood, the link verdict computed at export time, and a SHA-256 over
all of that — so :func:`verify_export` can check one years later with **no database present**.

Three design choices worth the words:

* **Segment verification is not `flight_recorder.verify_chain`.** That function starts at
  ``seq == 0`` with an empty ``prev_hash``, which is right for a whole chain and wrong for a
  slice of one. An archive of seqs 40–99 must verify against the hash of seq 39, which the
  archive records. Reusing the whole-chain verifier here would have reported every segment
  that does not start at the beginning as broken.
* **The archive is hashed, not signed with a key.** Nothing in this product manages a signing
  key, and inventing one here would add key distribution, rotation and revocation to a
  retention feature. A content hash is what makes an archive self-consistent and detects a
  later edit *of the archive*; it is not a claim about who produced it. :data:`ARCHIVE_LIMIT`
  below states that limit rather than letting "signed export" imply more than it does.
* **Nothing here deletes anything.** Exporting is safe and repeatable; truncating is neither.
  :func:`truncation_prerequisites` states exactly what a future truncation must record before
  a row may be removed, and the last item on that list is a change to the *verifier*, not to
  the data — see the note there.

Driver-agnostic in the same way as `db/backup.py`: every function takes a ``query(sql) -> rows``
callable, so the CLI drives it with psycopg and the tests drive it with a dict or a real pool.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

#: Bumped when the archive shape changes in a way an older `verify_export` cannot read.
ARCHIVE_VERSION = 1

#: What a content hash does and does not buy, stated where it cannot drift from the code.
ARCHIVE_LIMIT = (
    "A content hash proves this archive has not been edited since it was written. It does NOT "
    "prove who wrote it: anyone who can rewrite the archive can recompute the hash. Store the "
    "archive where the database's own operators cannot silently replace it — that, not this "
    "field, is what makes it evidence."
)

QueryFn = Callable[[str], list[Any]]


@dataclass(frozen=True)
class ChainSpec:
    """One hash-chained ledger: where the rows are, where the anchor is, what scopes them."""

    table: str
    anchor: str
    #: The column both the chain and its anchor key on — one chain per value of it.
    key: str
    why: str


CHAINS: dict[str, ChainSpec] = {
    "decision_log": ChainSpec(
        "decision_log", "decision_log_head", "episode_id",
        "The flight recorder (ADR-005): one chain per episode, rendered to a human by "
        "`kq postmortem`, which prints a tamper-evidence banner over it.",
    ),
    "memory_audit": ChainSpec(
        "memory_audit", "memory_chain_head", "cluster_id",
        "The memory write path (ADR-018 R8.2): one chain per cluster, verified on a schedule "
        "by the server and reported as `memory.chain` on /healthz.",
    ),
}


#: The columns each SELECT below asks for, in order. A positional driver (psycopg's default
#: cursor returns plain tuples) has no other way to name them — and guessing one shape for both
#: queries is how the anchor row silently lost its `hash`, caught the first time this ran
#: against a real database rather than a callable that answers by table name.
_ROW_COLUMNS = ("seq", "kind", "payload", "prev_hash", "hash")
_ANCHOR_COLUMNS = ("seq", "hash")


def _rows_as_dicts(rows: list[Any], columns: tuple[str, ...]) -> list[dict]:
    """Accept psycopg tuples, asyncpg Records and plain dicts alike."""
    out = []
    for row in rows:
        if isinstance(row, dict):
            out.append(dict(row))
        elif hasattr(row, "keys"):                       # asyncpg.Record, psycopg dict rows
            out.append({k: row[k] for k in row.keys()})  # noqa: SIM118 — Record has no items()
        else:
            if len(row) != len(columns):
                raise ValueError(
                    f"expected {len(columns)} column(s) {columns} from a positional driver, "
                    f"got {len(row)} — the query and the column list have drifted apart"
                )
            out.append(dict(zip(columns, row, strict=True)))
    return out


def _payload(value: Any) -> dict:
    """JSONB comes back as a str from asyncpg and as a dict from psycopg. Both must hash alike."""
    if isinstance(value, str):
        return json.loads(value)
    return dict(value or {})


def verify_segment(rows: list[dict], *, scope_id: str, start_prev_hash: str) -> bool:
    """Recompute the links of a chain *slice*. True iff every link verifies.

    Unlike `flight_recorder.verify_chain` this does not assume the slice starts at ``seq 0``:
    `start_prev_hash` is the hash the first row must chain from, and the seqs need only be
    contiguous. That is the whole difference between checking a chain and checking an archive.
    """
    from app.db.flight_recorder import compute_hash

    prev = start_prev_hash
    expected: int | None = None
    for row in rows:
        seq = int(row["seq"])
        if expected is not None and seq != expected:
            return False
        if row["prev_hash"] != prev:
            return False
        if compute_hash(prev, scope_id, seq, row["kind"], _payload(row["payload"])) != row["hash"]:
            return False
        prev = row["hash"]
        expected = seq + 1
    return True


def archive_hash(doc: dict[str, Any]) -> str:
    """SHA-256 over the archive's content, excluding the hash field itself.

    Canonical JSON (sorted keys, no whitespace slack) so the same archive hashes the same on
    any machine, any Python, any driver.
    """
    body = {k: v for k, v in doc.items() if k != "archive_hash"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def build_export(
    query: QueryFn, *, chain: str, scope_id: str, taken_at: str,
    through_seq: int | None = None, note: str = "",
) -> dict[str, Any]:
    """Read one chain (or a prefix of it) and return a self-verifying archive document.

    `through_seq` bounds the archive at that seq inclusive — the shape a future truncation
    needs, since only rows up to a chosen point are ever removed. ``None`` archives the whole
    chain, which is the useful default while nothing is being pruned.

    Read-only. Runs one SELECT for the rows and one for the anchor.
    """
    if chain not in CHAINS:
        raise ValueError(f"unknown chain {chain!r} — known: {', '.join(sorted(CHAINS))}")
    spec = CHAINS[chain]
    scope = scope_id.replace("'", "''")
    bound = "" if through_seq is None else f" AND seq <= {int(through_seq)}"
    rows = _rows_as_dicts(query(
        f"SELECT {', '.join(_ROW_COLUMNS)} FROM {spec.table} "
        f"WHERE {spec.key} = '{scope}'{bound} ORDER BY seq"
    ), _ROW_COLUMNS)
    anchor_rows = _rows_as_dicts(query(
        f"SELECT {', '.join(_ANCHOR_COLUMNS)} FROM {spec.anchor} WHERE {spec.key} = '{scope}'"
    ), _ANCHOR_COLUMNS)

    normalised = [
        {"seq": int(r["seq"]), "kind": r["kind"], "payload": _payload(r["payload"]),
         "prev_hash": r["prev_hash"], "hash": r["hash"]}
        for r in rows
    ]
    start_prev = normalised[0]["prev_hash"] if normalised else ""
    doc: dict[str, Any] = {
        "archive_version": ARCHIVE_VERSION,
        "chain": chain,
        "scope_key": spec.key,
        "scope_id": scope_id,
        "taken_at": taken_at,
        "note": note,
        "from_seq": normalised[0]["seq"] if normalised else None,
        "through_seq": normalised[-1]["seq"] if normalised else None,
        "row_count": len(normalised),
        # The bound the CALLER asked for, kept separate from the seq actually reached. Without
        # it a deliberately partial archive and a truncated source chain look identical: both
        # end before the anchor does, and only one of them is a finding.
        "bounded_at": through_seq,
        # The hash the first archived row chains from. Without it a segment cannot be verified
        # at all, and an archive that cannot be verified is a copy, not evidence.
        "start_prev_hash": start_prev,
        "end_hash": normalised[-1]["hash"] if normalised else "",
        # The anchor AS IT STOOD. Recorded, never compared here: an archive taken from an
        # already-damaged chain must carry the damage rather than silently define it as correct.
        "anchor": (
            {"seq": int(anchor_rows[0]["seq"]), "hash": anchor_rows[0]["hash"]}
            if anchor_rows else None
        ),
        # The link verdict computed while the database was in front of us. `verify_export`
        # recomputes it from the rows and disagreeing is itself a finding.
        "links_verified_at_export": verify_segment(
            normalised, scope_id=scope_id, start_prev_hash=start_prev),
        "rows": normalised,
        "limit": ARCHIVE_LIMIT,
    }
    doc["archive_hash"] = archive_hash(doc)
    return doc


def verify_export(doc: dict[str, Any]) -> dict[str, Any]:
    """Check an archive with no database present. Reports every problem, not the first.

    Returns ``{"ok", "problems", "checked"}``. Three failures are three different sentences
    because they need three different responses: an archive that was edited after it was
    written, an archive whose rows do not chain, and an archive this build is too old to read.
    """
    problems: list[str] = []
    checked = 0

    version = int(doc.get("archive_version", 0))
    if version > ARCHIVE_VERSION:
        return {
            "ok": False, "checked": 0,
            "problems": [
                f"archive is version {version} but this build reads v{ARCHIVE_VERSION} — "
                f"verify it with the build that wrote it"
            ],
        }

    checked += 1
    stored = doc.get("archive_hash")
    if not stored:
        problems.append("archive carries no archive_hash — it cannot be checked at all")
    elif stored != archive_hash(doc):
        problems.append(
            "archive_hash does not match the archive's content — this file was edited after it "
            "was written. Nothing below can be trusted from this copy."
        )

    rows = doc.get("rows") or []
    checked += 1
    links_ok = verify_segment(
        rows, scope_id=str(doc.get("scope_id", "")),
        start_prev_hash=str(doc.get("start_prev_hash", "")),
    )
    if not links_ok:
        problems.append(
            f"the {len(rows)} archived row(s) do not chain — recomputing their hashes from "
            f"start_prev_hash disagrees with what they carry"
        )

    checked += 1
    if doc.get("links_verified_at_export") is False and links_ok:
        problems.append(
            "the archive records that its links did NOT verify when it was taken, yet they "
            "verify now — the rows were repaired after export, which is a rewrite of evidence"
        )
    elif doc.get("links_verified_at_export") is True and not links_ok:
        problems.append(
            "the archive claims its links verified at export and they do not now — either the "
            "rows were altered in place, or the archive was assembled from a different chain"
        )

    checked += 1
    anchor = doc.get("anchor")
    if anchor is None:
        problems.append(
            "no anchor was recorded at export — this archive proves its rows chain, but not "
            "that they are all of them (that is the truncation the anchor exists to catch)"
        )
    elif rows:
        anchor_seq = int(anchor.get("seq", -1))
        through = int(doc.get("through_seq") or 0)
        if anchor_seq < through:
            problems.append(
                f"the recorded anchor stops at seq {anchor_seq} while the archive holds rows "
                f"through {through} — the source chain was already ahead of its own head when "
                f"this was taken (an append that crashed between the row and the anchor leaves "
                f"exactly this, and so does a forged row)"
            )
        elif anchor_seq > through and doc.get("bounded_at") is None:
            # Nobody asked for a partial archive, and the chain still ends before its anchor.
            # Every surviving link verifies — this is the truncation the links cannot see.
            problems.append(
                f"the source chain ended at seq {through} but its anchor records seq "
                f"{anchor_seq} — {anchor_seq - through} entr(y/ies) were missing from the chain "
                f"when this archive was taken. The archived rows chain perfectly; that is what "
                f"a truncation looks like from the rows alone"
            )
    return {"ok": not problems, "problems": problems, "checked": checked}


#: What a truncation must record before a single row may be deleted. Written down here rather
#: than in a plan because the list is the reason no `DELETE` ships in this module yet.
TRUNCATION_PREREQUISITES: tuple[str, ...] = (
    "A verified archive covering exactly the rows to be removed — `verify_export(doc)['ok']` "
    "true, and its `through_seq` equal to the highest seq being deleted.",
    "The archive stored somewhere the database's own operators cannot silently replace, per "
    "ARCHIVE_LIMIT. An archive kept beside the rows it justifies deleting proves nothing.",
    "A durable truncation record in the database — chain, scope, through_seq, the archive's "
    "hash, and the surviving chain's new first prev_hash — so the gap is DECLARED rather than "
    "discovered. An undeclared gap is exactly what tampering looks like.",
    "A verifier that reads that record. `flight_recorder.verify_chain` starts at seq 0 with an "
    "empty prev_hash, so a legitimately truncated chain fails it today — measured, not "
    "assumed. Shipping the DELETE before this change would turn every pruned install's own "
    "housekeeping into a permanent tamper alarm, which is the exact failure `retention.REFUSED` "
    "was written to avoid.",
)


class TruncationRefused(Exception):
    """The truncation did not happen, and the reason is the message.

    Every refusal is a *pre*-condition: nothing has been written or deleted when this is
    raised. That is the whole design — the DELETE is the last statement, after the record that
    makes the resulting gap legible.
    """


def _sql_str(value: str) -> str:
    """A single-quoted SQL literal. Same escaping as `build_export`'s scope, same reason."""
    return "'" + str(value).replace("'", "''") + "'"


def truncate_chain(
    query: QueryFn, execute: Callable[[str], Any], *, doc: dict[str, Any], note: str = "",
) -> dict[str, Any]:
    """Delete the rows an archive holds, after recording why the resulting gap is legitimate.

    The order is the point. A truncation record is written **first**, inside the caller's
    transaction, and the DELETE runs only after it. If the process dies between them, the
    database holds a declared gap that does not exist yet — which verifies fine, because the
    rows are still there. The opposite order would leave an undeclared gap, i.e. a permanent
    tamper alarm on the operator's own housekeeping.

    Refuses unless all of these hold, each checked against the live database rather than
    against the archive's own claims about it:

    * the archive verifies on its own terms (`verify_export`), and covers at least one row;
    * the row at `through_seq` is still present and still carries the archive's `end_hash` —
      an archive of a chain that has since changed does not describe what would be deleted;
    * rows survive past `through_seq`. Deleting a chain entirely is not truncation, it is
      deletion, and the head anchor would report it as such forever;
    * the surviving chain links to the archive: its first row's `prev_hash` is the archive's
      `end_hash`. This is what lets a verifier resume, and it is the reason a forged record
      cannot launder an edit — it has to be consistent with rows it does not control.

    `query`/`execute` are the caller's connection. This function does not commit; the caller
    owns the transaction, so a failure after the record and before the delete rolls back both.
    """
    problems = verify_export(doc)["problems"]
    if problems:
        raise TruncationRefused(
            "the archive does not verify, so it cannot justify deleting anything: "
            + "; ".join(problems))
    chain = str(doc.get("chain") or "")
    if chain not in CHAINS:
        raise TruncationRefused(f"unknown chain {chain!r} — known: {', '.join(sorted(CHAINS))}")
    spec = CHAINS[chain]
    rows = doc.get("rows") or []
    if not rows:
        raise TruncationRefused("the archive holds no rows — there is nothing to remove")
    through = int(doc["through_seq"])
    scope_id = str(doc["scope_id"])
    scope = _sql_str(scope_id)

    live = _rows_as_dicts(query(
        f"SELECT seq, hash FROM {spec.table} WHERE {spec.key} = {scope} AND seq = {through}"
    ), _ANCHOR_COLUMNS)
    if not live:
        raise TruncationRefused(
            f"the chain has no row at seq={through}, which the archive says it ends at — "
            f"this archive does not describe the chain as it stands now")
    if str(live[0]["hash"]) != str(doc["end_hash"]):
        raise TruncationRefused(
            f"the row at seq={through} carries a different hash than the archive recorded — "
            f"the chain changed after the archive was taken, so the archive is not a copy of "
            f"what would be deleted")

    survivors = _rows_as_dicts(query(
        f"SELECT seq, prev_hash AS hash FROM {spec.table} WHERE {spec.key} = {scope} "
        f"AND seq > {through} ORDER BY seq LIMIT 1"
    ), _ANCHOR_COLUMNS)
    if not survivors:
        raise TruncationRefused(
            f"nothing survives past seq={through} — removing every row is not truncation, and "
            f"the head anchor would report the chain as entirely removed, correctly")
    resume_seq = int(survivors[0]["seq"])
    resume_prev = str(survivors[0]["hash"])
    if resume_prev != str(doc["end_hash"]):
        raise TruncationRefused(
            f"the surviving chain does not link to this archive: the row at seq={resume_seq} "
            f"chains from a different hash than the archive's last row. Either the archive is "
            f"of a different chain, or the chain is already broken at that seam — verify it "
            f"before removing anything")

    execute(
        f"INSERT INTO chain_truncation (chain, scope_id, through_seq, resume_seq, "
        f"resume_prev_hash, archive_hash, note) VALUES ({_sql_str(chain)}, {scope}, "
        f"{through}, {resume_seq}, {_sql_str(resume_prev)}, "
        f"{_sql_str(str(doc['archive_hash']))}, {_sql_str(note)})"
    )
    execute(
        f"DELETE FROM {spec.table} WHERE {spec.key} = {scope} AND seq <= {through}"
    )
    return {
        "chain": chain, "scope_id": scope_id, "through_seq": through,
        "resume_seq": resume_seq, "resume_prev_hash": resume_prev,
        "archive_hash": str(doc["archive_hash"]), "rows_removed": len(rows),
    }


def truncation_prerequisites() -> tuple[str, ...]:
    """The checklist above, as a function so a CLI or a doc test can render it."""
    return TRUNCATION_PREREQUISITES
