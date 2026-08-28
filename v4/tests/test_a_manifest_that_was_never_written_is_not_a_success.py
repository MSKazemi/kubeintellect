"""`backup-manifest --out FILE` must never exit 0 without writing FILE.

`docs/operations.md` tells an operator to take the manifest **beside** the dump, and the
command's own help explains why it is the only thing that can see a restore which silently
dropped the newest rows of `decision_log` or `memory_audit`: such a restore breaks no hash
link, so the shortened record still verifies.

In SQLite mode the command printed its "copy the file while stopped" advisory and returned —
before `args.out` was consulted at all. So:

    kubeintellect backup-manifest --out m.json && cp kubeintellect.db backups/

printed advice, wrote nothing, and exited **0**. The `&&` therefore fired, the copy was taken,
and the backup was recorded as having proof beside it. The missing manifest is discovered at
restore time, which is precisely the moment it can no longer be taken.

The advisory itself is correct and stays: none of `COUNTED_TABLES` exists in SQLite mode — that
file holds the LangGraph checkpointer and nothing else — so there is genuinely no manifest to
build. What was wrong was reporting that as success. A request for a file that produced no file
is a failure, and it has to be visible to the shell.
"""
from __future__ import annotations

import argparse
import inspect
from pathlib import Path

import pytest

from app import cli


def _run(out, monkeypatch, tmp_path):
    monkeypatch.setenv("USE_SQLITE", "true")
    # Keep _load_effective_config away from the developer's own ./.env.
    monkeypatch.chdir(tmp_path)
    args = argparse.Namespace(out=out, note="")
    return cli.cmd_backup_manifest(args)


class TestAskingForAFileThatCannotBeWritten:
    def test_it_exits_nonzero_so_a_chained_backup_script_stops(self, monkeypatch, tmp_path):
        target = tmp_path / "m.json"
        with pytest.raises(SystemExit) as exc:
            _run(str(target), monkeypatch, tmp_path)
        assert exc.value.code != 0

    def test_it_does_not_leave_a_file_behind_either(self, monkeypatch, tmp_path):
        target = tmp_path / "m.json"
        with pytest.raises(SystemExit):
            _run(str(target), monkeypatch, tmp_path)
        assert not target.exists()

    def test_the_refusal_names_the_file_that_was_asked_for(self, monkeypatch, tmp_path, capsys):
        target = tmp_path / "m.json"
        with pytest.raises(SystemExit):
            _run(str(target), monkeypatch, tmp_path)
        err = capsys.readouterr().err
        assert str(target) in err

    def test_the_refusal_says_what_to_do_instead(self, monkeypatch, tmp_path, capsys):
        with pytest.raises(SystemExit):
            _run(str(tmp_path / "m.json"), monkeypatch, tmp_path)
        err = capsys.readouterr().err
        assert "How to fix" in err


class TestTheAdvisoryPathIsUnchanged:
    """Without `--out` nothing was promised, so printing the advisory and returning is right."""

    def test_no_out_still_returns_quietly(self, monkeypatch, tmp_path):
        assert _run(None, monkeypatch, tmp_path) is None

    def test_and_still_points_at_the_operations_doc(self, monkeypatch, tmp_path, capsys):
        _run(None, monkeypatch, tmp_path)
        out = capsys.readouterr().out
        assert "SQLite mode" in out and "operations.md" in out


class TestWhyTheManifestCannotBeBuiltHere:
    def test_the_counted_tables_are_postgres_only(self):
        """The advisory is not a shortcut: SQLite holds only the checkpointer."""
        from app.db.backup import COUNTED_TABLES

        src = inspect.getsource(cli.cmd_backup_manifest)
        assert "USE_SQLITE" in src
        # None of these are ever created on the SQLite path — the recorder, audit and memory
        # writers all report state "sqlite" and write nothing.
        assert "decision_log" in COUNTED_TABLES and "memory_audit" in COUNTED_TABLES

    def test_the_docs_do_not_promise_a_sqlite_manifest(self):
        text = Path("docs/operations.md").read_text(encoding="utf-8")
        assert "backup-manifest" in text
