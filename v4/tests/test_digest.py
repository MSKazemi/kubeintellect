"""Morning digest — built strictly from the flight recorder + episodes (P6)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from app.detectors import service as detector_service
from app.digest import builder
from app.sensorium import k8s_watcher
from app.sensorium.k8s_watcher import StreamHealth, reset_stream_health


class _Engine:
    """Enough of a DetectorEngine for `perception_state` to classify."""

    detectors = tuple(range(20))
    trend_blind_since = None
    last_trend_error = None


@pytest.fixture(autouse=True)
def _something_is_watching(monkeypatch):
    """CHANGED-2026-08-20: "Quiet watch" is now a claim about perception as well as
    about the record, so these builder tests stand up a connected watch stream. Without
    one the digest correctly reports that nothing could have fired — see
    `test_digest_quiet_requires_watching.py`, which owns that case.
    """
    reset_stream_health()
    monkeypatch.setattr(detector_service, "_engine", _Engine())
    health = StreamHealth("get pods -A")
    health.connected = True
    k8s_watcher._streams["get pods -A"] = health
    yield
    reset_stream_health()


class FakePool:
    def __init__(self, decision_rows=None, episode_rows=None):
        self.decision_rows = decision_rows or []
        self.episode_rows = episode_rows or []

    async def fetch(self, sql, *args):
        if "decision_log" in sql:
            return self.decision_rows
        return self.episode_rows


def _ts(offset: float = 0.0):
    return datetime.now(tz=timezone.utc)


class TestDigestBuilder:
    async def test_empty_window(self, mocker):
        mocker.patch.object(builder, "_get_pool", return_value=FakePool())
        digest = await builder.build_digest(hours=24)
        assert "Quiet watch" in digest["summary"]
        assert digest["findings"] == []

    async def test_sections_populated(self, mocker):
        decision_rows = [
            {"episode_id": "findings:c1", "kind": "finding",
             "payload": json.dumps({"playbook": "CrashLoopBackOff",
                                    "namespace": "shop", "object": "web-1"}),
             "created_at": _ts()},
            {"episode_id": "sess-1", "kind": "rollback_point",
             "payload": json.dumps({"rollback_id": "rb-abc", "command": "kubectl delete pod web-1"}),
             "created_at": _ts()},
            {"episode_id": "sess-1", "kind": "status",
             "payload": json.dumps({"type": "status"}), "created_at": _ts()},
        ]
        episode_rows = [
            {"trigger_kind": "user_query",
             "trigger_detail": "[autonomous investigation — triggered by detector CrashLoopBackOff] ...",
             "summary": "web-1 crashlooping due to bad command; fix proposed",
             "outcome": "report_only", "verified": None, "namespace": "shop",
             "playbooks": ["CrashLoopBackOff"], "started_at": _ts()},
        ]
        mocker.patch.object(
            builder, "_get_pool",
            return_value=FakePool(decision_rows, episode_rows),
        )
        digest = await builder.build_digest(hours=24)
        assert len(digest["findings"]) == 1
        assert digest["findings"][0]["playbook"] == "CrashLoopBackOff"
        assert len(digest["rollback_points"]) == 1
        assert len(digest["auto_investigations"]) == 1
        assert digest["user_sessions"] == 1
        assert "1 finding(s)" in digest["summary"]

    async def test_db_failure_degrades(self, mocker):
        class BrokenPool:
            async def fetch(self, *_a):
                raise RuntimeError("db down")

        mocker.patch.object(builder, "_get_pool", return_value=BrokenPool())
        digest = await builder.build_digest(hours=24)
        assert digest["findings"] == []  # degraded, never raised

    def test_render_markdown(self):
        digest = {
            "window_hours": 24.0, "generated_at": 0.0,
            "findings": [{"at": 0, "playbook": "OOMKilled", "namespace": "s", "object": "p"}],
            "auto_investigations": [
                {"at": 0, "namespace": "s", "summary": "diagnosed oom", "outcome": "resolved",
                 "verified": True, "playbooks": ["OOMKilled"]},
            ],
            "rollback_points": [{"at": 0, "rollback_id": "rb-1", "command": "kubectl x"}],
            "user_sessions": 2,
            "summary": "1 finding(s), 1 autonomous investigation(s), 1 verified fix(es) in the last 24h.",
        }
        md = builder.render_markdown(digest)
        assert "# KubeIntellect digest" in md
        assert "OOMKilled" in md
        assert "rb-1" in md
        assert "2 user session(s)" in md


class TestDigestEndpoint:
    async def test_endpoint_json_and_markdown(self, mocker):
        from app.main import app
        from httpx import ASGITransport, AsyncClient

        mocker.patch.object(builder, "_get_pool", return_value=FakePool())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.get("/v1/digest")
            assert response.status_code == 200
            assert "summary" in response.json()
            response = await client.get("/v1/digest", params={"format": "markdown"})
            assert "# KubeIntellect digest" in response.json()["markdown"]
