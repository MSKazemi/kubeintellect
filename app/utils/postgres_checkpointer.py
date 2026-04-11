import pickle
import json
import time
import logging
from contextlib import contextmanager
from datetime import datetime
from psycopg2 import OperationalError
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger(__name__)

_checkpointer_instance: "PostgresCheckpointer | None" = None


def get_checkpointer() -> "PostgresCheckpointer":
    """Return the application-wide PostgresCheckpointer singleton."""
    global _checkpointer_instance
    if _checkpointer_instance is None:
        from app.core.config import settings
        _checkpointer_instance = PostgresCheckpointer(
            host=settings.POSTGRES_HOST,
            dbname=settings.POSTGRES_DB,
            user=settings.POSTGRES_USER,
            password=settings.POSTGRES_PASSWORD,
            min_conn=settings.POSTGRES_POOL_MIN_CONN,
            max_conn=settings.POSTGRES_POOL_MAX_CONN,
        )
    return _checkpointer_instance


class PostgresCheckpointer:
    def __init__(self, host, dbname, user, password,
                 min_conn=1, max_conn=10,
                 max_retries=10, retry_delay=5):
        self.host = host
        self.dbname = dbname
        self.user = user
        self.password = password
        self.min_conn = min_conn
        self.max_conn = max_conn
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._pool: ThreadedConnectionPool | None = None
        self._init_pool()

    def _init_pool(self):
        """Initialize the connection pool with retry/backoff."""
        last_exception = None
        for attempt in range(self.max_retries):
            try:
                self._pool = ThreadedConnectionPool(
                    self.min_conn,
                    self.max_conn,
                    host=self.host,
                    dbname=self.dbname,
                    user=self.user,
                    password=self.password,
                    connect_timeout=5,
                )
                logger.info(
                    f"Postgres connection pool created (min={self.min_conn}, "
                    f"max={self.max_conn}) at {self.host}"
                )
                self._init_schema()
                return
            except (OperationalError, Exception) as e:
                last_exception = e
                if attempt < self.max_retries - 1:
                    delay = self.retry_delay * (2 ** attempt)
                    logger.warning(
                        f"Failed to create Postgres pool (attempt {attempt + 1}/"
                        f"{self.max_retries}): {e}. Retrying in {delay}s..."
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        f"Failed to create Postgres pool after {self.max_retries} attempts"
                    )
        raise ConnectionError(
            f"Could not create Postgres pool at {self.host} after "
            f"{self.max_retries} attempts. Last error: {last_exception}"
        ) from last_exception

    @contextmanager
    def _get_conn(self):
        """Borrow a connection from the pool and return it when done."""
        if self._pool is None:
            raise ConnectionError("Postgres connection pool is not initialized")
        conn = self._pool.getconn()
        try:
            conn.autocommit = True
            yield conn
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            self._pool.putconn(conn)

    def _init_schema(self):
        """Create required tables if they don't exist."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS workflow_checkpoints (
                        user_id TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        config JSONB,
                        state BYTEA,
                        updated_at TIMESTAMP,
                        PRIMARY KEY (user_id, thread_id)
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS audit_log (
                        id               SERIAL PRIMARY KEY,
                        user_id          TEXT,
                        conversation_id  TEXT,
                        query            TEXT NOT NULL,
                        outcome          TEXT NOT NULL,
                        agents_invoked   TEXT[],
                        latency_ms       INTEGER,
                        action_id        TEXT,
                        decision         TEXT,
                        timestamp        TIMESTAMP NOT NULL DEFAULT NOW()
                    );
                    CREATE INDEX IF NOT EXISTS idx_audit_log_user_id
                        ON audit_log (user_id);
                    CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp
                        ON audit_log (timestamp);
                """)
                # Migrate existing audit_log tables that predate action_id/decision columns
                cur.execute("""
                    ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS action_id TEXT;
                    ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS decision  TEXT;
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS tool_registry (
                        tool_id                     TEXT PRIMARY KEY,
                        name                        TEXT UNIQUE NOT NULL,
                        description                 TEXT    DEFAULT '',
                        file_path                   TEXT    NOT NULL,
                        file_checksum               TEXT,
                        function_name               TEXT    NOT NULL,
                        pydantic_class_name         TEXT,
                        tool_instance_variable_name TEXT    NOT NULL,
                        input_schema                TEXT    DEFAULT '{}',
                        output_schema               TEXT    DEFAULT '{}',
                        created_at                  TIMESTAMP NOT NULL DEFAULT NOW(),
                        base_app_version            TEXT    DEFAULT 'unknown',
                        status                      TEXT    NOT NULL DEFAULT 'enabled',
                        status_reason               TEXT,
                        created_by                  TEXT    DEFAULT 'runtime',
                        pr_url                      TEXT,
                        pr_number                   INTEGER,
                        pr_status                   TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_tool_registry_status
                        ON tool_registry (status);
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS conversation_summary_cache (
                        conversation_id  TEXT    NOT NULL,
                        message_count    INTEGER NOT NULL,
                        summary_text     TEXT    NOT NULL,
                        created_at       TIMESTAMP NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (conversation_id, message_count)
                    );
                """)
        logger.info("Postgres schema initialized (workflow_checkpoints + tool_registry + audit_log + conversation_summary_cache ready)")

    def write_audit_log(
        self,
        query: str,
        outcome: str,
        user_id: str = None,
        conversation_id: str = None,
        agents_invoked: list = None,
        latency_ms: int = None,
        action_id: str = None,
        decision: str = None,
    ) -> None:
        """
        Insert one row into audit_log.

        Failures are swallowed (non-critical) so the caller's response is never blocked.
        """
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO audit_log
                            (user_id, conversation_id, query, outcome, agents_invoked, latency_ms, action_id, decision)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            user_id,
                            conversation_id,
                            query[:2000],  # cap to avoid huge rows
                            outcome,
                            agents_invoked or [],
                            latency_ms,
                            action_id,
                            decision,
                        ),
                    )
        except Exception as e:
            logger.warning(f"audit_log write failed (non-critical): {e}")

    def read_summary_cache(self, conversation_id: str, message_count: int) -> str | None:
        """Return cached summary text for (conversation_id, message_count), or None if not found."""
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT summary_text FROM conversation_summary_cache "
                        "WHERE conversation_id = %s AND message_count = %s",
                        (conversation_id, message_count),
                    )
                    row = cur.fetchone()
                    return row[0] if row else None
        except Exception as e:
            logger.debug(f"summary cache read failed (non-critical): {e}")
            return None

    def write_summary_cache(self, conversation_id: str, message_count: int, summary_text: str) -> None:
        """Upsert a summary cache entry. Failures are non-critical — swallowed silently."""
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO conversation_summary_cache (conversation_id, message_count, summary_text)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (conversation_id, message_count) DO UPDATE SET
                            summary_text = EXCLUDED.summary_text,
                            created_at = NOW()
                        """,
                        (conversation_id, message_count, summary_text),
                    )
        except Exception as e:
            logger.debug(f"summary cache write failed (non-critical): {e}")

    def save_checkpoint(self, user_id, thread_id, config, state):
        # Strip runtime-only keys (callbacks contain non-JSON-serializable objects
        # like LangchainCallbackHandler). They are re-attached on resume at the
        # call site (workflow.py) and must not be persisted.
        serializable_config = {k: v for k, v in config.items() if k != "callbacks"}
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO workflow_checkpoints (user_id, thread_id, config, state, updated_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, thread_id) DO UPDATE SET
                        config = EXCLUDED.config,
                        state = EXCLUDED.state,
                        updated_at = EXCLUDED.updated_at;
                """, (user_id, thread_id, json.dumps(serializable_config), pickle.dumps(state), datetime.utcnow()))

    def load_checkpoint(self, user_id, thread_id):
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT config, state FROM workflow_checkpoints "
                    "WHERE user_id = %s AND thread_id = %s",
                    (user_id, thread_id)
                )
                row = cur.fetchone()
                if row:
                    config_json, state_bytes = row
                    if isinstance(config_json, (str, bytes, bytearray)):
                        config = json.loads(config_json)
                    elif isinstance(config_json, dict):
                        config = config_json
                    else:
                        config = None
                    return config, pickle.loads(state_bytes)
                return None

    def delete_checkpoint(self, user_id, thread_id):
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM workflow_checkpoints WHERE user_id = %s AND thread_id = %s",
                    (user_id, thread_id)
                )

    def close(self):
        """Close all connections in the pool."""
        if self._pool:
            self._pool.closeall()
            self._pool = None
            logger.info("Postgres connection pool closed")
