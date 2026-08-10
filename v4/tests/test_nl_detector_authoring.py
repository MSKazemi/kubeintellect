"""Natural-language detector authoring + shadow gate (ADR-012).

The load-bearing safety property: a candidate/shadow detector observes and
accrues precision but can NEVER reach the watchtower until a human promotes it.
"""
from __future__ import annotations

import json
import time

from app.detectors import authoring, review
from app.detectors import engine as engine_mod
from app.detectors.engine import DetectorEngine, load_db_detectors
from app.detectors.models import parse_detect_block
from app.memory import service
from app.sensorium.observations import Observation

_GOOD_BLOCK = {"watch_predicates": [{"kind": "Pod", "status_regex": "^OOMKilled$"}]}


def _obs(status="OOMKilled", ns="dev", name="web-1"):
    return Observation(
        kind="pod_status", cluster_id="t", namespace=ns, name=name,
        fields={"status": status}, ts=time.time(),
    )


class TestCompileAndValidate:
    async def test_compile_produces_valid_block(self, mocker):
        class FakeResp:
            content = "Here you go:\n" + json.dumps(_GOOD_BLOCK)

        class FakeLLM:
            async def ainvoke(self, _messages):
                return FakeResp()

        mocker.patch("app.cortex.models.get_specialist_llm", return_value=FakeLLM())
        raw = await authoring.compile_nl_to_detect_block("pods getting OOM killed")
        block, errors = authoring.validate_detect_block(raw, name="OOMtest")
        assert block is not None
        assert not errors
        assert block.watch_predicates[0].matches(_obs())

    def test_validate_rejects_bad_regex(self):
        block, errors = authoring.validate_detect_block(
            {"watch_predicates": [{"kind": "Pod", "status_regex": "[unterminated"}]}, name="x"
        )
        assert block is None
        assert errors

    def test_validate_rejects_empty_block(self):
        block, errors = authoring.validate_detect_block({"watch_predicates": []}, name="x")
        assert block is None
        assert errors

    async def test_compile_failure_is_fail_open(self, mocker):
        def _boom():
            raise RuntimeError("no llm")

        mocker.patch("app.cortex.models.get_specialist_llm", side_effect=_boom)
        raw = await authoring.compile_nl_to_detect_block("anything")
        assert raw == {}  # no raise


class TestShadowGate:
    def test_shadow_detector_does_not_call_watchtower(self):
        """CRITICAL safety test: a shadow detector fires into its own buffer and
        the watchtower callback is never invoked."""
        block = parse_detect_block("ShadowOOM", _GOOD_BLOCK)
        called = []
        eng = DetectorEngine(
            detectors=(), shadow_detectors=(block,), cluster_id="t",
            on_finding=lambda f: called.append(f),
        )
        eng.process(_obs())
        assert len(eng.shadow_findings) == 1
        assert eng.shadow_findings[0].source == "shadow"
        assert called == []  # watchtower NEVER invoked for a shadow detector

    def test_active_detector_still_calls_watchtower(self):
        block = parse_detect_block("ActiveOOM", _GOOD_BLOCK)
        called = []
        eng = DetectorEngine(
            detectors=(block,), cluster_id="t",
            on_finding=lambda f: called.append(f),
        )
        eng.process(_obs())
        assert len(called) == 1  # control: real detectors do reach the watchtower


class TestLoadDbDetectors:
    async def test_compiles_and_routes_by_status(self, mocker):
        class FakePool:
            async def fetch(self, _sql, *_a):
                return [
                    {"name": "nl:oom", "predicate": json.dumps(_GOOD_BLOCK), "status": "shadow"},
                    {"name": "nl:crash",
                     "predicate": json.dumps({"watch_predicates": [{"kind": "Pod", "status_regex": "^CrashLoopBackOff$"}]}),
                     "status": "active"},
                    # consolidation 'learned' rows are not detect blocks → skipped
                    {"name": "learned:x",
                     "predicate": json.dumps({"derived_from_playbooks": [], "pattern": "x"}),
                     "status": "active"},
                ]

        mocker.patch.object(service, "_pool", FakePool())
        active, shadow = await load_db_detectors()
        assert len(shadow) == 1
        assert len(active) == 1  # the non-detect 'learned' row is filtered out

    async def test_no_pool_returns_empty(self, mocker):
        mocker.patch.object(service, "_pool", None)
        assert await load_db_detectors() == ((), ())


class TestStageAndPromote:
    async def test_stage_candidate_inserts_shadow(self, mocker):
        captured = {}

        class FakePool:
            async def execute(self, sql, *args):
                captured["sql"] = sql
                captured["args"] = args
                return "INSERT 0 1"

        mocker.patch.object(service, "_pool", FakePool())
        ok = await authoring.stage_candidate("nl:oom", "OOM killed pods", _GOOD_BLOCK, author="admin")
        assert ok is True
        assert "shadow" in captured["sql"]
        assert "'nl'" in captured["sql"] or "nl" in captured["args"]

    async def test_promote_moves_shadow_to_active(self, mocker):
        captured = {}

        class FakePool:
            async def execute(self, sql, *args):
                captured["sql"] = sql
                captured["args"] = args
                return "UPDATE 1"

        mocker.patch.object(service, "_pool", FakePool())
        ok = await review.promote_candidate("nl:oom", reviewer="admin")
        assert ok is True
        assert "active" in captured["args"]  # status bound as a parameter
        assert "nl:oom" in captured["args"]

    async def test_promote_no_pool_returns_false(self, mocker):
        mocker.patch.object(service, "_pool", None)
        assert await review.promote_candidate("nl:oom", reviewer="admin") is False


class TestEndpoint:
    async def test_disabled_returns_404(self, mocker):
        from app.api.v1.endpoints import detectors as ep
        from app.main import app
        from httpx import ASGITransport, AsyncClient

        mocker.patch.object(ep.settings, "NL_DETECTOR_AUTHORING_ENABLED", False)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            r = await client.post("/v1/detectors", json={"description": "x"})
            assert r.status_code == 404
