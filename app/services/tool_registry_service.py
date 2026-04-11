# app/services/tool_registry_service.py
"""
PostgreSQL-backed tool registry service for runtime-generated tools.

Replaces the previous JSON/fcntl implementation. Uses the shared psycopg2
connection pool from PostgresCheckpointer so no extra connections are opened.

The public API is identical to the old JSON implementation so all callers
(tool_loader, code_generator_tools, API endpoints) work without changes.
"""

import json
import hashlib
from contextlib import contextmanager
from datetime import datetime
from typing import Dict, List, Optional

from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")


_SCHEMA = """
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
"""

# Column order matches the SELECT projection used in every query.
_COLS = [
    "tool_id", "name", "description", "file_path", "file_checksum",
    "function_name", "pydantic_class_name", "tool_instance_variable_name",
    "input_schema", "output_schema", "created_at", "base_app_version",
    "status", "status_reason", "created_by", "pr_url", "pr_number", "pr_status",
]

_SELECT_COLS = ", ".join(_COLS)


def _row_to_dict(row) -> Dict:
    """Convert a DB row (tuple or mapping) to a plain dict."""
    try:
        d = dict(row)           # sqlite3.Row / RealDictRow
    except (TypeError, ValueError):
        d = dict(zip(_COLS, row))   # psycopg2 default tuple
    # Deserialise JSON text columns
    for col in ("input_schema", "output_schema"):
        if isinstance(d.get(col), str):
            try:
                d[col] = json.loads(d[col])
            except (json.JSONDecodeError, TypeError):
                d[col] = {}
    # Normalise datetime → ISO string (psycopg2 returns datetime objects)
    if isinstance(d.get("created_at"), datetime):
        d["created_at"] = d["created_at"].isoformat()
    return d


class ToolRegistryService:
    """PostgreSQL-backed tool registry service."""

    def __init__(self, _pool=None):
        """
        Args:
            _pool: psycopg2-compatible connection pool (must expose
                   ``getconn()`` / ``putconn()``).  Defaults to the shared
                   PostgresCheckpointer pool.  Pass a test pool for DI.
        """
        if _pool is None:
            from app.utils.postgres_checkpointer import get_checkpointer
            _pool = get_checkpointer()._pool
        self._pool = _pool
        self._init_schema()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @contextmanager
    def _get_conn(self):
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
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(_SCHEMA)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_tool(self, metadata: Dict) -> str:
        """Register a new tool. Returns tool_id.

        Raises:
            ValueError: if a tool with the same name already exists.
        """
        if "tool_id" not in metadata:
            metadata["tool_id"] = self._generate_tool_id(
                metadata["name"], metadata.get("code", "")
            )

        tool_id = metadata["tool_id"]
        created_at = metadata.get("created_at", datetime.utcnow().isoformat())

        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tool_id FROM tool_registry WHERE name = %s",
                    (metadata["name"],),
                )
                if cur.fetchone():
                    raise ValueError(f"Tool name '{metadata['name']}' already exists")

                cur.execute(
                    f"""
                    INSERT INTO tool_registry (
                        {_SELECT_COLS}
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s
                    )
                    """,
                    (
                        tool_id,
                        metadata["name"],
                        metadata.get("description", ""),
                        metadata["file_path"],
                        metadata.get("file_checksum"),
                        metadata["function_name"],
                        metadata.get("pydantic_class_name"),
                        metadata["tool_instance_variable_name"],
                        json.dumps(metadata.get("input_schema", {})),
                        json.dumps(metadata.get("output_schema", {})),
                        created_at,
                        metadata.get("base_app_version", "unknown"),
                        metadata.get("status", "enabled"),
                        metadata.get("status_reason"),
                        metadata.get("created_by", "runtime"),
                        None,   # pr_url
                        None,   # pr_number
                        None,   # pr_status
                    ),
                )

        logger.info(f"Registered tool '{metadata['name']}' with ID {tool_id}")
        return tool_id

    def get_tool(self, tool_id: str) -> Optional[Dict]:
        """Get tool metadata by tool_id."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_SELECT_COLS} FROM tool_registry WHERE tool_id = %s",
                    (tool_id,),
                )
                row = cur.fetchone()
                return _row_to_dict(row) if row else None

    def get_tool_by_name(self, name: str) -> Optional[Dict]:
        """Get tool metadata by name."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_SELECT_COLS} FROM tool_registry WHERE name = %s",
                    (name,),
                )
                row = cur.fetchone()
                return _row_to_dict(row) if row else None

    def list_tools(self, status: Optional[str] = None) -> List[Dict]:
        """List all tools, optionally filtered by status."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                if status:
                    cur.execute(
                        f"SELECT {_SELECT_COLS} FROM tool_registry WHERE status = %s",
                        (status,),
                    )
                else:
                    cur.execute(f"SELECT {_SELECT_COLS} FROM tool_registry")
                return [_row_to_dict(r) for r in cur.fetchall()]

    def update_tool_status(self, tool_id: str, status: str, reason: Optional[str] = None):
        """Update tool status (enabled / disabled / deprecated)."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tool_id FROM tool_registry WHERE tool_id = %s",
                    (tool_id,),
                )
                if not cur.fetchone():
                    raise ValueError(f"Tool {tool_id} not found")

                cur.execute(
                    "UPDATE tool_registry SET status = %s, status_reason = %s WHERE tool_id = %s",
                    (status, reason, tool_id),
                )

        logger.info(f"Updated tool {tool_id} status to {status}")

    def update_pr_metadata(
        self,
        tool_id: str,
        pr_url: str,
        pr_number: int,
        pr_status: str = "open",
    ) -> None:
        """Persist PR metadata back into the registry entry after PR creation."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tool_id FROM tool_registry WHERE tool_id = %s",
                    (tool_id,),
                )
                if not cur.fetchone():
                    raise ValueError(f"Tool {tool_id} not found")

                cur.execute(
                    """
                    UPDATE tool_registry
                       SET pr_url = %s, pr_number = %s, pr_status = %s
                     WHERE tool_id = %s
                    """,
                    (pr_url, pr_number, pr_status, tool_id),
                )

        logger.info(
            f"Updated PR metadata for tool {tool_id}: PR #{pr_number} ({pr_status}) — {pr_url}"
        )

    def deprecate_tool(self, tool_id: str, reason: Optional[str] = None):
        """Mark a tool as deprecated."""
        self.update_tool_status(tool_id, "deprecated", reason)
        logger.info(f"Deprecated tool {tool_id}: {reason or 'no reason given'}")

    def delete_tool(self, tool_id: str):
        """Delete tool from registry (idempotent)."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM tool_registry WHERE tool_id = %s",
                    (tool_id,),
                )

        logger.info(f"Deleted tool {tool_id} from registry")

    @staticmethod
    def _generate_tool_id(name: str, code: str = "") -> str:
        """Generate a stable 16-char hex tool ID from name + code."""
        content = f"{name}:{code}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]
