"""An unreadable history store must not be rendered as an empty one.

`kube_q.cli.store` swallows every `sqlite3.Error` on purpose — a broken local cache must never
crash the REPL, and its module docstring promises exactly that. But each read then returns `[]`,
and `[]` is also what a fresh install returns. Measured before this was fixed, against a
`~/.kube-q/history.db` containing one line of text:

    $ kq --list
    No sessions found.
    $ echo $?
    0

Nothing on stderr either: the `_logger.warning` that was supposed to say otherwise goes to a
`kube_q` logger with no stderr handler outside `--debug`. A user with a hundred sessions was told
they had none, with no hint that deleting one corrupt file would bring them all back.

The rule these tests hold: an empty result is only reported as empty when the read succeeded.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re
import sqlite3

import pytest

from kube_q.cli import renderer, sessions_ui
from kube_q.cli import store as store_mod

_NOT_A_DATABASE = "this file is text, not a sqlite database\n"


@pytest.fixture
def clean_db(tmp_path, monkeypatch):
    """A real, empty, perfectly readable store."""
    monkeypatch.setattr(store_mod, "DB_PATH", tmp_path / "history.db")
    store_mod.list_sessions(5)          # creates + migrates the schema
    return tmp_path / "history.db"


@pytest.fixture
def broken_db(tmp_path, monkeypatch):
    """A store that exists and cannot be read — the case that used to print 'No sessions'."""
    db = tmp_path / "history.db"
    db.write_text(_NOT_A_DATABASE, encoding="utf-8")
    monkeypatch.setattr(store_mod, "DB_PATH", db)
    return db


class TestTheStoreRemembersWhyItFailed:
    def test_a_failed_read_still_returns_empty(self, broken_db):
        """The swallow itself is deliberate and stays — the REPL must not crash."""
        assert store_mod.list_sessions(20) == []

    def test_and_says_so(self, broken_db):
        store_mod.list_sessions(20)
        err = store_mod.last_error()
        assert err is not None
        assert "list_sessions" in err
        assert "not a database" in err

    def test_a_clean_read_reports_nothing(self, clean_db):
        """Vacuity guard: without this, "reports the failure" would pass for a store that
        always claims to be broken."""
        assert store_mod.list_sessions(20) == []
        assert store_mod.last_error() is None
        assert store_mod.empty_result_note() == ""

    def test_the_error_describes_the_last_operation_not_an_older_one(self, tmp_path, monkeypatch):
        """A stale failure printed beside a healthy empty result would be a new lie in the
        other direction."""
        broken = tmp_path / "history.db"
        broken.write_text(_NOT_A_DATABASE, encoding="utf-8")
        monkeypatch.setattr(store_mod, "DB_PATH", broken)
        store_mod.list_sessions(20)
        assert store_mod.last_error() is not None

        broken.unlink()                                   # the user deleted the corrupt file
        assert store_mod.list_sessions(20) == []
        assert store_mod.last_error() is None

    def test_search_and_branches_report_too(self, broken_db):
        """`--list` is not the only path that renders an empty result."""
        assert store_mod.search_sessions("anything") == []
        assert "search_sessions" in (store_mod.last_error() or "")
        assert store_mod.list_branches("some-session") == []
        assert "list_branches" in (store_mod.last_error() or "")

    def test_the_note_names_the_file_to_delete(self, broken_db):
        store_mod.list_sessions(20)
        note = store_mod.empty_result_note()
        assert str(broken_db) in note, "an unactionable warning is barely better than silence"


class TestEverySwallowRecords:
    """A guard, not a behaviour test: the next `except sqlite3.Error` must not go back to a
    bare `_logger.warning`, which is what made this invisible in the first place."""

    def _handlers(self) -> list[ast.ExceptHandler]:
        src = pathlib.Path(inspect.getfile(store_mod)).read_text(encoding="utf-8")
        tree = ast.parse(src)
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler) or node.type is None:
                continue
            name = ast.unparse(node.type)
            if "sqlite3.Error" in name:
                found.append(node)
        return found

    def test_the_guard_has_something_to_check(self):
        handlers = self._handlers()
        assert len(handlers) >= 10, f"only {len(handlers)} handlers found — the scan is wrong"

    def test_each_one_records_the_failure(self):
        for handler in self._handlers():
            body = ast.unparse(ast.Module(body=handler.body, type_ignores=[]))
            assert "_failed(" in body, (
                f"a swallowed sqlite3.Error at line {handler.lineno} records nothing — "
                f"an empty result from it is indistinguishable from a real one:\n{body}"
            )

    def test_no_read_logs_without_recording(self):
        """`_failed` itself logs `store.%s failed` — the banned shape is the per-function
        literal it replaced (`store.list_sessions failed: %s`)."""
        src = pathlib.Path(inspect.getfile(store_mod)).read_text(encoding="utf-8")
        stragglers = re.findall(r'_logger\.warning\("store\.[a-z_]+ failed', src)
        assert not stragglers, (
            f"the bare-warning form is back ({stragglers}) — it writes to a logger with no "
            f"stderr handler outside --debug, so the failure reaches nobody"
        )


class TestTheUserSeesIt:
    def test_the_sessions_table_warns_instead_of_saying_empty(self, broken_db, capsys):
        store_mod.list_sessions(20)
        renderer._print_sessions_table([])
        out = capsys.readouterr().out
        assert "could not be read" in out
        assert "No sessions found" not in out

    def test_and_still_says_empty_when_it_is(self, clean_db, capsys):
        store_mod.list_sessions(20)
        renderer._print_sessions_table([])
        out = capsys.readouterr().out
        assert "No sessions found" in out
        assert "could not be read" not in out

    def test_branches_too(self, broken_db, capsys):
        store_mod.list_branches("s")
        renderer.format_branches([], "s")
        out = capsys.readouterr().out
        assert "could not be read" in out
        assert "No branches" not in out

    def test_the_picker_too(self, broken_db, capsys):
        assert sessions_ui._pick_session_interactive(5) is None
        out = capsys.readouterr().out
        assert "could not be read" in out
        assert "No sessions found" not in out

    @pytest.mark.parametrize("module_name", ["kube_q.cli.main", "kube_q.cli.repl"])
    def test_the_search_paths_ask_before_saying_no_match(self, module_name):
        """Both `kq --search` and the REPL's `/search` render their own empty line, so neither
        goes through `_print_sessions_table`."""
        import importlib
        src = inspect.getsource(importlib.import_module(module_name))
        assert "No sessions matched" in src
        assert "print_store_failure()" in src, (
            f"{module_name} prints 'No sessions matched' without asking whether the search "
            f"actually ran"
        )


def test_a_corrupt_store_is_what_this_is_about(broken_db):
    """The fixture must really be unreadable — otherwise every test above is vacuous."""
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(str(broken_db)).execute("SELECT 1 FROM sessions")
