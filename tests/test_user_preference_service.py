# tests/test_user_preference_service.py
"""
Unit tests for UserPreferenceService and MemoryOrchestrator.

All DB calls are replaced by lightweight async fakes so these tests run
without a live database. Async functions are driven via asyncio.run().
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers — minimal async fakes
# ---------------------------------------------------------------------------


def _make_pool(*rows_per_call):
    """
    Return a fake pool whose .connection() context manager yields a fake conn.
    Each sequential call to conn.execute() cycles through *rows_per_call*.
    """
    call_count = [0]
    rows_list = list(rows_per_call) if rows_per_call else [[]]

    class _FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        async def fetchall(self):
            return self._rows

        async def fetchone(self):
            return self._rows[0] if self._rows else None

    class _FakeConn:
        async def execute(self, sql, params=None):
            idx = call_count[0] % len(rows_list)
            call_count[0] += 1
            return _FakeCursor(rows_list[idx])

    class _FakeCtx:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, *_):
            pass

    pool = MagicMock()
    pool.connection = lambda: _FakeCtx()
    return pool


# ---------------------------------------------------------------------------
# UserPreferenceService tests
# ---------------------------------------------------------------------------

from app.services.user_preference_service import (  # noqa: E402
    DEFAULT_VALUE,
    PREF_DEFAULTS,
    PREF_VERBOSITY,
    PREF_FORMAT,
    PREF_DEFAULT_NAMESPACE,
    PREF_DEFAULT_CLUSTER,
    PREF_REMEDIATION_STYLE,
    UserPreferenceService,
)


def test_load_returns_defaults_for_empty_db():
    pool = _make_pool([])
    result = asyncio.run(UserPreferenceService.load("user1", pool))
    assert result == PREF_DEFAULTS


def test_load_merges_stored_values():
    pool = _make_pool([(PREF_VERBOSITY, "concise")])
    result = asyncio.run(UserPreferenceService.load("user1", pool))
    assert result[PREF_VERBOSITY] == "concise"
    assert result[PREF_FORMAT] == DEFAULT_VALUE


def test_load_no_pool():
    result = asyncio.run(UserPreferenceService.load("user1", None))
    assert result == PREF_DEFAULTS


def test_upsert_calls_execute():
    executed: list[str] = []

    class _CaptureCursor:
        async def fetchall(self): return []
        async def fetchone(self): return None

    class _CaptureConn:
        async def execute(self, sql, params=None):
            executed.append(sql)
            return _CaptureCursor()

    class _FakeCtx:
        async def __aenter__(self): return _CaptureConn()
        async def __aexit__(self, *_): pass

    pool = MagicMock()
    pool.connection = lambda: _FakeCtx()

    asyncio.run(UserPreferenceService.upsert("user1", PREF_VERBOSITY, "concise", 1.0, pool))
    assert any("user_preferences" in s for s in executed)
    assert any("ON CONFLICT" in s for s in executed)


def test_upsert_skips_without_pool():
    asyncio.run(UserPreferenceService.upsert("user1", PREF_VERBOSITY, "concise", 1.0, None))
    # no exception → pass


# ---------------------------------------------------------------------------
# Heuristic tests
# ---------------------------------------------------------------------------

def test_heuristic_explicit_concise():
    upserted: list[tuple] = []

    async def _fake_upsert(user_id, key, value, confidence, pool):
        upserted.append((key, value, confidence))

    from langchain_core.messages import HumanMessage

    with patch.object(UserPreferenceService, "upsert", side_effect=_fake_upsert):
        asyncio.run(UserPreferenceService._heuristic_explicit(
            "user1",
            [HumanMessage(content="please keep it short and concise")],
            MagicMock(),
        ))

    assert any(k == PREF_VERBOSITY and v == "concise" and c == 1.0 for k, v, c in upserted)


def test_heuristic_explicit_verbose():
    upserted: list[tuple] = []

    async def _fake_upsert(user_id, key, value, confidence, pool):
        upserted.append((key, value, confidence))

    from langchain_core.messages import HumanMessage

    with patch.object(UserPreferenceService, "upsert", side_effect=_fake_upsert):
        asyncio.run(UserPreferenceService._heuristic_explicit(
            "user1",
            [HumanMessage(content="give me more detail please")],
            MagicMock(),
        ))

    assert any(k == PREF_VERBOSITY and v == "verbose" for k, v, _ in upserted)


def test_heuristic_explicit_conservative():
    upserted: list[tuple] = []

    async def _fake_upsert(user_id, key, value, confidence, pool):
        upserted.append((key, value, confidence))

    from langchain_core.messages import HumanMessage

    with patch.object(UserPreferenceService, "upsert", side_effect=_fake_upsert):
        asyncio.run(UserPreferenceService._heuristic_explicit(
            "user1",
            [HumanMessage(content="please be read-only, don't change anything")],
            MagicMock(),
        ))

    assert any(k == PREF_REMEDIATION_STYLE and v == "conservative" for k, v, _ in upserted)


def test_heuristic_namespace_promotes_after_3():
    upserted: list[tuple] = []

    async def _fake_upsert(user_id, key, value, confidence, pool):
        upserted.append((key, value, confidence))

    # DB returns 3 rows; production appears 2× in DB + 1 current = 3 → promotes
    pool = _make_pool([("production",), ("production",), ("staging",)])

    with patch.object(UserPreferenceService, "upsert", side_effect=_fake_upsert):
        asyncio.run(UserPreferenceService._heuristic_namespace(
            "user1", "production", pool
        ))

    assert any(k == PREF_DEFAULT_NAMESPACE and v == "production" and c == 0.7
               for k, v, c in upserted)


def test_heuristic_namespace_no_promotion_below_threshold():
    upserted: list[tuple] = []

    async def _fake_upsert(user_id, key, value, confidence, pool):
        upserted.append((key, value, confidence))

    # production appears once in DB + current=development → different, no promotion
    pool = _make_pool([("production",), ("staging",)])

    with patch.object(UserPreferenceService, "upsert", side_effect=_fake_upsert):
        asyncio.run(UserPreferenceService._heuristic_namespace(
            "user1", "development", pool
        ))

    # no key should reach count 3
    assert not any(k == PREF_DEFAULT_NAMESPACE for k, v, _ in upserted)


def test_heuristic_cluster_promotes_after_3():
    upserted: list[tuple] = []

    async def _fake_upsert(user_id, key, value, confidence, pool):
        upserted.append((key, value, confidence))

    existing = json.dumps({"prod-cluster": 2})
    pool = _make_pool([(existing,)])

    with patch.object(UserPreferenceService, "upsert", side_effect=_fake_upsert):
        asyncio.run(UserPreferenceService._heuristic_cluster(
            "user1", "prod-cluster", pool
        ))

    assert any(k == PREF_DEFAULT_CLUSTER and v == "prod-cluster" and c == 0.7
               for k, v, c in upserted)


def test_heuristic_cluster_skips_none():
    upserted: list[tuple] = []

    async def _fake_upsert(*args):
        upserted.append(args)

    with patch.object(UserPreferenceService, "upsert", side_effect=_fake_upsert):
        asyncio.run(UserPreferenceService._heuristic_cluster("user1", None, MagicMock()))

    assert upserted == []


def test_detect_and_save_never_raises():
    with patch.object(UserPreferenceService, "_heuristic_namespace", side_effect=RuntimeError("boom")):
        with patch.object(UserPreferenceService, "_heuristic_explicit", side_effect=RuntimeError("boom")):
            with patch.object(UserPreferenceService, "_heuristic_cluster", side_effect=RuntimeError("boom")):
                asyncio.run(UserPreferenceService.detect_and_save(
                    "user1", [], "default", "my-cluster", MagicMock()
                ))


# ---------------------------------------------------------------------------
# MemoryOrchestrator tests
# ---------------------------------------------------------------------------

from app.services.memory_orchestrator import MemoryOrchestrator, _render_as_text  # noqa: E402


class _FP:
    """Minimal FailurePattern stub."""
    pattern_id = "fp-oom"
    type = "OOMKilled"
    confidence = 0.85
    recommended_checks = ["Check memory limits", "Check HPA settings", "Check JVM heap"]
    remediation_steps = ["Increase memory limits", "Add VPA"]


def test_build_context_no_pool():
    ctx = asyncio.run(MemoryOrchestrator.build_context(
        user_id="u1", query="my pod is OOMKilled",
        conversation_history=[], current_namespace=None,
        current_cluster=None, pool=None,
    ))
    assert ctx.pinned_message is None
    assert ctx.reflection_lessons == []
    assert ctx.user_prefs == {}


def test_build_context_with_reflections_and_prefs():
    async def _load_refl(*a, **kw):
        return ["Use Logs agent for pod logs", "Check namespace first"]

    async def _load_fp(*a, **kw):
        return None

    async def _load_prefs(*a, **kw):
        return {
            "verbosity": "concise",
            "format": "default",
            "default_namespace": None,
            "default_cluster": None,
            "remediation_style": "default",
        }

    with patch("app.services.memory_orchestrator.MemoryOrchestrator._load_reflections", _load_refl):
        with patch("app.services.memory_orchestrator.MemoryOrchestrator._load_failure_pattern", _load_fp):
            with patch("app.services.memory_orchestrator.MemoryOrchestrator._load_user_prefs", _load_prefs):
                with patch("app.services.memory_orchestrator.asyncio.create_task"):
                    ctx = asyncio.run(MemoryOrchestrator.build_context(
                        user_id="u1", query="pod is crashing",
                        conversation_history=[], current_namespace=None,
                        current_cluster=None, pool=MagicMock(),
                    ))

    assert ctx.pinned_message is not None
    text = ctx.pinned_message.content
    assert "concise" in text
    assert "Past routing lessons" in text


def test_build_context_with_failure_pattern():
    async def _load_refl(*a, **kw): return []
    async def _load_fp(*a, **kw): return _FP()
    async def _load_prefs(*a, **kw):
        return {"verbosity": "default", "format": "default",
                "default_namespace": None, "default_cluster": None,
                "remediation_style": "default"}

    with patch("app.services.memory_orchestrator.MemoryOrchestrator._load_reflections", _load_refl):
        with patch("app.services.memory_orchestrator.MemoryOrchestrator._load_failure_pattern", _load_fp):
            with patch("app.services.memory_orchestrator.MemoryOrchestrator._load_user_prefs", _load_prefs):
                with patch("app.services.memory_orchestrator.asyncio.create_task"):
                    ctx = asyncio.run(MemoryOrchestrator.build_context(
                        user_id="u1", query="OOMKilled pod",
                        conversation_history=[], current_namespace=None,
                        current_cluster=None, pool=MagicMock(),
                    ))

    assert ctx.matched_pattern_id == "fp-oom"
    assert ctx.pinned_message is not None
    assert "OOMKilled" in ctx.pinned_message.content
    assert "Check memory limits" in ctx.pinned_message.content


def test_build_context_all_defaults_no_message():
    async def _load_refl(*a, **kw): return []
    async def _load_fp(*a, **kw): return None
    async def _load_prefs(*a, **kw):
        return {"verbosity": "default", "format": "default",
                "default_namespace": None, "default_cluster": None,
                "remediation_style": "default"}

    with patch("app.services.memory_orchestrator.MemoryOrchestrator._load_reflections", _load_refl):
        with patch("app.services.memory_orchestrator.MemoryOrchestrator._load_failure_pattern", _load_fp):
            with patch("app.services.memory_orchestrator.MemoryOrchestrator._load_user_prefs", _load_prefs):
                with patch("app.services.memory_orchestrator.asyncio.create_task"):
                    ctx = asyncio.run(MemoryOrchestrator.build_context(
                        user_id="u1", query="list pods",
                        conversation_history=[], current_namespace=None,
                        current_cluster=None, pool=MagicMock(),
                    ))

    assert ctx.pinned_message is None


def test_render_token_budget():
    from app.services.memory_orchestrator import _PINNED_MSG_BUDGET, _tokens

    reflections = [
        "Use Logs agent for get_pod_logs" * 5,
        "Check namespace first" * 5,
        "Ask for confirmation before deleting" * 5,
    ]
    prefs = {"verbosity": "concise", "format": "root-cause-first"}
    text = _render_as_text(reflections, _FP(), prefs)

    assert _tokens(text) <= _PINNED_MSG_BUDGET


def test_render_empty_when_all_default():
    text = _render_as_text([], None, {"verbosity": "default", "format": "default",
                                      "default_namespace": None, "default_cluster": None,
                                      "remediation_style": "default"})
    assert text == ""


# ---------------------------------------------------------------------------
# Regression: Bug 2 — psycopg3 LIKE placeholder fix
# ---------------------------------------------------------------------------

def test_load_sql_uses_correct_like_escape():
    """load() must use '\\%%_' (not '\\_%') so psycopg3 does not raise ProgrammingError.

    This test captures the raw SQL passed to conn.execute() and asserts that the
    LIKE clause contains the correctly escaped literal '%%_'.  If the original bug
    ('\\_%') were reintroduced, psycopg3 would reject '%%_' → '%_' → fail with
    ProgrammingError("only '%s', '%b', '%t' are allowed as placeholders, got '%'").
    """
    executed_sql: list[str] = []

    class _CaptureCursor:
        async def fetchall(self): return []

    class _CaptureConn:
        async def execute(self, sql, params=None):
            executed_sql.append(sql)
            return _CaptureCursor()

    class _FakeCtx:
        async def __aenter__(self): return _CaptureConn()
        async def __aexit__(self, *_): pass

    pool = MagicMock()
    pool.connection = lambda: _FakeCtx()

    result = asyncio.run(UserPreferenceService.load("cli-user-test", pool))

    # The query must execute without raising (i.e. load returns defaults, not crashes).
    assert result == PREF_DEFAULTS

    # The LIKE clause must use '%%_' — the psycopg3-safe escape for a literal '%_'.
    assert executed_sql, "conn.execute() was never called"
    like_sql = next((s for s in executed_sql if "LIKE" in s), None)
    assert like_sql is not None, "No LIKE clause found in executed SQL"
    assert "%%_" in like_sql, (
        f"Expected '%%_' in LIKE clause but got: {like_sql!r}. "
        "Bug 2 regression: '\\_%' would cause psycopg3 ProgrammingError."
    )
    assert "\\_%'" not in like_sql or "\\%%_" in like_sql, (
        "LIKE clause contains unescaped '%_' placeholder — psycopg3 would reject this."
    )
