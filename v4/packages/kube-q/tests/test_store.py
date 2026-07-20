"""
Unit tests for kube_q/store.py.

Covers: upsert_session (idempotency), append_message + load_messages roundtrip,
        set_session_title (NULL-only semantics), delete_session (cascade),
        list_sessions (ordering + limit), sqlite3 error swallowing,
        schema migration v1→v2, log_tokens, get_session_tokens, get_last_usage.
"""

import sqlite3
import time
from pathlib import Path

import pytest

import kube_q.cli.store as store_mod
from kube_q.cli.store import (
    append_message,
    branch_session,
    delete_session,
    get_last_usage,
    get_session_tokens,
    list_branches,
    list_sessions,
    load_messages,
    log_tokens,
    rename_session,
    search_sessions,
    set_session_title,
    upsert_session,
)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect every test to a fresh, temporary SQLite database."""
    monkeypatch.setattr(store_mod, "DB_PATH", tmp_path / "test.db")


# ── upsert_session ────────────────────────────────────────────────────────────


def test_upsert_session_creates_row() -> None:
    upsert_session("sess-1", "user-a", "default")
    sessions = list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "sess-1"
    assert sessions[0]["namespace"] == "default"


def test_upsert_session_is_idempotent() -> None:
    upsert_session("sess-1", "user-a", "default")
    upsert_session("sess-1", "user-a", "kube-system")
    sessions = list_sessions()
    # Still only one row
    assert len(sessions) == 1
    # Namespace updated to latest call
    assert sessions[0]["namespace"] == "kube-system"


def test_upsert_session_does_not_duplicate_on_repeat() -> None:
    for _ in range(5):
        upsert_session("sess-dup", "user-b", None)
    assert len(list_sessions()) == 1


# ── append_message + load_messages ───────────────────────────────────────────


def test_append_and_load_messages_roundtrip() -> None:
    upsert_session("sess-2", "user-a", None)
    append_message("sess-2", "user", "Hello!", "req-1")
    append_message("sess-2", "assistant", "Hi there!", "req-1")
    msgs = load_messages("sess-2")
    assert len(msgs) == 2
    assert msgs[0] == {"role": "user", "content": "Hello!"}
    assert msgs[1] == {"role": "assistant", "content": "Hi there!"}


def test_load_messages_preserves_insertion_order() -> None:
    upsert_session("sess-order", "user-a", None)
    for i in range(6):
        role = "user" if i % 2 == 0 else "assistant"
        append_message("sess-order", role, f"msg-{i}", None)
    msgs = load_messages("sess-order")
    assert [m["content"] for m in msgs] == [f"msg-{i}" for i in range(6)]


def test_load_messages_empty_session() -> None:
    assert load_messages("nonexistent-session") == []


# ── set_session_title ─────────────────────────────────────────────────────────


def test_set_session_title_sets_on_null() -> None:
    upsert_session("sess-3", "user-a", None)
    set_session_title("sess-3", "My first query")
    sessions = list_sessions()
    assert sessions[0]["title"] == "My first query"


def test_set_session_title_noop_if_already_set() -> None:
    upsert_session("sess-4", "user-a", None)
    set_session_title("sess-4", "First title")
    set_session_title("sess-4", "Should be ignored")
    sessions = list_sessions()
    assert sessions[0]["title"] == "First title"


def test_set_session_title_second_call_is_noop() -> None:
    upsert_session("sess-5", "user-a", None)
    set_session_title("sess-5", "keep this")
    for _ in range(3):
        set_session_title("sess-5", "try to overwrite")
    assert list_sessions()[0]["title"] == "keep this"


# ── delete_session ────────────────────────────────────────────────────────────


def test_delete_session_removes_session_row() -> None:
    upsert_session("sess-del", "user-a", None)
    delete_session("sess-del")
    assert list_sessions() == []


def test_delete_session_cascades_to_messages() -> None:
    upsert_session("sess-cascade", "user-a", None)
    append_message("sess-cascade", "user", "hello", None)
    append_message("sess-cascade", "assistant", "world", None)
    delete_session("sess-cascade")
    # Messages must also be gone
    assert load_messages("sess-cascade") == []


def test_delete_nonexistent_session_is_safe() -> None:
    # Should not raise
    delete_session("does-not-exist")


# ── list_sessions ─────────────────────────────────────────────────────────────


def test_list_sessions_ordered_by_updated_at_desc() -> None:
    for i in range(4):
        sid = f"sess-{i}"
        upsert_session(sid, "user-a", None)
        append_message(sid, "user", f"msg {i}", None)
        # Small sleep to ensure distinct updated_at timestamps
        time.sleep(0.01)

    sessions = list_sessions()
    ids = [s["session_id"] for s in sessions]
    # Most recently updated first
    assert ids == ["sess-3", "sess-2", "sess-1", "sess-0"]


def test_list_sessions_respects_limit() -> None:
    for i in range(10):
        upsert_session(f"limit-{i}", "user-a", None)

    sessions = list_sessions(limit=3)
    assert len(sessions) == 3


def test_list_sessions_message_count() -> None:
    upsert_session("count-sess", "user-a", None)
    for _ in range(5):
        append_message("count-sess", "user", "x", None)

    sessions = list_sessions()
    assert sessions[0]["message_count"] == 5


def test_list_sessions_empty() -> None:
    assert list_sessions() == []


# ── Error swallowing ──────────────────────────────────────────────────────────


def test_append_message_swallows_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """sqlite3 errors must be logged but must NOT propagate to the caller."""
    def _bad_get_db() -> None:
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(store_mod, "get_db", _bad_get_db)
    # Must not raise
    append_message("any", "user", "content", None)


def test_upsert_session_swallows_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _bad_get_db() -> None:
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(store_mod, "get_db", _bad_get_db)
    upsert_session("any", "user", None)  # must not raise


def test_load_messages_returns_empty_on_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _bad_get_db() -> None:
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(store_mod, "get_db", _bad_get_db)
    result = load_messages("any")
    assert result == []


def test_list_sessions_returns_empty_on_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _bad_get_db() -> None:
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(store_mod, "get_db", _bad_get_db)
    result = list_sessions()
    assert result == []


def test_set_session_title_swallows_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _bad_get_db() -> None:
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(store_mod, "get_db", _bad_get_db)
    set_session_title("any", "title")  # must not raise


def test_delete_session_swallows_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _bad_get_db() -> None:
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(store_mod, "get_db", _bad_get_db)
    delete_session("any")  # must not raise


# ── Schema migration v1 → v2 ──────────────────────────────────────────────────


def _make_v1_db(path: Path) -> None:
    """Create a minimal v1-format database (user_version=1, no token columns)."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            user       TEXT NOT NULL,
            title      TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            namespace  TEXT
        );
        CREATE TABLE messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            request_id TEXT,
            created_at TEXT NOT NULL
        );
    """)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()


def test_migration_v1_to_v2_creates_token_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "migration_test.db"
    _make_v1_db(db_path)
    monkeypatch.setattr(store_mod, "DB_PATH", db_path)

    conn = store_mod.get_db()
    # token_log table must exist
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    tables = {row[0] for row in rows}
    assert "token_log" in tables
    # user_version must be 4 (v1→v2→v3→v4 all run in sequence)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 4
    conn.close()


def test_migration_v1_to_v2_adds_token_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "migration_test2.db"
    _make_v1_db(db_path)
    monkeypatch.setattr(store_mod, "DB_PATH", db_path)

    conn = store_mod.get_db()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    assert "total_prompt_tokens" in cols
    assert "total_completion_tokens" in cols
    conn.close()


def test_migration_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling get_db() multiple times on a fully migrated database must not fail."""
    db_path = tmp_path / "idempotent.db"
    monkeypatch.setattr(store_mod, "DB_PATH", db_path)
    store_mod.get_db().close()  # first call: v0 → v4
    store_mod.get_db().close()  # second call: already v4, no-op
    conn = store_mod.get_db()   # third call
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 4
    conn.close()


# ── log_tokens + get_session_tokens ──────────────────────────────────────────


def test_log_tokens_and_get_session_tokens_roundtrip() -> None:
    upsert_session("sess-tok", "user-a", None)
    log_tokens("sess-tok", "req-1", "gpt-4o", 120, 340)

    tok = get_session_tokens("sess-tok")
    assert tok["total_prompt_tokens"] == 120
    assert tok["total_completion_tokens"] == 340
    assert tok["total_tokens"] == 460
    assert tok["request_count"] == 1


def test_log_tokens_accumulates_across_requests() -> None:
    upsert_session("sess-acc", "user-a", None)
    log_tokens("sess-acc", "req-1", "gpt-4o", 100, 200)
    log_tokens("sess-acc", "req-2", "gpt-4o", 50, 150)

    tok = get_session_tokens("sess-acc")
    assert tok["total_prompt_tokens"] == 150
    assert tok["total_completion_tokens"] == 350
    assert tok["total_tokens"] == 500
    assert tok["request_count"] == 2


def test_get_session_tokens_returns_zeros_for_unknown_session() -> None:
    tok = get_session_tokens("does-not-exist")
    assert tok["total_prompt_tokens"] == 0
    assert tok["total_completion_tokens"] == 0
    assert tok["total_tokens"] == 0
    assert tok["request_count"] == 0


def test_get_session_tokens_returns_zeros_before_any_logging() -> None:
    upsert_session("sess-no-tok", "user-a", None)
    tok = get_session_tokens("sess-no-tok")
    assert tok["total_tokens"] == 0
    assert tok["request_count"] == 0


def test_log_tokens_swallows_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _bad_get_db() -> None:
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(store_mod, "get_db", _bad_get_db)
    log_tokens("any", "req", "model", 100, 200)  # must not raise


# ── get_last_usage ─────────────────────────────────────────────────────────────


def test_get_last_usage_returns_none_when_no_entries() -> None:
    upsert_session("sess-empty", "user-a", None)
    assert get_last_usage("sess-empty") is None


def test_get_last_usage_returns_most_recent_entry() -> None:
    upsert_session("sess-lu", "user-a", None)
    log_tokens("sess-lu", "req-1", "model-a", 10, 20)
    log_tokens("sess-lu", "req-2", "model-b", 30, 40)

    last = get_last_usage("sess-lu")
    assert last is not None
    assert last["model"] == "model-b"
    assert last["prompt_tokens"] == 30
    assert last["completion_tokens"] == 40


def test_get_last_usage_swallows_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _bad_get_db() -> None:
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(store_mod, "get_db", _bad_get_db)
    result = get_last_usage("any")
    assert result is None


# ── list_sessions includes token counts ───────────────────────────────────────


def test_list_sessions_includes_token_counts() -> None:
    upsert_session("sess-with-tok", "user-a", None)
    log_tokens("sess-with-tok", "req-1", "gpt-4o", 100, 200)

    sessions = list_sessions()
    s = next(x for x in sessions if x["session_id"] == "sess-with-tok")
    assert s["total_prompt_tokens"] == 100
    assert s["total_completion_tokens"] == 200
    assert s["total_tokens"] == 300


def test_list_sessions_token_columns_default_to_zero() -> None:
    upsert_session("sess-no-tok-2", "user-a", None)
    sessions = list_sessions()
    s = next(x for x in sessions if x["session_id"] == "sess-no-tok-2")
    assert s["total_tokens"] == 0


# ── token_log cascade delete ──────────────────────────────────────────────────


def test_delete_session_cascades_to_token_log() -> None:
    upsert_session("sess-cascade-tok", "user-a", None)
    append_message("sess-cascade-tok", "user", "hello", None)
    log_tokens("sess-cascade-tok", "req-1", "gpt-4o", 10, 20)
    delete_session("sess-cascade-tok")

    # token_log entries must be gone
    conn = store_mod.get_db()
    count = conn.execute(
        "SELECT COUNT(*) FROM token_log WHERE session_id = ?",
        ("sess-cascade-tok",),
    ).fetchone()[0]
    conn.close()
    assert count == 0


# ── Schema migration v2 → v3 (FTS + branches) ────────────────────────────────


def _make_v2_db(path: Path) -> None:
    """Create a v2-format database (user_version=2, token columns, no FTS/branch)."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            user       TEXT NOT NULL,
            title      TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            namespace  TEXT,
            total_prompt_tokens     INTEGER DEFAULT 0,
            total_completion_tokens INTEGER DEFAULT 0
        );
        CREATE TABLE messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            request_id TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE token_log (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id        TEXT NOT NULL,
            request_id        TEXT,
            model             TEXT,
            prompt_tokens     INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL,
            created_at        TEXT NOT NULL
        );
    """)
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()


def test_migration_v2_to_v3_adds_fts_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "v2_to_v3.db"
    _make_v2_db(db_path)
    monkeypatch.setattr(store_mod, "DB_PATH", db_path)

    conn = store_mod.get_db()
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    tables = {row[0] for row in rows}
    assert "messages_fts" in tables
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 4
    conn.close()


def test_migration_v2_to_v3_adds_branch_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "v2_branch_cols.db"
    _make_v2_db(db_path)
    monkeypatch.setattr(store_mod, "DB_PATH", db_path)

    conn = store_mod.get_db()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    assert "parent_session_id" in cols
    assert "branch_point" in cols
    conn.close()


def test_migration_v2_to_v3_backfills_fts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Messages present before FTS migration must be searchable afterwards."""
    db_path = tmp_path / "v2_backfill.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            user TEXT NOT NULL,
            title TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            namespace TEXT,
            total_prompt_tokens INTEGER DEFAULT 0,
            total_completion_tokens INTEGER DEFAULT 0
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            request_id TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE token_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            request_id TEXT,
            model TEXT,
            prompt_tokens INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO sessions VALUES ('s1','u','My session','2026-01-01','2026-01-01',NULL,0,0)"
    )
    for i in range(5):
        conn.execute(
            "INSERT INTO messages (session_id, role, content, request_id, created_at) "
            "VALUES ('s1', 'user', ?, NULL, '2026-01-01')",
            (f"pod crash number {i}",),
        )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    monkeypatch.setattr(store_mod, "DB_PATH", db_path)
    store_mod.get_db().close()  # trigger migration

    results = search_sessions("crash")
    session_ids = {r["session_id"] for r in results}
    assert session_ids == {"s1"}


# ── Schema migration v3 → v4 (kube_context column) ────────────────────────────


def _make_v3_db(path: Path) -> None:
    """Create a v3-format database (user_version=3, no kube_context column)."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            user       TEXT NOT NULL,
            title      TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            namespace  TEXT,
            total_prompt_tokens     INTEGER DEFAULT 0,
            total_completion_tokens INTEGER DEFAULT 0,
            parent_session_id TEXT,
            branch_point INTEGER
        );
        CREATE TABLE messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            request_id TEXT,
            created_at TEXT NOT NULL
        );
    """)
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()


def test_migration_v3_to_v4_adds_kube_context_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "v3_to_v4.db"
    _make_v3_db(db_path)
    monkeypatch.setattr(store_mod, "DB_PATH", db_path)

    conn = store_mod.get_db()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    assert "kube_context" in cols
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 4
    conn.close()


def test_migration_v3_to_v4_preserves_existing_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v3 sessions must survive the migration with kube_context defaulting to NULL."""
    db_path = tmp_path / "v3_preserve.db"
    _make_v3_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO sessions VALUES "
        "('s-old', 'u', 'Old session', '2026-01-01', '2026-01-01', 'default', 0, 0, NULL, NULL)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(store_mod, "DB_PATH", db_path)

    meta = store_mod.load_session_meta("s-old")
    assert meta is not None
    assert meta["title"] == "Old session"
    assert meta["kube_context"] is None


# ── kube_context roundtrip ────────────────────────────────────────────────────


def test_upsert_session_persists_kube_context() -> None:
    upsert_session("sess-ctx", "u", "default", kube_context="prod-cluster")
    meta = store_mod.load_session_meta("sess-ctx")
    assert meta is not None
    assert meta["kube_context"] == "prod-cluster"


def test_upsert_session_updates_kube_context() -> None:
    upsert_session("sess-ctx", "u", "default", kube_context="prod-cluster")
    upsert_session("sess-ctx", "u", "default", kube_context="staging")
    assert store_mod.load_session_meta("sess-ctx")["kube_context"] == "staging"


def test_upsert_session_clears_kube_context_when_none() -> None:
    upsert_session("sess-ctx", "u", "default", kube_context="prod-cluster")
    upsert_session("sess-ctx", "u", "default", kube_context=None)
    assert store_mod.load_session_meta("sess-ctx")["kube_context"] is None


def test_upsert_session_defaults_kube_context_to_none() -> None:
    upsert_session("sess-no-ctx", "u", "default")
    assert store_mod.load_session_meta("sess-no-ctx")["kube_context"] is None


def test_list_sessions_includes_kube_context() -> None:
    upsert_session("s-prod", "u", None, kube_context="prod")
    upsert_session("s-dev",  "u", None, kube_context="dev")
    rows = {r["session_id"]: r["kube_context"] for r in list_sessions()}
    assert rows["s-prod"] == "prod"
    assert rows["s-dev"] == "dev"


def test_load_session_meta_returns_none_for_missing_id() -> None:
    assert store_mod.load_session_meta("no-such-session") is None


# ── search_sessions ───────────────────────────────────────────────────────────


def test_search_sessions_finds_matching_session() -> None:
    upsert_session("s-alpha", "u", None)
    upsert_session("s-beta",  "u", None)
    upsert_session("s-gamma", "u", None)
    append_message("s-alpha", "user", "pod crash in production", None)
    append_message("s-beta",  "user", "scaling the deployment", None)
    append_message("s-gamma", "user", "network policy question", None)

    results = search_sessions("crash")
    assert len(results) == 1
    assert results[0]["session_id"] == "s-alpha"


def test_search_sessions_no_match_returns_empty() -> None:
    upsert_session("s-only", "u", None)
    append_message("s-only", "user", "hello world", None)
    assert search_sessions("foobar_xyz_notaword") == []


def test_search_sessions_empty_db_returns_empty() -> None:
    assert search_sessions("anything") == []


def test_search_sessions_fts_boolean() -> None:
    upsert_session("s-bool", "u", None)
    append_message("s-bool", "user", "pods are crashing in staging", None)
    upsert_session("s-prod", "u", None)
    append_message("s-prod", "user", "pods are crashing in production", None)

    # FTS5 boolean: "pods AND production"
    results = search_sessions("pods AND production")
    ids = {r["session_id"] for r in results}
    assert "s-prod" in ids
    assert "s-bool" not in ids


def test_search_sessions_snippet_has_markers() -> None:
    upsert_session("s-snip", "u", None)
    append_message("s-snip", "user", "the deployment timed out during rollout", None)

    results = search_sessions("timeout OR timed")
    assert len(results) == 1
    snip = results[0]["snippet"]
    assert ">>>" in snip or "<<<" in snip or "timed" in snip


def test_search_sessions_finds_branches_too() -> None:
    """Branch sessions are regular sessions — search must find their messages."""
    upsert_session("s-orig-search", "u", None)
    for i in range(4):
        append_message("s-orig-search", "user", f"message {i}", None)
    branch_session("s-orig-search", "s-branch-search", 4)
    append_message("s-branch-search", "user", "unique branch content xyzzy", None)

    results = search_sessions("xyzzy")
    assert len(results) == 1
    assert results[0]["session_id"] == "s-branch-search"


# ── branch_session ────────────────────────────────────────────────────────────


def _make_session_with_messages(sid: str, n: int) -> None:
    upsert_session(sid, "user-a", None)
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        append_message(sid, role, f"message-{i}", f"req-{i}")


def test_branch_session_copies_correct_message_count() -> None:
    _make_session_with_messages("orig-10", 10)
    branch_session("orig-10", "branch-6", 6)

    assert len(load_messages("branch-6")) == 6
    assert len(load_messages("orig-10")) == 10


def test_branch_session_original_unchanged() -> None:
    _make_session_with_messages("orig-intact", 8)
    branch_session("orig-intact", "branch-intact", 4)

    msgs = load_messages("orig-intact")
    assert len(msgs) == 8
    assert msgs[7]["content"] == "message-7"


def test_branch_session_sets_parent_and_branch_point() -> None:
    _make_session_with_messages("orig-meta", 6)
    branch_session("orig-meta", "branch-meta", 3)

    conn = store_mod.get_db()
    row = conn.execute(
        "SELECT parent_session_id, branch_point FROM sessions WHERE session_id = ?",
        ("branch-meta",),
    ).fetchone()
    conn.close()
    assert row["parent_session_id"] == "orig-meta"
    assert row["branch_point"] == 3


def test_branch_session_message_content_matches() -> None:
    _make_session_with_messages("orig-content", 6)
    branch_session("orig-content", "branch-content", 6)

    orig_msgs = load_messages("orig-content")
    branch_msgs = load_messages("branch-content")
    for o, b in zip(orig_msgs, branch_msgs):
        assert o["role"] == b["role"]
        assert o["content"] == b["content"]


def test_branch_session_messages_have_different_ids() -> None:
    _make_session_with_messages("orig-ids", 4)
    branch_session("orig-ids", "branch-ids", 4)

    conn = store_mod.get_db()
    orig_ids = {r[0] for r in conn.execute(
        "SELECT id FROM messages WHERE session_id = ?", ("orig-ids",)
    ).fetchall()}
    branch_ids = {r[0] for r in conn.execute(
        "SELECT id FROM messages WHERE session_id = ?", ("branch-ids",)
    ).fetchall()}
    conn.close()
    assert orig_ids.isdisjoint(branch_ids)


def test_branch_at_zero_creates_empty_branch() -> None:
    _make_session_with_messages("orig-zero", 5)
    branch_session("orig-zero", "branch-zero", 0)
    assert load_messages("branch-zero") == []


def test_branch_of_branch_points_to_immediate_parent() -> None:
    _make_session_with_messages("root", 8)
    branch_session("root", "child", 4)
    branch_session("child", "grandchild", 2)

    conn = store_mod.get_db()
    row = conn.execute(
        "SELECT parent_session_id FROM sessions WHERE session_id = ?",
        ("grandchild",),
    ).fetchone()
    conn.close()
    assert row["parent_session_id"] == "child"


def test_delete_branch_does_not_affect_original() -> None:
    _make_session_with_messages("orig-del", 6)
    branch_session("orig-del", "branch-del", 3)
    delete_session("branch-del")

    assert len(load_messages("orig-del")) == 6
    assert load_messages("branch-del") == []


# ── list_branches ─────────────────────────────────────────────────────────────


def test_list_branches_returns_direct_children() -> None:
    _make_session_with_messages("orig-lb", 6)
    branch_session("orig-lb", "branch-lb-1", 3)
    branch_session("orig-lb", "branch-lb-2", 5)

    branches = list_branches("orig-lb")
    ids = {b["session_id"] for b in branches}
    assert "branch-lb-1" in ids
    assert "branch-lb-2" in ids


def test_list_branches_empty_when_no_branches() -> None:
    _make_session_with_messages("orig-no-branch", 4)
    assert list_branches("orig-no-branch") == []


# ── rename_session ────────────────────────────────────────────────────────────


def test_rename_session_updates_title() -> None:
    upsert_session("rename-me", "u", None)
    set_session_title("rename-me", "original title")
    rename_session("rename-me", "new title")
    sessions = list_sessions()
    s = next(x for x in sessions if x["session_id"] == "rename-me")
    assert s["title"] == "new title"


def test_rename_session_can_overwrite_existing_title() -> None:
    """rename_session always overwrites, unlike set_session_title."""
    upsert_session("rename-force", "u", None)
    set_session_title("rename-force", "first")
    rename_session("rename-force", "second")
    rename_session("rename-force", "third")
    sessions = list_sessions()
    s = next(x for x in sessions if x["session_id"] == "rename-force")
    assert s["title"] == "third"
