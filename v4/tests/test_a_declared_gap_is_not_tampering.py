"""Pruning a hash chain used to be impossible on purpose. This is the narrow door.

`retention.REFUSED` still refuses to prune `decision_log` and `memory_audit` on the plain
schedule, and that is correct: a chain with rows missing is indistinguishable from a chain
someone edited, so an automatic pruner would convert routine housekeeping into a permanent
tamper alarm. Pass 284 shipped the archive half — an export that verifies with no database in
front of it. This is the other half: the record that makes a gap *declared*, and the verifier
change that reads it.

The property being defended is easy to lose in the direction of convenience, so it is asserted
from both ends:

* a chain that still starts at seq 0 takes the old path exactly — no lookup, no new failure
  mode, and no way for a missing `chain_truncation` table to soften an existing verdict;
* a short chain with **no** record is still TAMPERED, which is the behaviour that must not
  change;
* a record that does not match the surviving rows is TAMPERED too — worse than none, because
  something claims the gap is accounted for and the rows disagree;
* a record that could not be READ is `unverified`, never `intact` and never TAMPERED.

The refusals in `truncate_chain` are checked against a live database in
`test_a_truncated_chain_still_verifies_against_a_real_database.py`; what is proved here is
that each one refuses *before* anything is written, which is why the archive can be trusted
to be the only copy.
"""
from __future__ import annotations

import pytest

from app.db import chain_export, chain_truncation
from app.db.flight_recorder import compute_hash, verify_chain

EP = "ep-truncate"


def chain(n: int, *, scope: str = EP, start: int = 0, prev: str = "") -> list[dict]:
    rows = []
    for seq in range(start, start + n):
        payload = {"step": seq}
        digest = compute_hash(prev, scope, seq, "tool_call", payload)
        rows.append({"episode_id": scope, "seq": seq, "kind": "tool_call",
                     "payload": payload, "prev_hash": prev, "hash": digest})
        prev = digest
    return rows


class TestTheOldBehaviourIsUntouched:
    def test_a_whole_chain_verifies_as_before(self):
        assert verify_chain(chain(5)) is True

    def test_a_chain_missing_its_front_still_fails_by_default(self):
        """The defaults ARE the invariant: no argument, no forgiveness."""
        assert verify_chain(chain(5)[2:]) is False

    def test_the_defaults_are_the_origin(self):
        import inspect
        sig = inspect.signature(verify_chain)
        assert sig.parameters["start_seq"].default == 0
        assert sig.parameters["start_prev_hash"].default == ""

    def test_a_declared_start_verifies_the_remainder(self):
        rows = chain(5)
        tail = rows[2:]
        assert verify_chain(tail, start_seq=2, start_prev_hash=rows[1]["hash"]) is True

    def test_a_wrong_declared_start_does_not(self):
        rows = chain(5)
        assert verify_chain(rows[2:], start_seq=2, start_prev_hash="not-the-hash") is False
        assert verify_chain(rows[2:], start_seq=3, start_prev_hash=rows[1]["hash"]) is False


class FakePool:
    """Answers `fetchrow` with whatever the test wants — including by failing."""

    def __init__(self, row=None, exc: Exception | None = None):
        self.row, self.exc, self.calls = row, exc, []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if self.exc:
            raise self.exc
        return self.row


class TestReadingTheRecord:
    @pytest.mark.asyncio
    async def test_no_record_is_not_the_same_as_no_answer(self):
        found = await chain_truncation.declared_start(
            FakePool(row=None), chain="decision_log", scope_id=EP)
        assert (found.found, found.read) == (False, True)

    @pytest.mark.asyncio
    async def test_a_missing_table_reports_that_it_could_not_look(self):
        """The ordinary state of an install that has never re-run `db-init`."""
        got = await chain_truncation.declared_start(
            FakePool(exc=RuntimeError('relation "chain_truncation" does not exist')),
            chain="decision_log", scope_id=EP)
        assert got.read is False and got.found is False

    @pytest.mark.asyncio
    async def test_no_pool_is_not_an_answer_either(self):
        assert (await chain_truncation.declared_start(
            None, chain="decision_log", scope_id=EP)).read is False

    @pytest.mark.asyncio
    async def test_an_unreadable_record_is_loud(self, caplog):
        with caplog.at_level("WARNING"):
            got = await chain_truncation.declared_start(
                FakePool(row={"resume_seq": "not-a-number", "resume_prev_hash": "x",
                              "archive_hash": "y", "through_seq": 1}),
                chain="decision_log", scope_id=EP)
        assert got.read is False
        assert "NOT being honoured" in caplog.text

    @pytest.mark.asyncio
    async def test_it_reads_the_newest_declaration(self):
        pool = FakePool(row={"through_seq": 9, "resume_seq": 10,
                             "resume_prev_hash": "h9", "archive_hash": "a"})
        got = await chain_truncation.declared_start(
            pool, chain="memory_audit", scope_id="c1")
        assert (got.seq, got.prev_hash, got.found) == (10, "h9", True)
        sql, args = pool.calls[0]
        assert "ORDER BY through_seq DESC" in sql and "LIMIT 1" in sql
        assert args == ("memory_audit", "c1")


class FakeDb:
    """A query/execute pair over dict rows, recording every statement in order."""

    def __init__(self, rows: dict[str, list[dict]]):
        self.rows, self.executed = rows, []

    def query(self, sql: str):
        for key, value in self.rows.items():
            if key in sql:
                return value
        return []

    def execute(self, sql: str) -> None:
        self.executed.append(sql)


def archive(rows: list[dict], *, through: int | None = None) -> dict:
    kept = [r for r in rows if through is None or r["seq"] <= through]
    doc = {
        "archive_version": chain_export.ARCHIVE_VERSION, "chain": "decision_log",
        "scope_key": "episode_id", "scope_id": EP, "taken_at": "2026-08-28T00:00:00+00:00",
        "note": "", "from_seq": kept[0]["seq"] if kept else None,
        "through_seq": kept[-1]["seq"] if kept else None, "row_count": len(kept),
        "bounded_at": through,
        "start_prev_hash": kept[0]["prev_hash"] if kept else "",
        "end_hash": kept[-1]["hash"] if kept else "",
        "anchor": {"seq": rows[-1]["seq"], "hash": rows[-1]["hash"]},
        "links_verified_at_export": True,
        "rows": [{k: r[k] for k in ("seq", "kind", "payload", "prev_hash", "hash")}
                 for r in kept],
        "limit": chain_export.ARCHIVE_LIMIT,
    }
    doc["archive_hash"] = chain_export.archive_hash(doc)
    return doc


class TestItRefusesBeforeItWrites:
    def _db(self, rows, through):
        return FakeDb({
            f"FROM decision_log WHERE episode_id = '{EP}' AND seq = {through}":
                [{"seq": through, "hash": rows[through]["hash"]}],
            f"AND seq > {through}":
                [{"seq": through + 1, "hash": rows[through + 1]["prev_hash"]}],
        })

    def test_the_happy_path_records_the_gap_before_deleting_it(self):
        rows = chain(6)
        db = self._db(rows, 2)
        out = chain_export.truncate_chain(db.query, db.execute, doc=archive(rows, through=2),
                                          note="90-day retention")
        assert out["rows_removed"] == 3 and out["resume_seq"] == 3
        assert len(db.executed) == 2
        assert db.executed[0].startswith("INSERT INTO chain_truncation")
        assert db.executed[1].startswith("DELETE FROM decision_log")
        assert "'90-day retention'" in db.executed[0]
        assert f"'{rows[2]['hash']}'" in db.executed[0], "the seam hash must be recorded"

    def test_an_archive_that_does_not_verify_is_refused(self):
        rows = chain(6)
        doc = archive(rows, through=2)
        doc["rows"][1]["payload"] = {"step": "edited"}
        db = self._db(rows, 2)
        with pytest.raises(chain_export.TruncationRefused, match="does not verify"):
            chain_export.truncate_chain(db.query, db.execute, doc=doc)
        assert db.executed == []

    def test_an_empty_archive_is_refused(self):
        doc = archive(chain(6), through=2)
        doc.update(rows=[], row_count=0, through_seq=None, from_seq=None, end_hash="")
        doc["archive_hash"] = chain_export.archive_hash(doc)
        db = FakeDb({})
        with pytest.raises(chain_export.TruncationRefused, match="nothing to remove"):
            chain_export.truncate_chain(db.query, db.execute, doc=doc)
        assert db.executed == []

    def test_a_chain_that_changed_since_the_archive_is_refused(self):
        """An archive is evidence only of the chain it was taken from."""
        rows = chain(6)
        db = FakeDb({
            "AND seq = 2": [{"seq": 2, "hash": "a-different-hash"}],
            "AND seq > 2": [{"seq": 3, "hash": rows[3]["prev_hash"]}],
        })
        with pytest.raises(chain_export.TruncationRefused, match="different hash"):
            chain_export.truncate_chain(db.query, db.execute, doc=archive(rows, through=2))
        assert db.executed == []

    def test_a_missing_row_at_the_boundary_is_refused(self):
        rows = chain(6)
        db = FakeDb({"AND seq > 2": [{"seq": 3, "hash": rows[3]["prev_hash"]}]})
        with pytest.raises(chain_export.TruncationRefused, match="no row at seq=2"):
            chain_export.truncate_chain(db.query, db.execute, doc=archive(rows, through=2))
        assert db.executed == []

    def test_removing_the_whole_chain_is_refused(self):
        """Not truncation — deletion. The head anchor would report it forever, correctly."""
        rows = chain(6)
        db = FakeDb({"AND seq = 5": [{"seq": 5, "hash": rows[5]["hash"]}]})
        with pytest.raises(chain_export.TruncationRefused, match="nothing survives"):
            chain_export.truncate_chain(db.query, db.execute, doc=archive(rows))
        assert db.executed == []

    def test_a_survivor_that_does_not_link_to_the_archive_is_refused(self):
        """The seam is the whole guarantee; without it the record would be an assertion."""
        rows = chain(6)
        db = FakeDb({
            "AND seq = 2": [{"seq": 2, "hash": rows[2]["hash"]}],
            "AND seq > 2": [{"seq": 3, "hash": "chains-from-somewhere-else"}],
        })
        with pytest.raises(chain_export.TruncationRefused, match="does not link"):
            chain_export.truncate_chain(db.query, db.execute, doc=archive(rows, through=2))
        assert db.executed == []

    def test_an_unknown_chain_is_refused(self):
        rows = chain(6)
        doc = archive(rows, through=2)
        doc["chain"] = "some_other_table"
        doc["archive_hash"] = chain_export.archive_hash(doc)
        db = FakeDb({})
        with pytest.raises(chain_export.TruncationRefused, match="unknown chain"):
            chain_export.truncate_chain(db.query, db.execute, doc=doc)
        assert db.executed == []

    def test_the_scope_id_is_escaped(self):
        rows = chain(6, scope="ep'--drop")
        doc = archive(rows, through=2)
        doc["scope_id"] = "ep'--drop"
        doc["archive_hash"] = chain_export.archive_hash(doc)
        db = FakeDb({
            "AND seq = 2": [{"seq": 2, "hash": rows[2]["hash"]}],
            "AND seq > 2": [{"seq": 3, "hash": rows[3]["prev_hash"]}],
        })
        chain_export.truncate_chain(db.query, db.execute, doc=doc)
        assert "'ep''--drop'" in db.executed[1]


class TestTheChecklistIsNowSatisfied:
    def test_the_prerequisite_about_the_verifier_names_what_changed(self):
        """`TRUNCATION_PREREQUISITES` was written as the reason NOT to ship this."""
        assert any("verify_chain" in item for item in chain_export.truncation_prerequisites())

    def test_retention_still_refuses_the_automatic_path(self):
        """The scheduled pruner must stay refusing — this door is manual, per chain."""
        from app.memory import retention
        for name in ("decision_log", "memory_audit"):
            assert any(name in key for key in retention.REFUSED)
