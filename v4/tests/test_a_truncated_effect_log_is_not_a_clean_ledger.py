"""ADR-008 effect log — truncating the newest rows must fail closed, not re-enable a replay.

`effect_log.verify` proves the links of a per-session hash chain. Deleting the NEWEST rows
breaks no link — the surviving prefix still verifies — and a missing `effect` row is exactly
what lets an irreversible call run a second time. The flight recorder solved this for
`decision_log` with a head anchor (`decision_log_head`); the effect log now carries the same
(`effect_log_head`), advanced in the same transaction as every append and checked on every open.

What would have to break for each test to fail:

* head not written with the append — a freshly written ledger is rejected as truncated;
* head not compared — a ledger with its newest rows removed opens as clean (the bug);
* a missing head tolerated for a non-empty ledger — deleting the head makes truncation invisible;
* a fresh session rejected — the guard could never record a first call.

The fake `psycopg` below models only what `PostgresEffectStore` issues; it keeps the real hash
chain and the real SQL strings, so the head logic under test is the production code.
"""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from types import ModuleType

import pytest
from app.db import effect_log
from app.db.effect_log import EffectLogUnavailable, PostgresEffectStore, head_problem


class _Db:
    def __init__(self) -> None:
        self.rows: dict[str, list[tuple]] = {}      # session -> [(seq, kind, payload, prev, hash)]
        self.heads: dict[str, tuple[int, str]] = {}
        self.head_read_fails = False


class _Cur:
    def __init__(self, db: _Db) -> None:
        self.db = db
        self._out: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if "pg_advisory_xact_lock" in s:
            self._out = []
        elif s.startswith("SELECT seq, kind, payload"):
            self._out = [(q, k, json.dumps(p), prev, h, "t")
                         for q, k, p, prev, h in self.db.rows.get(params[0], [])]
        elif s.startswith("SELECT seq, hash FROM effect_log_head"):
            if self.db.head_read_fails:
                raise RuntimeError('relation "effect_log_head" does not exist')
            head = self.db.heads.get(params[0])
            self._out = [head] if head else []
        elif s.startswith("INSERT INTO effect_log_head"):
            sid, seq, digest = params
            self.db.heads[sid] = (seq, digest)
        elif s.startswith("INSERT INTO effect_log"):
            sid, seq, kind, _rp, _br, _tool, _key, payload, prev, digest = params
            self.db.rows.setdefault(sid, []).append(
                (seq, kind, json.loads(payload), prev, digest))
            self._out = [("t",)]
        else:
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchall(self):
        return self._out

    def fetchone(self):
        return self._out[0] if self._out else None


class _Conn:
    def __init__(self, db: _Db) -> None:
        self.db = db

    @contextmanager
    def transaction(self):
        yield

    def cursor(self):
        return _Cur(self.db)

    def close(self):
        pass


@pytest.fixture
def db(monkeypatch):
    database = _Db()
    fake = ModuleType("psycopg")
    fake.connect = lambda dsn, connect_timeout=3: _Conn(database)   # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    monkeypatch.setattr(effect_log, "_mirror", lambda *a, **k: None)
    return database


def _append(session: str, kind: str, n: int = 1) -> None:
    store = PostgresEffectStore("postgresql://fake")
    for i in range(n):
        with store.transaction(session) as txn:
            txn.append(kind, {"rollback_point": "rp", "canonical_key": f"k{i}",
                              "tool": "run_kubectl"})


class TestHeadProblem:
    ROW = {"seq": 2, "hash": "h2"}

    def test_empty_ledger_with_no_head_is_a_fresh_session(self):
        assert head_problem([], None) is None

    def test_matching_head_is_clean(self):
        assert head_problem([{"seq": 0, "hash": "h0"}, self.ROW], (2, "h2")) is None

    def test_rows_without_a_head_are_rejected(self):
        assert "tampered/truncated" in (head_problem([self.ROW], None) or "")

    def test_a_head_with_no_rows_is_a_wiped_ledger(self):
        assert "tampered/truncated" in (head_problem([], (2, "h2")) or "")

    def test_a_ledger_shorter_than_its_head_is_truncated(self):
        assert "tampered/truncated" in (head_problem([{"seq": 1, "hash": "h1"}], (2, "h2")) or "")

    def test_a_ledger_ahead_of_its_head_is_not_trusted_either(self):
        # The head is written in the same transaction, so it cannot legitimately lag.
        assert "tampered/truncated" in (head_problem([self.ROW], (1, "h1")) or "")

    def test_same_seq_different_hash_is_rejected(self):
        assert "tampered/truncated" in (head_problem([self.ROW], (2, "other")) or "")


class TestTheStoreChecksTheAnchor:
    def test_a_fresh_session_opens_and_each_append_advances_the_head(self, db):
        _append("s1", effect_log.INTENT, 3)
        assert db.heads["s1"][0] == 2
        assert db.heads["s1"][1] == db.rows["s1"][-1][4]

    def test_an_untouched_ledger_reopens(self, db):
        _append("s1", effect_log.INTENT, 3)
        with PostgresEffectStore("x").transaction("s1") as txn:
            assert len(txn.rows) == 3

    def test_truncating_the_newest_rows_fails_closed(self, db):
        """The reported gap: the surviving prefix still verifies, only the anchor sees it."""
        _append("s1", effect_log.INTENT, 3)
        db.rows["s1"].pop()                       # drop the newest row; every link still holds
        assert effect_log.verify([
            {"session_id": "s1", "seq": q, "kind": k, "payload": p, "prev_hash": pv, "hash": h}
            for q, k, p, pv, h in db.rows["s1"]
        ]), "precondition: the truncated prefix is a valid chain"
        with pytest.raises(EffectLogUnavailable, match="effect log tampered/truncated"):
            with PostgresEffectStore("x").transaction("s1"):
                pytest.fail("a truncated ledger must not be handed to the guard")

    def test_wiping_the_session_is_caught_while_the_head_survives(self, db):
        _append("s1", effect_log.INTENT, 2)
        db.rows["s1"].clear()
        with pytest.raises(EffectLogUnavailable, match="tampered/truncated"):
            with PostgresEffectStore("x").transaction("s1"):
                pass

    def test_deleting_the_head_of_a_non_empty_ledger_is_caught(self, db):
        _append("s1", effect_log.INTENT, 2)
        del db.heads["s1"]
        with pytest.raises(EffectLogUnavailable, match="tampered/truncated"):
            with PostgresEffectStore("x").transaction("s1"):
                pass

    def test_an_unreadable_head_cannot_clear_the_ledger(self, db):
        _append("s1", effect_log.INTENT, 1)
        db.head_read_fails = True
        with pytest.raises(EffectLogUnavailable, match="head read failed"):
            with PostgresEffectStore("x").transaction("s1"):
                pass

    def test_other_sessions_are_unaffected(self, db):
        _append("s1", effect_log.INTENT, 2)
        _append("s2", effect_log.INTENT, 1)
        db.rows["s1"].pop()
        with PostgresEffectStore("x").transaction("s2") as txn:
            assert len(txn.rows) == 1


class TestTheSchemaCarriesTheAnchor:
    def test_the_head_table_exists_and_cannot_rewind_or_be_deleted(self):
        from app.db.schema_version import schema_sql
        sql = schema_sql()
        assert "CREATE TABLE IF NOT EXISTS effect_log_head" in sql
        assert "effect_log_head may only advance" in sql
        assert "BEFORE TRUNCATE ON effect_log_head" in sql
        assert "BEFORE TRUNCATE ON effect_log\n" in sql

    def test_the_head_is_never_pruned_and_is_a_backup_chain(self):
        from app.db.backup import CHAINS
        from app.memory import retention
        assert "effect_log_head" in retention.REFUSED
        assert ("effect_log", "effect_log_head", "session_id") in CHAINS

    def test_the_helm_chart_ships_the_same_schema(self):
        from pathlib import Path
        text = Path("deploy/helm/kubeintellect/templates/configmap-schema.yaml").read_text(
            encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS effect_log_head" in text
