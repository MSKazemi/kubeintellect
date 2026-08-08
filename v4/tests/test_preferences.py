"""Operator-preference memory — set/recall/render/forget, learning, forgetting."""
from __future__ import annotations

from app.memory import preferences


class FakePool:
    def __init__(self, rows=None, execute_result="INSERT 0 1"):
        self.rows = rows or []
        self.execute_result = execute_result
        self.calls: list[tuple] = []
        self.raise_on = None

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        if self.raise_on == "fetch":
            raise RuntimeError("db down")
        return self.rows

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        if self.raise_on == "execute":
            raise RuntimeError("db down")
        return self.execute_result


class TestSetPreference:
    async def test_explicit_upsert(self):
        pool = FakePool()
        preferences.init_preferences(pool)
        try:
            ok = await preferences.set_preference("u1", "verbosity", "concise")
            assert ok is True
            kind, sql, args = pool.calls[-1]
            assert kind == "execute" and "'explicit'" in sql
            assert args[:3] == ("u1", "verbosity", "concise")
        finally:
            preferences.close_preferences()

    async def test_inferred_upsert_passes_confidence(self):
        pool = FakePool()
        preferences.init_preferences(pool)
        try:
            ok = await preferences.set_preference(
                "u1", "default_namespace", "payments", source="inferred", confidence=0.8
            )
            assert ok is True
            _, sql, args = pool.calls[-1]
            assert "'inferred'" in sql
            assert args[3] == 0.8  # seed confidence
        finally:
            preferences.close_preferences()

    async def test_secrets_redacted_in_value(self):
        pool = FakePool()
        preferences.init_preferences(pool)
        try:
            await preferences.set_preference("u1", "note", "token=ghp_ABC1234567890secret")
            _, _, args = pool.calls[-1]
            assert "ghp_ABC1234567890secret" not in args[2]
        finally:
            preferences.close_preferences()

    async def test_no_pool_returns_false(self):
        preferences.close_preferences()
        assert await preferences.set_preference("u1", "k", "v") is False

    async def test_fail_open_on_db_error(self):
        pool = FakePool()
        pool.raise_on = "execute"
        preferences.init_preferences(pool)
        try:
            assert await preferences.set_preference("u1", "k", "v") is False
        finally:
            preferences.close_preferences()


class TestRecallAndRender:
    async def test_recall_returns_dicts(self):
        pool = FakePool(rows=[
            {"key": "default_namespace", "value": "payments", "source": "inferred",
             "confidence": 0.8, "occurrence_count": 4},
        ])
        preferences.init_preferences(pool)
        try:
            out = await preferences.recall_preferences("u1")
            assert out and out[0]["value"] == "payments"
        finally:
            preferences.close_preferences()

    async def test_recall_fail_open(self):
        pool = FakePool()
        pool.raise_on = "fetch"
        preferences.init_preferences(pool)
        try:
            assert await preferences.recall_preferences("u1") == []
        finally:
            preferences.close_preferences()

    def test_render_marks_inferred_with_confidence(self):
        block = preferences.render_preferences_block([
            {"key": "verbosity", "value": "concise", "source": "explicit", "confidence": 1.0},
            {"key": "default_namespace", "value": "payments", "source": "inferred", "confidence": 0.83},
        ])
        assert "verbosity: concise" in block
        assert "inferred, confidence 83%" in block
        assert block.startswith("## Operator preferences")

    def test_render_empty(self):
        assert preferences.render_preferences_block([]) == ""


class TestForgetAndLearn:
    async def test_forget_executes_delete(self):
        pool = FakePool()
        preferences.init_preferences(pool)
        try:
            assert await preferences.forget_preference("u1", "default_namespace") is True
            _, sql, args = pool.calls[-1]
            assert "DELETE FROM user_prefs" in sql and args == ("u1", "default_namespace")
        finally:
            preferences.close_preferences()

    async def test_infer_from_behaviour_writes_one_per_user(self):
        pool = FakePool(rows=[
            {"user_id": "u1", "namespace": "payments", "c": 5, "total": 6, "share": 0.83},
            {"user_id": "u2", "namespace": "web", "c": 4, "total": 4, "share": 1.0},
        ])
        preferences.init_preferences(pool)
        try:
            updated = await preferences.infer_from_behaviour()
            assert updated == 2
            # each user got a set_preference (execute) for default_namespace
            execs = [c for c in pool.calls if c[0] == "execute"]
            assert len(execs) == 2 and all("'inferred'" in c[1] for c in execs)
        finally:
            preferences.close_preferences()

    async def test_decay_reports_forgotten_count(self):
        pool = FakePool(execute_result="DELETE 3")
        preferences.init_preferences(pool)
        try:
            assert await preferences.decay_and_forget() == 3
        finally:
            preferences.close_preferences()
