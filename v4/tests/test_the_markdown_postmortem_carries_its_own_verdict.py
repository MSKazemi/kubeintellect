"""`format=markdown` must not reduce the audit-chain verdict to prose.

`GET /v1/episodes/{id}/postmortem?format=markdown` returned `{"markdown": ...}` and nothing
else. `render_markdown` puts the verdict in one of four English banners — verified intact,
CHAIN BROKEN, NOT VERIFIED, RECORD INCOMPLETE — so a programmatic caller received the verdict
only as text it would have to parse. Measured 2026-08-24: that is why `kq postmortem` rendered
the tamper warning and still exited 0, while `kq replay` and `kq export`, which render the same
verdict, map it to exit 3/4/5. The CLI had no datum to decide on.

The fields now ride alongside the prose. Additive — a caller reading only `markdown` is
unaffected — and `format=json` is untouched.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.db import flight_recorder
from app.main import app

_EVENTS = [
    ("status", {"message": "investigation started"}),
    ("finding", {"playbook": "OOMKilled", "namespace": "shop", "object": "web-1",
                 "severity": "warning"}),
    ("answer", {"text": "Root cause: container exceeded its 128Mi memory limit."}),
]

_VERDICT_KEYS = ("chain_valid", "chain_verified", "events_lost", "gaps", "enrichment_failed")


def _chain(episode_id: str, events: list[tuple[str, dict]]) -> list[dict]:
    rows, prev = [], ""
    for seq, (kind, payload) in enumerate(events):
        h = flight_recorder.compute_hash(prev, episode_id, seq, kind, payload)
        rows.append({"episode_id": episode_id, "seq": seq, "kind": kind,
                     "payload": json.dumps(payload), "prev_hash": prev, "hash": h,
                     "created_at": datetime.now(tz=timezone.utc)})
        prev = h
    return rows


def _patch_fetch(mocker, rows):
    async def _fetch(_episode_id):
        return rows
    mocker.patch.object(flight_recorder, "fetch_episode", side_effect=_fetch)


class _NullPool:
    """Answers every lookup with "asked, and there is nothing"."""

    async def fetchrow(self, _sql, *_args):
        return None


@pytest.fixture(autouse=True)
def _anchor_read_succeeds(mocker):
    """These tests inject rows straight past `fetch_episode`, so the module's own `_pool` is
    whatever the process left there — and left at None it means the recorder never started, so
    the chain anchor is unreadable and `chain_verified` is False for every case below. What
    they mean is a reachable store with no anchor row for this episode."""
    mocker.patch.object(flight_recorder, "_pool", _NullPool())


async def _get(params: dict) -> dict:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        r = await client.get("/v1/episodes/ep-1/postmortem", params=params)
        assert r.status_code == 200, r.text
        return r.json()


class TestTheVerdictArrivesAsData:
    async def test_markdown_mode_carries_every_verdict_field(self, mocker):
        _patch_fetch(mocker, _chain("ep-1", _EVENTS))
        body = await _get({"format": "markdown"})
        missing = [k for k in _VERDICT_KEYS if k not in body]
        assert not missing, (
            f"a markdown postmortem still reports its verdict only as prose; missing {missing}")

    async def test_the_prose_is_still_there(self, mocker):
        _patch_fetch(mocker, _chain("ep-1", _EVENTS))
        body = await _get({"format": "markdown"})
        assert "# Incident postmortem" in body["markdown"], (
            "the verdict fields displaced the report they were meant to accompany")

    async def test_an_intact_chain_reports_intact(self, mocker):
        _patch_fetch(mocker, _chain("ep-1", _EVENTS))
        body = await _get({"format": "markdown"})
        assert body["chain_valid"] is True and body["chain_verified"] is True
        assert body["events_lost"] == 0

    async def test_a_broken_chain_reports_broken_in_the_data_too(self, mocker):
        rows = _chain("ep-1", _EVENTS)
        rows[1]["hash"] = "0" * 64          # tamper
        _patch_fetch(mocker, rows)
        body = await _get({"format": "markdown"})
        assert body["chain_valid"] is False, "the data said intact while the prose said BROKEN"
        assert "BROKEN" in body["markdown"], "the two surfaces disagree"

    async def test_an_empty_episode_reports_not_verified(self, mocker):
        _patch_fetch(mocker, [])
        body = await _get({"format": "markdown"})
        assert body["chain_verified"] is False, (
            "nothing was read, so 'verified' is not a claim this response may make")

    async def test_a_legacy_pm_does_not_contradict_its_own_banner(self, mocker):
        """The verdict fields and the banner printed above them must never disagree.

        The defaults here MIRROR `render_markdown`'s deliberately. A stored postmortem written
        before `chain_verified` existed still renders "✅ verified intact" — that is a decided
        contract (`test_an_unverified_chain_is_not_a_broken_one.py`). If this endpoint defaulted
        that field to False instead, the same response would carry a prose banner saying intact
        and a datum that makes the CLI exit 4 NOT VERIFIED. One episode, one verdict.
        """
        async def _legacy(_episode_id):
            return {"episode_id": "ep-1", "chain_valid": True, "timeline": [], "summary": "s"}
        mocker.patch("app.api.v1.endpoints.postmortem.build_postmortem", side_effect=_legacy)
        body = await _get({"format": "markdown"})
        assert "verified intact" in body["markdown"], "the decided legacy banner changed"
        assert body["chain_verified"] is True, (
            "the response says 'verified intact' in prose and 'not verified' in data")

    async def test_a_real_gap_reaches_the_caller_as_a_number(self, mocker):
        """`events_lost` must be the recorder's own count, not a placeholder.

        Everything downstream of this field is arithmetic — `kq postmortem` exits 5 only when it
        is non-zero. A response that always sent 0 would render "RECORD INCOMPLETE" in the prose
        and still exit 0, which is the exact defect this pass is closing, one field over.
        """
        _patch_fetch(mocker, _chain("ep-1", [
            _EVENTS[0],
            (flight_recorder.GAP_KIND, {"dropped": 3, "reason": "recorder was not running"}),
            _EVENTS[2],
        ]))
        body = await _get({"format": "markdown"})
        assert body["events_lost"] == 3, "the recorder's loss count did not reach the caller"
        assert len(body["gaps"]) == 1 and body["gaps"][0]["dropped"] == 3
        assert "RECORD INCOMPLETE" in body["markdown"], "the two surfaces disagree"

    async def test_json_mode_is_unchanged(self, mocker):
        _patch_fetch(mocker, _chain("ep-1", _EVENTS))
        body = await _get({})
        assert body["chain_valid"] is True
        assert "timeline" in body and "markdown" not in body, (
            "the json response changed shape; markdown mode was the only thing being fixed")

    async def test_the_two_formats_never_disagree_on_the_verdict(self, mocker):
        """The whole point: one build, two renderings, one verdict."""
        rows = _chain("ep-1", _EVENTS)
        rows[2]["prev_hash"] = "deadbeef"
        _patch_fetch(mocker, rows)
        md, js = await _get({"format": "markdown"}), await _get({})
        for key in ("chain_valid", "chain_verified", "events_lost"):
            assert md[key] == js[key], f"{key} differs between formats: {md[key]} vs {js[key]}"
