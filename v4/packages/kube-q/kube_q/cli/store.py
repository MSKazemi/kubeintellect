"""
store.py — Local SQLite cache for session and message history.

DB location: ~/.kube-q/history.db
This is a best-effort local mirror; the server remains source of truth.
Persistence failures are logged as warnings and never crash the REPL.

Schema versions
───────────────
v0 → v1  Initial schema: sessions + messages tables.
v1 → v2  Token tracking: token_log table + total_*_tokens columns on sessions.
v2 → v3  FTS5 full-text search index + branch columns on sessions.
v3 → v4  Per-session kubectl context: sessions.kube_context column.
"""

import logging
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone

from kube_q.core.config import CONFIG_DIR

_logger = logging.getLogger(__name__)

DB_PATH = CONFIG_DIR / "history.db"

# ── Schema definitions ────────────────────────────────────────────────────────

_V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user       TEXT NOT NULL,
    title      TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    namespace  TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    request_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
"""

_V2_TOKEN_LOG = """
CREATE TABLE IF NOT EXISTS token_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    request_id        TEXT,
    model             TEXT,
    prompt_tokens     INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_token_log_session ON token_log(session_id);
"""

# FTS availability flag — disabled at runtime if SQLite was built without FTS5
_fts_available: bool = True


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()  # noqa: UP017


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """Apply v1 → v2 migration: add token columns + token_log table."""
    for col_sql in (
        "ALTER TABLE sessions ADD COLUMN total_prompt_tokens INTEGER DEFAULT 0",
        "ALTER TABLE sessions ADD COLUMN total_completion_tokens INTEGER DEFAULT 0",
    ):
        try:
            conn.execute(col_sql)
        except sqlite3.OperationalError:
            pass  # Column already exists (partial migration or re-run)
    conn.executescript(_V2_TOKEN_LOG)
    conn.execute("PRAGMA user_version = 2")
    conn.commit()


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    """Apply v2 → v3 migration: FTS5 search index + branch columns on sessions."""
    global _fts_available

    # Add branch columns (idempotent)
    for col_sql in (
        "ALTER TABLE sessions ADD COLUMN parent_session_id TEXT",
        "ALTER TABLE sessions ADD COLUMN branch_point INTEGER",
    ):
        try:
            conn.execute(col_sql)
        except sqlite3.OperationalError:
            pass  # Column already exists

    # FTS5 virtual table + triggers
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                content,
                content='messages',
                content_rowid='id'
            )
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS messages_fts_insert
            AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS messages_fts_delete
            BEFORE DELETE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
            END
        """)
        # Backfill existing messages into FTS index
        conn.execute(
            "INSERT INTO messages_fts(rowid, content) SELECT id, content FROM messages"
        )
        _fts_available = True
    except sqlite3.OperationalError as exc:
        _logger.warning("FTS5 unavailable — search disabled: %s", exc)
        _fts_available = False

    conn.execute("PRAGMA user_version = 3")
    conn.commit()


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    """Apply v3 → v4 migration: add kube_context column on sessions."""
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN kube_context TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists (partial migration or re-run)
    conn.execute("PRAGMA user_version = 4")
    conn.commit()


# ── Connection factory ────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    """Return a configured connection, running schema migrations as needed.

    Versions are tracked with PRAGMA user_version:
      0 → 1  Create base sessions + messages tables.
      1 → 2  Add token_log table + total_*_tokens columns on sessions.
      2 → 3  Add FTS5 index on messages + branch columns on sessions.
      3 → 4  Add kube_context column on sessions.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")

    version: int = conn.execute("PRAGMA user_version").fetchone()[0]

    if version == 0:
        conn.executescript(_V1_SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        version = 1

    if version == 1:
        _migrate_to_v2(conn)
        version = 2

    if version == 2:
        _migrate_to_v3(conn)
        version = 3

    if version == 3:
        _migrate_to_v4(conn)

    return conn


# ── Connection context manager ────────────────────────────────────────────────

@contextmanager
def _db() -> Generator[sqlite3.Connection, None, None]:
    """Open a connection and guarantee it is closed, even on exception."""
    conn = get_db()
    try:
        yield conn
    finally:
        conn.close()


# ── Session operations ────────────────────────────────────────────────────────

def upsert_session(
    session_id: str,
    user: str,
    namespace: str | None,
    kube_context: str | None = None,
) -> None:
    """INSERT OR IGNORE on session_id, then UPDATE updated_at, namespace, kube_context."""
    try:
        with _db() as conn:
            now = _now()
            conn.execute(
                "INSERT OR IGNORE INTO sessions "
                "(session_id, user, created_at, updated_at, namespace, kube_context) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, user, now, now, namespace, kube_context),
            )
            conn.execute(
                "UPDATE sessions "
                "SET updated_at = ?, namespace = ?, kube_context = ? "
                "WHERE session_id = ?",
                (now, namespace, kube_context, session_id),
            )
            conn.commit()
    except sqlite3.Error as exc:
        _logger.warning("store.upsert_session failed: %s", exc)


def load_session_meta(session_id: str) -> dict | None:
    """Return session metadata (namespace, kube_context, title, …) or None if absent."""
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT session_id, user, title, created_at, updated_at, "
                "namespace, kube_context "
                "FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error as exc:
        _logger.warning("store.load_session_meta failed: %s", exc)
        return None


def set_session_title(session_id: str, title: str) -> None:
    """Set title only if currently NULL — first user message wins."""
    try:
        with _db() as conn:
            conn.execute(
                "UPDATE sessions SET title = ? WHERE session_id = ? AND title IS NULL",
                (title, session_id),
            )
            conn.commit()
    except sqlite3.Error as exc:
        _logger.warning("store.set_session_title failed: %s", exc)


def delete_session(session_id: str) -> None:
    """Delete session row; CASCADE removes its messages and token_log entries."""
    try:
        with _db() as conn:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            conn.commit()
    except sqlite3.Error as exc:
        _logger.warning("store.delete_session failed: %s", exc)


def list_sessions(limit: int = 20) -> list[dict]:
    """Return up to *limit* sessions ordered by updated_at DESC.

    Each dict includes: session_id, title, updated_at, namespace, kube_context,
    message_count, total_prompt_tokens, total_completion_tokens, total_tokens.
    """
    try:
        with _db() as conn:
            rows = conn.execute(
                """
                SELECT s.session_id, s.title, s.updated_at, s.namespace,
                       s.kube_context,
                       COUNT(m.id) AS message_count,
                       COALESCE(s.total_prompt_tokens, 0)     AS total_prompt_tokens,
                       COALESCE(s.total_completion_tokens, 0) AS total_completion_tokens
                FROM sessions s
                LEFT JOIN messages m ON m.session_id = s.session_id
                GROUP BY s.session_id
                ORDER BY s.updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["total_tokens"] = d["total_prompt_tokens"] + d["total_completion_tokens"]
            result.append(d)
        return result
    except sqlite3.Error as exc:
        _logger.warning("store.list_sessions failed: %s", exc)
        return []


# ── Message operations ────────────────────────────────────────────────────────

def append_message(
    session_id: str, role: str, content: str, request_id: str | None
) -> None:
    """Insert a message row and bump session.updated_at in the same transaction."""
    try:
        with _db() as conn:
            now = _now()
            conn.execute(
                "INSERT INTO messages (session_id, role, content, request_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, role, content, request_id, now),
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (now, session_id),
            )
            conn.commit()
    except sqlite3.Error as exc:
        _logger.warning("store.append_message failed: %s", exc)


def load_messages(session_id: str) -> list[dict]:
    """Return [{"role": ..., "content": ...}, ...] in insertion order."""
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in rows]
    except sqlite3.Error as exc:
        _logger.warning("store.load_messages failed: %s", exc)
        return []


# ── Token operations ──────────────────────────────────────────────────────────

def log_tokens(
    session_id: str,
    request_id: str | None,
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    """Insert into token_log AND update session totals in one transaction.

    All sqlite errors are swallowed — token logging is best-effort.
    """
    try:
        with _db() as conn:
            now = _now()
            conn.execute(
                "INSERT INTO token_log "
                "(session_id, request_id, model, prompt_tokens, completion_tokens, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, request_id, model, prompt_tokens, completion_tokens, now),
            )
            conn.execute(
                "UPDATE sessions SET "
                "total_prompt_tokens     = COALESCE(total_prompt_tokens,     0) + ?, "
                "total_completion_tokens = COALESCE(total_completion_tokens, 0) + ?, "
                "updated_at = ? "
                "WHERE session_id = ?",
                (prompt_tokens, completion_tokens, now, session_id),
            )
            conn.commit()
    except sqlite3.Error as exc:
        _logger.warning("store.log_tokens failed: %s", exc)


def get_session_tokens(session_id: str) -> dict:
    """Return token totals for a session.

    Keys: total_prompt_tokens, total_completion_tokens, total_tokens, request_count.
    Returns zeros for unknown sessions.
    """
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT total_prompt_tokens, total_completion_tokens "
                "FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            count_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM token_log WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if not row:
            return {
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "total_tokens": 0,
                "request_count": 0,
            }
        p = row["total_prompt_tokens"] or 0
        c = row["total_completion_tokens"] or 0
        return {
            "total_prompt_tokens": p,
            "total_completion_tokens": c,
            "total_tokens": p + c,
            "request_count": count_row["cnt"] if count_row else 0,
        }
    except sqlite3.Error as exc:
        _logger.warning("store.get_session_tokens failed: %s", exc)
        return {
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_tokens": 0,
            "request_count": 0,
        }


def get_last_usage(session_id: str) -> dict | None:
    """Return the most recent token_log entry for this session, or None.

    Keys: model, prompt_tokens, completion_tokens, created_at.
    """
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT model, prompt_tokens, completion_tokens, created_at "
                "FROM token_log WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error as exc:
        _logger.warning("store.get_last_usage failed: %s", exc)
        return None


# ── Search ────────────────────────────────────────────────────────────────────

def search_sessions(query: str, limit: int = 20) -> list[dict]:
    """Full-text search across all message content using FTS5.

    Returns list of dicts with keys:
      session_id, title, updated_at, message_count, snippet, rank.
    Snippet uses >>> <<< markers around matched terms.
    Returns [] if FTS is unavailable or query is empty.
    """
    if not query.strip() or not _fts_available:
        return []
    try:
        with _db() as conn:
            rows = conn.execute(
                """
                SELECT s.session_id, s.title, s.updated_at,
                       (SELECT COUNT(*) FROM messages
                        WHERE session_id = s.session_id) AS message_count,
                       snippet(messages_fts, 0, '>>>', '<<<', '...', 40) AS snippet,
                       messages_fts.rank AS rank
                FROM messages_fts
                JOIN messages m ON m.id = messages_fts.rowid
                JOIN sessions s ON s.session_id = m.session_id
                WHERE messages_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()
        # Deduplicate by session_id — keep the best-ranked (first) row per session
        seen: set[str] = set()
        result = []
        for row in rows:
            d = dict(row)
            if d["session_id"] not in seen:
                seen.add(d["session_id"])
                result.append(d)
        return result
    except sqlite3.Error as exc:
        _logger.warning("store.search_sessions failed: %s", exc)
        return []


# ── Branching ─────────────────────────────────────────────────────────────────

def branch_session(
    source_session_id: str,
    new_session_id: str,
    at_message_count: int,
) -> dict:
    """Fork source_session_id into a new session at the given message count.

    Copies the session row (with new IDs and branch metadata) and the first
    at_message_count messages. Returns the new session dict.
    All errors are swallowed — branching is best-effort.
    """
    try:
        with _db() as conn:
            now = _now()

            source = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (source_session_id,)
            ).fetchone()
            if not source:
                return {}

            source = dict(source)
            title = f"Branch of: {source['title'] or '(untitled)'} (at msg {at_message_count})"

            conn.execute(
                """
                INSERT INTO sessions
                    (session_id, user, title, created_at, updated_at, namespace,
                     total_prompt_tokens, total_completion_tokens,
                     parent_session_id, branch_point)
                VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
                """,
                (new_session_id, source["user"], title, now, now,
                 source["namespace"], source_session_id, at_message_count),
            )

            # Copy messages up to at_message_count
            source_msgs = conn.execute(
                "SELECT role, content, request_id FROM messages "
                "WHERE session_id = ? ORDER BY id ASC LIMIT ?",
                (source_session_id, at_message_count),
            ).fetchall()

            for msg in source_msgs:
                conn.execute(
                    "INSERT INTO messages (session_id, role, content, request_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (new_session_id, msg["role"], msg["content"], msg["request_id"], now),
                )

            conn.commit()
            new_session = dict(conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (new_session_id,)
            ).fetchone())
        return new_session
    except sqlite3.Error as exc:
        _logger.warning("store.branch_session failed: %s", exc)
        return {}


def list_branches(session_id: str) -> list[dict]:
    """Return all sessions branched from session_id, plus siblings (sessions
    sharing the same parent), ordered by created_at DESC.

    Each dict has the same shape as list_sessions() rows.
    """
    try:
        with _db() as conn:
            # Determine parent of current session (if it's itself a branch)
            row = conn.execute(
                "SELECT parent_session_id FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            parent_id = row["parent_session_id"] if row else None

            # Collect: direct children + siblings (other children of same parent)
            if parent_id:
                rows = conn.execute(
                    """
                    SELECT s.session_id, s.title, s.updated_at, s.namespace,
                           s.parent_session_id, s.branch_point,
                           COUNT(m.id) AS message_count,
                           COALESCE(s.total_prompt_tokens, 0)     AS total_prompt_tokens,
                           COALESCE(s.total_completion_tokens, 0) AS total_completion_tokens
                    FROM sessions s
                    LEFT JOIN messages m ON m.session_id = s.session_id
                    WHERE s.parent_session_id IN (?, ?)
                       OR (s.parent_session_id IS NULL AND s.session_id IN (?, ?))
                    GROUP BY s.session_id
                    ORDER BY s.created_at DESC
                    """,
                    (session_id, parent_id, session_id, parent_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT s.session_id, s.title, s.updated_at, s.namespace,
                           s.parent_session_id, s.branch_point,
                           COUNT(m.id) AS message_count,
                           COALESCE(s.total_prompt_tokens, 0)     AS total_prompt_tokens,
                           COALESCE(s.total_completion_tokens, 0) AS total_completion_tokens
                    FROM sessions s
                    LEFT JOIN messages m ON m.session_id = s.session_id
                    WHERE s.parent_session_id = ?
                    GROUP BY s.session_id
                    ORDER BY s.created_at DESC
                    """,
                    (session_id,),
                ).fetchall()

        result = []
        for r in rows:
            d = dict(r)
            d["total_tokens"] = d["total_prompt_tokens"] + d["total_completion_tokens"]
            result.append(d)
        return result
    except sqlite3.Error as exc:
        _logger.warning("store.list_branches failed: %s", exc)
        return []


def rename_session(session_id: str, title: str) -> None:
    """Set session title unconditionally (unlike set_session_title which is NULL-only)."""
    try:
        with _db() as conn:
            conn.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE session_id = ?",
                (title, _now(), session_id),
            )
            conn.commit()
    except sqlite3.Error as exc:
        _logger.warning("store.rename_session failed: %s", exc)
