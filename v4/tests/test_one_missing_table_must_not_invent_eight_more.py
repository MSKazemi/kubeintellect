"""`verify-restore` reports every problem — the driver underneath it must let it.

`app.db.backup.verify` is written to report **every** discrepancy rather than the first: it
catches each check's exception and carries on, because mid-incident a list of three things to
fix beats one error and a re-run. That design is defeated one layer down. `_backup_query` hands
it a psycopg connection, and in a transaction a single failing statement poisons the connection
— every later query dies with *current transaction is aborted*.

Measured 2026-08-28 against a real restore (PostgreSQL 17.11) missing exactly one table:

    ✗  9 discrepancy(ies) after 18 check(s):
      • memory_audit: cannot be read (relation "memory_audit" does not exist
      • promotion_outcomes: cannot be read (current transaction is aborted ...) — the restore did not create it
      • prospective_memory: ... rca_outcomes: ... runbooks: ... semantic_rules: ... user_prefs: ...
      • decision_log: chain check could not run (current transaction is aborted ...)
      • memory_audit: chain check could not run (current transaction is aborted ...)

One true finding and **eight false ones** — six of them stating `the restore did not create it`
about tables that were present and correct. An operator reading that mid-recovery concludes seven
tables were lost when one was. After the fix the same database reports 2 discrepancies, both
`memory_audit`, both true.

Nothing caught it because the suite drives `verify` with a dict `FakeDb`, and a dict has no
transaction to poison. So the claim these tests make is about the **adapter contract**: every
query `backup` issues has to stand on its own.
"""
from __future__ import annotations

import sys
import types

import pytest

from app.db.backup import CHAINS, COUNTED_TABLES, verify


class PoisonableConnection:
    """A connection that behaves like psycopg: outside autocommit, one failure poisons it."""

    def __init__(self, missing: tuple[str, ...] = ()) -> None:
        self.autocommit = False
        self.missing = missing
        self._aborted = False
        self.closed = False

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def execute(self_inner, sql):
                if conn._aborted and not conn.autocommit:
                    raise RuntimeError(
                        "current transaction is aborted, commands ignored until end of "
                        "transaction block")
                for t in conn.missing:
                    if f" {t}" in sql or f"FROM {t}" in sql:
                        if not conn.autocommit:
                            conn._aborted = True
                        raise RuntimeError(f'relation "{t}" does not exist')
                self_inner._rows = [(0,)]

            def fetchall(self_inner):
                return self_inner._rows

        return _Cur()

    def close(self):
        self.closed = True


def _query_over(conn):
    def query(sql):
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    return query


def _manifest():
    return {
        "manifest_version": 1,
        "schema_version": __import__(
            "app.db.schema_version", fromlist=["x"]).SCHEMA_VERSION,
        "schema_fingerprint": __import__(
            "app.db.schema_version", fromlist=["x"]).schema_fingerprint(),
        "row_counts": {t: 0 for t in COUNTED_TABLES},
        "chains": {c: {"anchors": 0, "unreached_at_backup": 0} for c, _, _ in CHAINS},
    }


class TestTheAdapterMustNotPoisonItsOwnReport:
    def test_the_cli_adapter_opens_the_connection_in_autocommit(self, monkeypatch):
        """The actual fix: without this, everything below is unreachable in production."""
        made = PoisonableConnection()
        fake = types.ModuleType("psycopg")
        fake.connect = lambda dsn: made          # noqa: ARG005
        monkeypatch.setitem(sys.modules, "psycopg", fake)
        from app.cli import _backup_query

        conn, _query = _backup_query("postgresql://x/y")
        assert conn is made
        assert made.autocommit is True

    def test_one_missing_table_yields_one_missing_table(self):
        conn = PoisonableConnection(missing=("memory_audit",))
        conn.autocommit = True                   # what the fixed adapter does
        result = verify(_query_over(conn), _manifest())
        offenders = {p.split(":")[0] for p in result["problems"]}
        assert offenders == {"memory_audit"}, result["problems"]

    def test_without_autocommit_it_would_invent_the_rest(self):
        """Pins the failure being fixed, so a revert is a red test and not a quiet regression."""
        conn = PoisonableConnection(missing=("memory_audit",))
        assert conn.autocommit is False
        result = verify(_query_over(conn), _manifest())
        offenders = {p.split(":")[0] for p in result["problems"]}
        assert len(offenders) > 1

    def test_the_false_sentence_is_the_dangerous_one(self):
        """`the restore did not create it` about a table that exists is a false statement."""
        conn = PoisonableConnection(missing=("memory_audit",))
        poisoned = verify(_query_over(conn), _manifest())
        lied_about = [p for p in poisoned["problems"]
                      if "the restore did not create it" in p and not p.startswith("memory_audit")]
        assert lied_about, "expected the poisoned run to mis-report other tables"

        clean = PoisonableConnection(missing=("memory_audit",))
        clean.autocommit = True
        fixed = verify(_query_over(clean), _manifest())
        assert not [p for p in fixed["problems"]
                    if "the restore did not create it" in p and not p.startswith("memory_audit")]


class TestTheReasonThisWasInvisible:
    def test_the_adapter_documents_why_autocommit_is_load_bearing(self):
        import inspect

        from app.cli import _backup_query

        doc = inspect.getdoc(_backup_query) or ""
        assert "autocommit" in doc and "every" in doc

    @pytest.mark.parametrize("table", ["promotion_outcomes", "user_prefs", "runbooks"])
    def test_a_healthy_table_is_never_named(self, table):
        conn = PoisonableConnection(missing=("memory_audit",))
        conn.autocommit = True
        result = verify(_query_over(conn), _manifest())
        assert not [p for p in result["problems"] if p.startswith(table)]
