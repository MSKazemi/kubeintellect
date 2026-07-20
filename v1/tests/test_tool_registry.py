"""
Unit tests for app.services.tool_registry_service.ToolRegistryService.

Uses an in-memory SQLite database via a thin pool adapter so no real Postgres
is needed.  The adapter translates psycopg2 paramstyle (%s → ?) and the
NOW() default (→ CURRENT_TIMESTAMP) so the exact same SQL runs in both
engines.
"""
import sqlite3

import pytest
from app.services.tool_registry_service import ToolRegistryService


# ---------------------------------------------------------------------------
# SQLite pool adapter (DI shim for tests)
# ---------------------------------------------------------------------------

class _Cursor:
    """Wraps a sqlite3 cursor to look like a psycopg2 cursor."""

    def __init__(self, cur):
        self._c = cur

    def execute(self, sql, params=None):
        sql = (
            sql
            .replace("%s", "?")
            .replace("DEFAULT NOW()", "DEFAULT CURRENT_TIMESTAMP")
        )
        self._c.execute(sql, params or ())

    def fetchone(self):
        return self._c.fetchone()   # sqlite3.Row (dict-like)

    def fetchall(self):
        return self._c.fetchall()   # list of sqlite3.Row

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class _Conn:
    """Wraps a sqlite3 connection to look like a psycopg2 connection."""

    def __init__(self, conn):
        self._conn = conn

    @property
    def autocommit(self):
        return self._conn.isolation_level is None

    @autocommit.setter
    def autocommit(self, val):
        self._conn.isolation_level = None if val else ""

    def cursor(self):
        return _Cursor(self._conn.cursor())

    def rollback(self):
        try:
            self._conn.rollback()
        except Exception:
            pass


class _SqlitePool:
    """Minimal pool shim backed by a single in-memory SQLite connection."""

    def __init__(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None   # autocommit
        self._conn = _Conn(conn)

    def getconn(self):
        return self._conn

    def putconn(self, conn):
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def registry():
    return ToolRegistryService(_pool=_SqlitePool())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _meta(name="my_tool", **overrides):
    """Return the minimum valid metadata dict accepted by register_tool."""
    base = {
        "name": name,
        "description": "Does something useful.",
        "file_path": "/mnt/runtime-tools/tools/gen_abc123.py",
        "function_name": "my_tool_func",
        "tool_instance_variable_name": "my_tool_instance",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# register_tool
# ---------------------------------------------------------------------------

def test_register_returns_16_char_hex_id(registry):
    tool_id = registry.register_tool(_meta())
    assert isinstance(tool_id, str)
    assert len(tool_id) == 16


def test_registered_tool_is_retrievable(registry):
    tool_id = registry.register_tool(_meta(name="alpha"))
    tool = registry.get_tool(tool_id)
    assert tool is not None
    assert tool["name"] == "alpha"


def test_register_stores_all_required_fields(registry):
    tool_id = registry.register_tool(_meta(name="beta", description="Desc."))
    tool = registry.get_tool(tool_id)
    assert tool["description"] == "Desc."
    assert tool["function_name"] == "my_tool_func"
    assert tool["status"] == "enabled"


def test_duplicate_name_raises_value_error(registry):
    registry.register_tool(_meta(name="dup"))
    with pytest.raises(ValueError, match="already exists"):
        registry.register_tool(_meta(name="dup"))


def test_different_names_do_not_conflict(registry):
    registry.register_tool(_meta(name="tool_a"))
    registry.register_tool(_meta(name="tool_b"))
    assert len(registry.list_tools()) == 2


def test_explicit_tool_id_is_respected(registry):
    registry.register_tool(_meta(tool_id="custom_id_1234"))
    tool = registry.get_tool("custom_id_1234")
    assert tool is not None


# ---------------------------------------------------------------------------
# get_tool / get_tool_by_name
# ---------------------------------------------------------------------------

def test_get_tool_unknown_id_returns_none(registry):
    assert registry.get_tool("no_such_id") is None


def test_get_tool_by_name(registry):
    registry.register_tool(_meta(name="find_me"))
    tool = registry.get_tool_by_name("find_me")
    assert tool is not None
    assert tool["name"] == "find_me"


def test_get_tool_by_name_missing_returns_none(registry):
    assert registry.get_tool_by_name("ghost") is None


# ---------------------------------------------------------------------------
# list_tools
# ---------------------------------------------------------------------------

def test_list_empty_registry(registry):
    assert registry.list_tools() == []


def test_list_returns_all_tools(registry):
    registry.register_tool(_meta(name="t1"))
    registry.register_tool(_meta(name="t2"))
    registry.register_tool(_meta(name="t3"))
    assert len(registry.list_tools()) == 3


def test_list_filters_by_status(registry):
    registry.register_tool(_meta(name="active", status="enabled"))
    registry.register_tool(_meta(name="inactive", status="disabled"))

    enabled = registry.list_tools(status="enabled")
    disabled = registry.list_tools(status="disabled")

    assert len(enabled) == 1 and enabled[0]["name"] == "active"
    assert len(disabled) == 1 and disabled[0]["name"] == "inactive"


def test_list_no_filter_returns_all_statuses(registry):
    registry.register_tool(_meta(name="e", status="enabled"))
    registry.register_tool(_meta(name="d", status="disabled"))
    assert len(registry.list_tools()) == 2


# ---------------------------------------------------------------------------
# update_tool_status
# ---------------------------------------------------------------------------

def test_update_status_changes_value(registry):
    tool_id = registry.register_tool(_meta())
    registry.update_tool_status(tool_id, "disabled", reason="maintenance")
    tool = registry.get_tool(tool_id)
    assert tool["status"] == "disabled"
    assert tool["status_reason"] == "maintenance"


def test_update_status_unknown_tool_raises(registry):
    with pytest.raises(ValueError, match="not found"):
        registry.update_tool_status("bad_id", "disabled")


def test_update_status_reason_can_be_none(registry):
    tool_id = registry.register_tool(_meta())
    registry.update_tool_status(tool_id, "disabled")
    tool = registry.get_tool(tool_id)
    assert tool["status_reason"] is None


# ---------------------------------------------------------------------------
# deprecate_tool
# ---------------------------------------------------------------------------

def test_deprecate_sets_deprecated_status(registry):
    tool_id = registry.register_tool(_meta(name="old"))
    registry.deprecate_tool(tool_id, reason="replaced by new_tool")
    tool = registry.get_tool(tool_id)
    assert tool["status"] == "deprecated"
    assert "replaced" in tool["status_reason"]


def test_deprecated_tool_appears_in_full_list(registry):
    tool_id = registry.register_tool(_meta(name="legacy"))
    registry.deprecate_tool(tool_id)
    assert len(registry.list_tools()) == 1


def test_deprecated_tool_filterable_by_status(registry):
    tool_id = registry.register_tool(_meta(name="legacy"))
    registry.deprecate_tool(tool_id)
    assert len(registry.list_tools(status="deprecated")) == 1
    assert len(registry.list_tools(status="enabled")) == 0


# ---------------------------------------------------------------------------
# delete_tool
# ---------------------------------------------------------------------------

def test_delete_removes_tool(registry):
    tool_id = registry.register_tool(_meta())
    registry.delete_tool(tool_id)
    assert registry.get_tool(tool_id) is None


def test_delete_nonexistent_tool_is_idempotent(registry):
    registry.delete_tool("does_not_exist")  # must not raise


def test_delete_reduces_list_length(registry):
    id_a = registry.register_tool(_meta(name="a"))
    registry.register_tool(_meta(name="b"))
    registry.delete_tool(id_a)
    assert len(registry.list_tools()) == 1


# ---------------------------------------------------------------------------
# _generate_tool_id (pure Python — no DB)
# ---------------------------------------------------------------------------

def test_tool_id_is_deterministic(registry):
    id1 = registry._generate_tool_id("my_tool", "print('hello')")
    id2 = registry._generate_tool_id("my_tool", "print('hello')")
    assert id1 == id2


def test_tool_id_differs_for_different_names(registry):
    assert (
        registry._generate_tool_id("tool_a", "")
        != registry._generate_tool_id("tool_b", "")
    )


def test_tool_id_differs_for_different_code(registry):
    assert (
        registry._generate_tool_id("t", "version_1")
        != registry._generate_tool_id("t", "version_2")
    )


def test_tool_id_is_16_chars(registry):
    assert len(registry._generate_tool_id("name", "code")) == 16
