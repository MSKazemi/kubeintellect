"""Incident postmortems — grounded narrative over the flight recorder (ADR-011).

Every postmortem is a view over the hash-chained decision_log; the deterministic
timeline cites seq numbers, and the LLM narrative (when enabled) is constrained to
those events and falls back to the deterministic render on any failure.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.db import flight_recorder
from app.digest import postmortem


def _chain(episode_id: str, events: list[tuple[str, dict]]) -> list[dict]:
    """Build a valid hash chain over (kind, payload) events."""
    rows = []
    prev = ""
    for seq, (kind, payload) in enumerate(events):
        h = flight_recorder.compute_hash(prev, episode_id, seq, kind, payload)
        rows.append({
            "episode_id": episode_id, "seq": seq, "kind": kind,
            "payload": json.dumps(payload), "prev_hash": prev, "hash": h,
            "created_at": datetime.now(tz=timezone.utc),
        })
        prev = h
    return rows


_EVENTS = [
    ("status", {"message": "investigation started"}),
    ("finding", {"playbook": "OOMKilled", "namespace": "shop", "object": "web-1",
                 "severity": "warning"}),
    ("tool_call", {"tool": "run_kubectl", "command": "kubectl describe pod web-1 -n shop"}),
    ("tool_result", {"summary": "Last State.Reason=OOMKilled, limit 128Mi"}),
    ("rollback_point", {"rollback_id": "rb-abc123", "command": "kubectl delete pod web-1 -n shop"}),
    ("answer", {"text": "Root cause: container exceeded its 128Mi memory limit. Raised to 256Mi."}),
]


class _NullPool:
    """Answers every lookup with "asked, and there is nothing"."""

    async def fetchrow(self, _sql, *_args):
        return None


def _patch_fetch(mocker, rows):
    async def _fetch(_episode_id):
        return rows
    mocker.patch.object(flight_recorder, "fetch_episode", side_effect=_fetch)


class TestBuildPostmortem:
    async def test_reconstructs_timeline(self, mocker):
        _patch_fetch(mocker, _chain("ep-1", _EVENTS))
        pm = await postmortem.build_postmortem("ep-1")
        assert pm["chain_valid"] is True
        assert len(pm["timeline"]) == len(_EVENTS)
        # ordered by seq, every entry carries its seq
        assert [e["seq"] for e in pm["timeline"]] == list(range(len(_EVENTS)))
        assert len(pm["what_fired"]) == 1
        assert pm["what_fired"][0]["playbook"] == "OOMKilled"
        assert any("describe" in t for t in pm["investigated"])
        assert any("rb-abc123" in t for t in pm["tried"])
        assert pm["root_cause"] and "memory limit" in pm["root_cause"]

    async def test_marks_broken_chain(self, mocker):
        rows = _chain("ep-2", _EVENTS)
        # tamper a payload after the hash was computed
        rows[3]["payload"] = json.dumps({"summary": "ALTERED EVIDENCE"})
        _patch_fetch(mocker, rows)
        pm = await postmortem.build_postmortem("ep-2")
        assert pm["chain_valid"] is False

    async def test_empty_episode_degrades(self, mocker):
        # A reachable store with no anchor row. Leaving `_pool` at None would say the recorder
        # is not running, and an episode with no events and no readable anchor is no longer
        # reported as empty — it cannot be told apart from one whose records were all removed.
        mocker.patch.object(flight_recorder, "_pool", _NullPool())
        _patch_fetch(mocker, [])
        pm = await postmortem.build_postmortem("missing")
        assert pm["timeline"] == []
        assert "no recorded events" in pm["summary"].lower()


class TestRenderMarkdown:
    def test_every_timeline_line_cites_seq(self):
        pm = {
            "episode_id": "ep-1", "generated_at": 0.0, "chain_valid": True,
            "timeline": [{"seq": 0, "at": 0.0, "kind": "status", "summary": "started"},
                         {"seq": 1, "at": 0.0, "kind": "finding", "summary": "OOMKilled fired"}],
            "what_fired": [], "investigated": [], "tried": [], "worked": [],
            "root_cause": None, "follow_ups": [], "narrative": None,
        }
        md = postmortem.render_markdown(pm)
        assert "# Incident postmortem" in md
        assert "verified intact" in md
        # each timeline line references its seq, e.g. "[#0]"
        assert "[#0]" in md and "[#1]" in md

    def test_broken_chain_is_flagged_prominently(self):
        pm = {
            "episode_id": "ep-2", "generated_at": 0.0, "chain_valid": False,
            "timeline": [], "what_fired": [], "investigated": [], "tried": [],
            "worked": [], "root_cause": None, "follow_ups": [], "narrative": None,
        }
        md = postmortem.render_markdown(pm)
        assert "CHAIN BROKEN" in md.upper()


class TestNarrative:
    async def test_disabled_by_default(self, mocker):
        _patch_fetch(mocker, _chain("ep-1", _EVENTS))
        mocker.patch.object(postmortem.settings, "POSTMORTEM_LLM_NARRATIVE", False)
        pm = await postmortem.build_postmortem("ep-1")
        assert pm["narrative"] is None

    async def test_falls_back_when_llm_unavailable(self, mocker):
        _patch_fetch(mocker, _chain("ep-1", _EVENTS))
        mocker.patch.object(postmortem.settings, "POSTMORTEM_LLM_NARRATIVE", True)

        def _boom():
            raise RuntimeError("no llm configured")

        mocker.patch("app.cortex.models.get_synthesis_llm", side_effect=_boom)
        pm = await postmortem.build_postmortem("ep-1")
        assert pm["narrative"] is None  # degraded, never raised
        # deterministic timeline still present
        assert "[#0]" in postmortem.render_markdown(pm)

    async def test_narrative_attached_when_llm_returns(self, mocker):
        _patch_fetch(mocker, _chain("ep-1", _EVENTS))
        mocker.patch.object(postmortem.settings, "POSTMORTEM_LLM_NARRATIVE", True)

        class FakeResp:
            content = "Per [#1] the OOMKilled detector fired; per [#5] the fix raised the limit."

        class FakeLLM:
            async def ainvoke(self, _messages):
                return FakeResp()

        mocker.patch("app.cortex.models.get_synthesis_llm", return_value=FakeLLM())
        pm = await postmortem.build_postmortem("ep-1")
        assert pm["narrative"] is not None
        assert "[#1]" in pm["narrative"]


class TestPostmortemEndpoint:
    async def test_endpoint_json_and_markdown(self, mocker):
        from app.main import app
        from httpx import ASGITransport, AsyncClient

        _patch_fetch(mocker, _chain("ep-1", _EVENTS))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            r = await client.get("/v1/episodes/ep-1/postmortem")
            assert r.status_code == 200
            assert r.json()["chain_valid"] is True
            r = await client.get("/v1/episodes/ep-1/postmortem", params={"format": "markdown"})
            assert "# Incident postmortem" in r.json()["markdown"]
