"""One memory section failing must not read as that section being empty.

`load_memory_context` already had the right idea at the outer level: pass 46 gave it
`MemoryStoreUnavailable` precisely because `""` means both "nothing stored" and "could not
look", and `memory_loader` turns that exception into a block telling the model *"this is not
the same as there being none"*.

Four section loaders defeated it one level in. Each wrapped its own query in
`try/except: return []` — two silently, two at `logger.debug`, which production never emits —
so a query error in exactly one section produced a context that reads as complete. Measured
against a `user_prefs` query raising `column "confidence" does not exist` (the shape of a
half-applied migration), with the other three sections healthy:

    ## Recent RCA History (this cluster)
      - [2026-08-20 ns=prod ✓] node pressure
        → drain

No notice, no exception, nothing above INFO in the log. The operator's stored preferences —
which is where "never restart pods in prod" lives — were simply not there, and the model had
every reason to believe the user had never set any.

The rule these tests hold: every section either loads or is named.
"""

from __future__ import annotations

import ast
import inspect
import logging
import pathlib

import pytest

class _Row(dict):
    def __getitem__(self, key):
        return dict.get(self, key)


_RCA_ROW = _Row(root_cause="node pressure", recommended_fix="drain", namespace="prod",
                date="2026-08-20", verified_resolved=True)
_PREF_ROW = _Row(key="restart_policy", value="never in prod", source="explicit", confidence=1.0)


class _Conn:
    """An asyncpg connection whose queries can be made to fail one table at a time."""

    def __init__(self, failing: set[str] | None = None, rows: dict | None = None):
        self.failing = failing or set()
        self.rows = rows or {}
        self.closed = False

    async def fetch(self, query, *args):
        for table, exc in (("user_prefs", 'column "confidence" does not exist'),
                           ("failure_patterns", 'relation "failure_patterns" does not exist'),
                           ("session_notes", "connection reset by peer"),
                           ("rca_outcomes", "canceling statement due to statement timeout")):
            if table in query:
                if table in self.failing:
                    raise RuntimeError(exc)
                return self.rows.get(table, [])
        return []

    async def close(self):
        self.closed = True


@pytest.fixture
def store(mocker):
    from app.db import memory_store

    mocker.patch.object(memory_store.settings, "USE_SQLITE", False)
    mocker.patch.object(memory_store.settings, "PREFERENCE_MEMORY_ENABLED", True)
    return memory_store


def _with_conn(mocker, store, conn):
    mocker.patch.object(store, "_get_conn", mocker.AsyncMock(return_value=conn))


@pytest.mark.asyncio
class TestAFailedSectionIsNamed:
    async def test_the_notice_names_the_section(self, mocker, store):
        _with_conn(mocker, store, _Conn(failing={"user_prefs"}, rows={"rca_outcomes": [_RCA_ROW]}))
        ctx = await store.load_memory_context("u1", "s1")
        assert "Memory partially unavailable" in ctx
        assert "operator preferences" in ctx

    async def test_the_healthy_sections_still_load(self, mocker, store):
        """The per-section catch exists so one bad query does not cost the other three."""
        _with_conn(mocker, store, _Conn(failing={"user_prefs"}, rows={"rca_outcomes": [_RCA_ROW]}))
        ctx = await store.load_memory_context("u1", "s1")
        assert "node pressure" in ctx

    async def test_a_healthy_load_says_nothing(self, mocker, store):
        """Vacuity guard: without it, "names the failure" would pass for a store that always
        claims to be broken."""
        _with_conn(mocker, store, _Conn(rows={"user_prefs": [_PREF_ROW],
                                              "rca_outcomes": [_RCA_ROW]}))
        ctx = await store.load_memory_context("u1", "s1")
        assert "partially unavailable" not in ctx
        assert "never in prod" in ctx

    async def test_no_rows_is_not_a_failure(self, mocker, store):
        """An empty section is the normal state for a new user and must stay silent."""
        _with_conn(mocker, store, _Conn())
        ctx = await store.load_memory_context("u1", "s1")
        assert ctx == ""

    @pytest.mark.parametrize("table,label", [
        ("user_prefs", "operator preferences"),
        ("failure_patterns", "failure hints"),
        ("session_notes", "session notes"),
        ("rca_outcomes", "past RCA"),
    ])
    async def test_every_section_reports(self, mocker, store, table, label):
        _with_conn(mocker, store, _Conn(failing={table}))
        ctx = await store.load_memory_context("u1", "s1")
        assert label in ctx, f"{table} failed silently"

    async def test_several_failures_are_all_named(self, mocker, store):
        _with_conn(mocker, store, _Conn(failing={"user_prefs", "session_notes"}))
        ctx = await store.load_memory_context("u1", "s1")
        assert "operator preferences" in ctx and "session notes" in ctx

    async def test_the_failure_is_logged_where_production_can_see_it(self, mocker, store, caplog):
        """Two of the four loaders did log — at `debug`, which a default deployment never
        emits. That is indistinguishable from silence."""
        _with_conn(mocker, store, _Conn(failing={"user_prefs"}))
        with caplog.at_level(logging.WARNING):
            await store.load_memory_context("u1", "s1")
        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("operator preferences" in m for m in messages), messages


@pytest.mark.asyncio
class TestTheNoticeSurvivesTruncation:
    async def test_it_is_not_cut_off_the_end(self, mocker, store):
        """The context is truncated to `_MAX_CONTEXT_CHARS` from the tail. A notice appended
        after 1,800 characters of RCA history would be removed before the model ever read it."""
        fat = [_Row(root_cause="x" * 900, recommended_fix="y" * 900, namespace="prod",
                    date="2026-08-20", verified_resolved=True) for _ in range(3)]
        _with_conn(mocker, store, _Conn(failing={"user_prefs"}, rows={"rca_outcomes": fat}))
        ctx = await store.load_memory_context("u1", "s1")
        assert len(ctx) > store._MAX_CONTEXT_CHARS - 100, "the fixture must actually overflow"
        assert "[context truncated]" in ctx
        assert "Memory partially unavailable" in ctx


@pytest.mark.asyncio
class TestTheOuterContractStillHolds:
    async def test_a_dead_connection_still_raises(self, mocker, store):
        """Regression guard: the per-section catch must not swallow a total outage into a
        partial one."""
        mocker.patch.object(store, "_get_conn",
                            mocker.AsyncMock(side_effect=RuntimeError("pg down")))
        with pytest.raises(store.MemoryStoreUnavailable):
            await store.load_memory_context("u1", "s1")

    async def test_sqlite_mode_is_still_not_an_outage(self, mocker, store):
        mocker.patch.object(store.settings, "USE_SQLITE", True)
        assert await store.load_memory_context("u1", "s1") == ""

    async def test_the_connection_is_closed_even_when_a_section_fails(self, mocker, store):
        conn = _Conn(failing={"user_prefs", "rca_outcomes"})
        _with_conn(mocker, store, conn)
        await store.load_memory_context("u1", "s1")
        assert conn.closed


class TestNoLoaderCatchesItsOwnFailureAgain:
    """A guard, not a behaviour test. The next `_load_*` written with its own
    `except: return []` would be invisible again, and no behavioural test would notice
    because a section nobody wrote a fixture for simply never fails."""

    def _loaders(self):
        from app.db import memory_store

        src = pathlib.Path(inspect.getfile(memory_store)).read_text(encoding="utf-8")
        tree = ast.parse(src)
        return [n for n in tree.body
                if isinstance(n, ast.AsyncFunctionDef) and n.name.startswith("_load_")]

    def test_the_guard_has_something_to_check(self):
        names = [n.name for n in self._loaders()]
        assert len(names) >= 4, f"the scan found only {names}"

    def test_none_of_them_swallow(self):
        for fn in self._loaders():
            for node in ast.walk(fn):
                if isinstance(node, ast.ExceptHandler):
                    body = ast.unparse(ast.Module(body=node.body, type_ignores=[]))
                    assert "raise" in body, (
                        f"{fn.name} catches its own failure at line {node.lineno} and returns "
                        f"without re-raising — `load_memory_context` cannot then name it:\n{body}"
                    )

    def test_the_orchestrator_names_every_loader(self):
        from app.db import memory_store

        src = inspect.getsource(memory_store.load_memory_context)
        for fn in self._loaders():
            assert fn.name in src, (
                f"{fn.name} exists but `load_memory_context` never calls it — a section that is "
                f"never loaded cannot be reported either"
            )
