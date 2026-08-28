"""Declared gaps — the record that separates housekeeping from tampering.

A hash chain has exactly one way to say "rows are missing", and it is the same way whether an
attacker removed them or the operator did: the chain is short, and the anchor says so. That
symmetry is why `memory/retention.py` refuses to prune `decision_log` and `memory_audit` at all,
and why `db/chain_export.py` ships an export flow that stops short of deleting anything.

This module is the missing half of the sentence. A row in `chain_truncation` says: *entries up
to `through_seq` were removed deliberately, the surviving chain resumes at `resume_seq` and must
chain from `resume_prev_hash`, and the removed rows are preserved in an archive whose content
hash is `archive_hash`.* The verifiers consult it before reporting a short chain, and **only**
that: with no matching row a short chain is still TAMPERED, which is the behaviour that must not
change.

Two properties this deliberately does not have.

* **It is not prevention.** An attacker with full database write can insert a row here as easily
  as they can delete chain rows — the same limit the head anchors have. What it removes is the
  *free* gap: a truncation now has to be declared in a second place, with a hash of an archive
  that has to exist somewhere the database does not control.
* **It cannot make a gap disappear.** `resume_prev_hash` is checked against the first surviving
  row, so a record that claims a truncation which does not match the surviving chain is itself
  a contradiction, and is reported. A forged record has to be *consistent* with the rows to be
  useful, which means it cannot launder an edit — only a deletion the operator could have made
  legitimately anyway.
"""
from __future__ import annotations

from typing import NamedTuple

from app.utils.logger import get_logger

logger = get_logger(__name__)

#: The chain names this table keys on — the same two `chain_export.CHAINS` knows.
_SQL_LATEST = (
    "SELECT through_seq, resume_seq, resume_prev_hash, archive_hash FROM chain_truncation "
    "WHERE chain = $1 AND scope_id = $2 ORDER BY through_seq DESC LIMIT 1"
)


class DeclaredStart(NamedTuple):
    """Where a verifier should begin, given what has been declared removed.

    ``found`` is False when nothing was declared — the ordinary case, and the one where
    ``seq``/``prev_hash`` are the whole-chain defaults. ``read`` is False when the lookup could
    not be performed at all, which is not the same answer and must never be treated as
    "nothing was declared": that would report every legitimately pruned chain as tampered the
    moment its own database hiccuped.
    """

    seq: int = 0
    prev_hash: str = ""
    found: bool = False
    read: bool = True
    archive_hash: str = ""


async def declared_start(pool, *, chain: str, scope_id: str) -> DeclaredStart:
    """The seq and prev_hash a verifier should start from for this chain. Never raises."""
    if pool is None:
        return DeclaredStart(read=False)
    try:
        row = await pool.fetchrow(_SQL_LATEST, chain, scope_id)
    except Exception as exc:
        # A missing table is the normal state of an install that has never truncated anything
        # and has not re-run `db-init`. Log once at debug level rather than warning on every
        # verification: the verdict this produces is `read=False`, which callers handle.
        logger.debug(f"chain_truncation: lookup failed for {chain}/{scope_id!r}: {exc}")
        return DeclaredStart(read=False)
    if row is None:
        return DeclaredStart(found=False, read=True)
    try:
        return DeclaredStart(
            seq=int(row["resume_seq"]), prev_hash=str(row["resume_prev_hash"]),
            found=True, read=True, archive_hash=str(row["archive_hash"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(
            f"chain_truncation: a record exists for {chain}/{scope_id!r} but cannot be read "
            f"({exc!r}) — this chain's declared gap is NOT being honoured, so a legitimate "
            f"truncation will read as tampering until the record is repaired"
        )
        return DeclaredStart(read=False)
