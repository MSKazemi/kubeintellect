"""The two fastest-growing tables in the schema are the two nothing could ever bound.

`memory/retention.py` refuses to prune `decision_log` and `memory_audit`, and the refusal is
right: they are hash-chained tamper-evidence, deleting their newest rows breaks no link, and a
clock-driven `DELETE` would make an install's own housekeeping indistinguishable from an attack
for ever. Its written reason ends *"needs a signed export-then-truncate flow, not a `DELETE`"*.
No such flow existed, so the refusal was permanent and A10 could not reach green.

This is the **export** half, and it is worth shipping alone: before it there was no way to take
a *verifiable* copy of either chain off the box at all — `pg_dump` gives you a copy, and a copy
of tamper-evidence that cannot itself be checked is not evidence.

What is claimed here:

* an archive verifies **with no database present**, years later, from the file alone;
* a segment that does not start at seq 0 verifies — `flight_recorder.verify_chain` starts at
  seq 0 with an empty prev_hash, so reusing it would have called every partial archive broken;
* editing the archive is caught, and editing the archive's rows is caught *separately*, because
  the two need different responses from whoever reads the report;
* an archive of an ALREADY-broken chain is allowed and says so — the archive is how you keep
  the evidence of a break, and refusing to write one would destroy it;
* nothing here deletes anything, and the checklist that must come first is executable data
  rather than a plan, with the verifier change named as the item that is not about data.
"""
from __future__ import annotations

import json

import pytest

from app.db import chain_export
from app.db.flight_recorder import compute_hash

SCOPE = "ep-1"


def chain(n: int, scope: str = SCOPE, start: int = 0, prev: str = "") -> list[dict]:
    rows = []
    for i in range(n):
        seq = start + i
        payload = {"step": seq}
        h = compute_hash(prev, scope, seq, "tool_call", payload)
        rows.append({"seq": seq, "kind": "tool_call", "payload": payload,
                     "prev_hash": prev, "hash": h})
        prev = h
    return rows


def fake_query(rows: list[dict], anchor: dict | None):
    """A `query(sql) -> rows` that answers by which table the SQL names."""
    def query(sql: str):
        if "_head" in sql or "chain_head" in sql:
            return [anchor] if anchor else []
        return [dict(r) for r in rows]
    return query


def export(rows, anchor=None, *, chain_name="decision_log", scope=SCOPE, **kw):
    return chain_export.build_export(
        fake_query(rows, anchor), chain=chain_name, scope_id=scope,
        taken_at="2026-08-28T00:00:00+00:00", **kw)


def anchor_of(rows: list[dict]) -> dict:
    return {"seq": rows[-1]["seq"], "hash": rows[-1]["hash"]}


class TestAnArchiveChecksItselfWithNoDatabase:
    def test_a_whole_chain_verifies(self):
        rows = chain(5)
        doc = export(rows, anchor_of(rows))
        assert chain_export.verify_export(doc) == {"ok": True, "problems": [], "checked": 4}

    def test_it_survives_a_round_trip_through_json(self):
        """The archive is a file. If it only verifies in memory it verifies nowhere useful."""
        rows = chain(5)
        doc = json.loads(json.dumps(export(rows, anchor_of(rows))))
        assert chain_export.verify_export(doc)["ok"] is True

    def test_the_rows_are_carried_verbatim(self):
        rows = chain(3)
        doc = export(rows, anchor_of(rows))
        assert [r["hash"] for r in doc["rows"]] == [r["hash"] for r in rows]
        assert doc["row_count"] == 3
        assert doc["end_hash"] == rows[-1]["hash"]

    def test_the_anchor_is_recorded_as_it_stood(self):
        rows = chain(4)
        doc = export(rows, anchor_of(rows))
        assert doc["anchor"] == {"seq": 3, "hash": rows[-1]["hash"]}

    def test_an_archive_with_no_anchor_says_what_it_cannot_prove(self):
        """It proves its rows chain. It does not prove they are all of them."""
        rows = chain(4)
        doc = export(rows, None)
        problems = chain_export.verify_export(doc)["problems"]
        assert any("not that they are all of them" in p for p in problems)

    def test_a_future_archive_version_is_refused_not_guessed(self):
        doc = export(chain(3), anchor_of(chain(3)))
        doc["archive_version"] = chain_export.ARCHIVE_VERSION + 1
        result = chain_export.verify_export(doc)
        assert result["ok"] is False
        assert "verify it with the build that wrote it" in result["problems"][0]


class TestASegmentIsNotAWholeChain:
    def test_a_segment_starting_mid_chain_verifies(self):
        """`flight_recorder.verify_chain` starts at seq 0 — every partial archive would fail it."""
        whole = chain(10)
        segment = whole[4:]
        assert chain_export.verify_segment(
            segment, scope_id=SCOPE, start_prev_hash=segment[0]["prev_hash"]) is True

    def test_the_whole_chain_verifier_would_have_rejected_it(self):
        """States the reason this module has its own verifier, rather than asserting it."""
        from app.db.flight_recorder import verify_chain
        whole = chain(10)
        adapted = [{"episode_id": SCOPE, **r} for r in whole[4:]]
        assert verify_chain(adapted) is False

    def test_through_seq_bounds_the_archive(self):
        rows = chain(10)
        doc = export(rows[:6], anchor_of(rows), through_seq=5)
        assert (doc["from_seq"], doc["through_seq"], doc["row_count"]) == (0, 5, 6)

    def test_an_archive_ahead_of_its_own_anchor_is_reported(self):
        """The source chain had rows its head did not know about — a finding, not a crash."""
        rows = chain(6)
        doc = export(rows, {"seq": 2, "hash": rows[2]["hash"]})
        assert any("already ahead of its own head" in p
                   for p in chain_export.verify_export(doc)["problems"])

    def test_a_chain_that_ends_before_its_anchor_is_reported_as_a_truncation(self):
        """Every link verifies; only the anchor knows rows are missing."""
        rows = chain(8)
        doc = export(rows[:5], {"seq": 7, "hash": rows[7]["hash"]})
        assert doc["links_verified_at_export"] is True
        assert any("3 entr(y/ies) were missing" in p
                   for p in chain_export.verify_export(doc)["problems"])

    def test_a_deliberately_bounded_archive_is_not_called_a_truncation(self):
        """`--through-seq` is a request, not damage. Reporting it would train the alarm away."""
        rows = chain(8)
        doc = export(rows[:5], {"seq": 7, "hash": rows[7]["hash"]}, through_seq=4)
        assert chain_export.verify_export(doc)["problems"] == []

    def test_an_empty_chain_archives_without_error(self):
        doc = export([], None)
        assert doc["row_count"] == 0 and doc["from_seq"] is None
        assert doc["links_verified_at_export"] is True


class TestTamperingWithTheArchiveIsCaught:
    def test_editing_the_note_breaks_the_archive_hash(self):
        rows = chain(5)
        doc = export(rows, anchor_of(rows))
        doc["note"] = "nothing to see"
        problems = chain_export.verify_export(doc)["problems"]
        assert any("edited after it was written" in p for p in problems)

    def test_editing_a_row_is_reported_as_its_own_problem(self):
        """Two different failures, two different sentences — they need different responses."""
        rows = chain(5)
        doc = export(rows, anchor_of(rows))
        doc["rows"][2]["payload"] = {"step": "redacted"}
        problems = chain_export.verify_export(doc)["problems"]
        assert any("do not chain" in p for p in problems)
        assert any("edited after it was written" in p for p in problems)

    def test_dropping_a_row_and_rehashing_the_archive_is_still_caught(self):
        """The attacker who knows about archive_hash. The chain is what defeats them."""
        rows = chain(5)
        doc = export(rows, anchor_of(rows))
        del doc["rows"][2]
        doc["archive_hash"] = chain_export.archive_hash(doc)
        result = chain_export.verify_export(doc)
        assert result["ok"] is False
        assert any("do not chain" in p for p in result["problems"])

    def test_a_missing_archive_hash_is_not_silently_accepted(self):
        rows = chain(5)
        doc = export(rows, anchor_of(rows))
        del doc["archive_hash"]
        assert any("carries no archive_hash" in p
                   for p in chain_export.verify_export(doc)["problems"])

    def test_the_limit_of_a_content_hash_travels_with_the_archive(self):
        """"Signed" must not be read as "attributable" — the archive says so itself."""
        doc = export(chain(2), None)
        assert "does NOT prove who wrote it" in doc["limit"]


class TestABrokenChainIsArchivedNotRefused:
    def test_a_broken_chain_exports_and_records_that_it_is_broken(self):
        """The archive is how you keep the evidence OF a break."""
        rows = chain(5)
        rows[2]["payload"] = {"step": "tampered"}
        doc = export(rows, anchor_of(rows))
        assert doc["links_verified_at_export"] is False
        assert doc["row_count"] == 5

    def test_repairing_the_rows_after_export_is_reported(self):
        """An archive that claims broken and now verifies is a rewrite of evidence."""
        rows = chain(5)
        doc = export(rows, anchor_of(rows))
        doc["links_verified_at_export"] = False
        doc["archive_hash"] = chain_export.archive_hash(doc)
        assert any("repaired after export" in p
                   for p in chain_export.verify_export(doc)["problems"])


class TestNothingHereDeletesAnything:
    def test_every_statement_this_module_runs_is_a_select(self):
        """Exporting is safe and repeatable; truncating is neither. Checked, not asserted."""
        import re
        from pathlib import Path
        src = Path(chain_export.__file__).read_text(encoding="utf-8")
        verbs = re.findall(r"query\(\s*\n?\s*f?\"(\w+)", src)
        assert verbs and set(verbs) == {"SELECT"}, verbs

    def test_the_prerequisites_name_the_verifier_change(self):
        """The item that is not about data, and the reason no DELETE ships yet."""
        joined = " ".join(chain_export.truncation_prerequisites())
        assert "verify_chain" in joined and "starts at seq 0" in joined

    def test_retention_still_refuses_both_ledgers(self):
        from app.memory.retention import REFUSED
        assert {"decision_log", "memory_audit"} <= set(REFUSED)

    def test_both_refused_ledgers_are_exportable(self):
        """A refusal that points at a flow which does not cover it is not a plan."""
        from app.memory.retention import REFUSED
        for table in ("decision_log", "memory_audit"):
            assert table in chain_export.CHAINS
            assert "export" in REFUSED[table].lower()

    def test_an_unknown_chain_is_refused_rather_than_interpolated(self):
        with pytest.raises(ValueError, match="unknown chain"):
            chain_export.build_export(
                fake_query([], None), chain="request_log", scope_id="x", taken_at="t")
