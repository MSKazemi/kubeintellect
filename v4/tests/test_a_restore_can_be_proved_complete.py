"""A12 — a backup now carries proof it restored, and the proof covers the case nothing else can.

`docs/operations.md` already had the right `pg_dump` / `psql` commands and the right warning about
`ON_ERROR_STOP=1`. What no operator could answer afterwards was *did everything come back*.

For most tables a wrong answer is lost data. For `decision_log` and `memory_audit` it is worse:
they are hash chains, and a restore that drops the **newest** rows of a chain breaks no link. The
surviving rows hash correctly, `verify_chain` returns True, the postmortem prints its intact-chain
banner — over a record that is quietly short. The head anchors exist to catch exactly that, and
they only work if something compares them. Nothing did.

So the claims here are: the manifest measures the right things, `verify` reports **every**
discrepancy rather than the first, a truncated chain is named as a truncated chain rather than as
a row-count mismatch, and a manifest taken from an already-damaged source is not silently adopted
as the definition of correct.
"""
from __future__ import annotations

import inspect

import pytest

from app.db import backup
from app.db.backup import (
    CHAINS,
    COUNTED_TABLES,
    MANIFEST_VERSION,
    build_manifest,
    verify,
)
from app.db.schema_version import SCHEMA_VERSION, schema_fingerprint


class FakeDb:
    """Answers `SELECT count(*) FROM t` from a dict; chain queries from `unreached`."""

    def __init__(self, counts: dict[str, int], anchors: dict[str, int] | None = None,
                 unreached: dict[str, int] | None = None,
                 missing: tuple[str, ...] = ()) -> None:
        self.counts = counts
        self.anchors = anchors or {"decision_log": 0, "memory_audit": 0}
        self.unreached = unreached or {"decision_log": 0, "memory_audit": 0}
        self.missing = set(missing)
        self.seen: list[str] = []

    def __call__(self, sql: str):
        self.seen.append(sql)
        for table in self.missing:
            if f"FROM {table}" in sql or f"FROM {table} " in sql:
                raise RuntimeError(f'relation "{table}" does not exist')
        for chain, anchor, _key in CHAINS:
            if sql == f"SELECT count(*) FROM {anchor}":
                return [(self.anchors[chain],)]
            if sql.startswith(f"SELECT count(*) FROM {anchor} h LEFT JOIN"):
                return [(self.unreached[chain],)]
        for table in COUNTED_TABLES:
            if sql == f"SELECT count(*) FROM {table}":
                return [(self.counts.get(table, 0),)]
        raise AssertionError(f"unexpected query: {sql}")


def _healthy(**over) -> FakeDb:
    counts = {t: 10 for t in COUNTED_TABLES}
    counts.update(over.pop("counts", {}))
    return FakeDb(counts, anchors={"decision_log": 4, "memory_audit": 2}, **over)


def _manifest(db: FakeDb | None = None) -> dict:
    return build_manifest(db or _healthy(), taken_at="2026-08-28T10:00:00+00:00", note="nightly")


class TestTheManifestMeasuresTheRightThings:
    def test_it_records_the_schema_it_was_taken_from(self):
        m = _manifest()
        assert m["schema_version"] == SCHEMA_VERSION
        assert m["schema_fingerprint"] == schema_fingerprint()

    def test_it_counts_every_table_whose_loss_is_data_loss(self):
        m = _manifest()
        assert set(m["row_counts"]) == set(COUNTED_TABLES)
        for table in ("episodes", "decision_log", "memory_audit"):
            assert table in m["row_counts"]

    def test_it_records_how_far_each_chain_got(self):
        """The part no row count can replace."""
        m = _manifest()
        assert set(m["chains"]) == {c[0] for c in CHAINS}
        assert m["chains"]["decision_log"]["anchors"] == 4

    def test_it_carries_when_and_why(self):
        m = _manifest()
        assert m["taken_at"].startswith("2026-08-28")
        assert m["note"] == "nightly"
        assert m["manifest_version"] == MANIFEST_VERSION


class TestAGoodRestoreVerifies:
    def test_an_identical_database_is_ok(self):
        m = _manifest()
        result = verify(_healthy(), m)
        assert result["ok"] is True
        assert result["problems"] == []
        assert result["checked"] > len(COUNTED_TABLES)


class TestABadRestoreIsNamedPrecisely:
    def test_a_short_table_says_how_many_are_missing(self):
        m = _manifest()
        result = verify(_healthy(counts={"episodes": 3}), m)
        assert result["ok"] is False
        assert any("episodes: 3 rows" in p and "7 MISSING" in p for p in result["problems"])

    def test_extra_rows_are_reported_too_and_not_as_missing(self):
        """A restore onto a non-empty database is a different mistake, not a safe one."""
        result = verify(_healthy(counts={"episodes": 25}), _manifest())
        assert any("extra" in p for p in result["problems"])
        assert not any("MISSING" in p for p in result["problems"])

    def test_a_table_the_restore_never_created_is_its_own_sentence(self):
        result = verify(_healthy(missing=("runbooks",)), _manifest())
        assert any("cannot be read" in p and "did not create it" in p for p in result["problems"])

    def test_every_problem_is_reported_not_just_the_first(self):
        """Mid-incident, a list of three things to fix beats one error and a re-run."""
        db = _healthy(counts={"episodes": 1, "kg_edges": 2, "runbooks": 3})
        result = verify(db, _manifest())
        assert len(result["problems"]) >= 3

    def test_a_different_schema_version_is_reported_without_hiding_the_counts(self):
        m = _manifest()
        m["schema_version"] = SCHEMA_VERSION + 5
        result = verify(_healthy(counts={"episodes": 0}), m)
        assert any("schema version differs" in p for p in result["problems"])
        assert any("episodes" in p for p in result["problems"])

    def test_a_changed_fingerprint_at_the_same_version_is_reported(self):
        m = _manifest()
        m["schema_fingerprint"] = "0" * 64
        result = verify(_healthy(), m)
        assert any("fingerprint differs at the same version" in p for p in result["problems"])


class TestTheTruncatedChainIsTheWholePoint:
    def test_a_chain_that_no_longer_reaches_its_head_is_caught(self):
        m = _manifest()
        restored = _healthy(unreached={"decision_log": 2, "memory_audit": 0})
        result = verify(restored, m)
        assert result["ok"] is False
        assert any("do not reach their recorded head" in p for p in result["problems"])

    def test_the_message_says_why_verify_chain_cannot_see_it(self):
        """An operator reading this must not go and 'check the chain' to disprove it."""
        result = verify(_healthy(unreached={"decision_log": 1, "memory_audit": 0}), _manifest())
        problem = next(p for p in result["problems"] if "recorded head" in p)
        assert "verify_chain" in problem and "CANNOT see" in problem

    def test_row_counts_alone_would_have_missed_it(self):
        """The control: every count matches, and the restore is still wrong."""
        m = _manifest()
        restored = _healthy(unreached={"decision_log": 3, "memory_audit": 0})
        assert all(restored.counts[t] == m["row_counts"][t] for t in COUNTED_TABLES)
        assert verify(restored, m)["ok"] is False

    def test_a_lost_anchor_row_is_caught_separately(self):
        m = _manifest()
        restored = _healthy()
        restored.anchors["decision_log"] = 1
        assert any("anchor rows" in p for p in verify(restored, m)["problems"])

    def test_damage_that_predates_the_backup_is_not_adopted_as_correct(self):
        """A manifest from an already-broken source must not define 'broken' as fine..."""
        m = _manifest(_healthy(unreached={"decision_log": 2, "memory_audit": 0}))
        assert m["chains"]["decision_log"]["unreached_at_backup"] == 2
        # ...but the restore is not blamed for damage it faithfully reproduced.
        same = _healthy(unreached={"decision_log": 2, "memory_audit": 0})
        assert verify(same, m)["ok"] is True
        # New damage on top of it still is.
        worse = _healthy(unreached={"decision_log": 3, "memory_audit": 0})
        assert verify(worse, m)["ok"] is False


class TestItRefusesToGuess:
    def test_a_newer_manifest_is_refused_rather_than_half_read(self):
        m = _manifest()
        m["manifest_version"] = MANIFEST_VERSION + 1
        result = verify(_healthy(), m)
        assert result["ok"] is False
        assert result["checked"] == 0
        assert any("verify with the build that took it" in p for p in result["problems"])

    def test_a_chain_check_that_cannot_run_is_reported_not_skipped(self):
        result = verify(_healthy(missing=("decision_log_head",)), _manifest())
        assert any("chain check could not run" in p for p in result["problems"])

    def test_nothing_here_writes_to_the_database(self):
        src = inspect.getsource(backup)
        for verb in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "TRUNCATE "):
            assert verb not in src.upper().replace("DELETED", ""), f"{verb} in a read-only module"


class TestTheOperatorCanActuallyRunIt:
    @pytest.mark.parametrize("command", ["backup-manifest", "verify-restore"])
    def test_the_command_exists(self, command):
        from app import cli
        src = inspect.getsource(cli.main)
        assert f'"{command}"' in src

    def test_verify_restore_exits_nonzero_on_a_bad_restore(self):
        from app import cli
        src = inspect.getsource(cli.cmd_verify_restore)
        assert "sys.exit(1)" in src, "a rehearsal that always exits 0 proves nothing"

    def test_the_docs_tell_an_operator_to_take_the_manifest_with_the_dump(self):
        from pathlib import Path
        ops = Path(__file__).resolve().parents[1] / "docs" / "operations.md"
        text = ops.read_text(encoding="utf-8")
        assert "backup-manifest" in text and "verify-restore" in text
        assert "RPO" in text and "RTO" in text


class TestWhatIsStillMissingIsWrittenDown:
    def test_the_module_states_it_is_only_the_verification_half(self):
        doc = " ".join((backup.__doc__ or "").split())
        assert "no scheduled backup in the Helm chart" in doc
        assert "no off-site copy" in doc
        assert "2026-08-28" in doc
