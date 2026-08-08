"""Flight recorder (ADR-005) — hash chain, scrubbing, fire-and-forget, replay."""
from __future__ import annotations

import asyncio
import json
import time


from app.db import flight_recorder as fr


def _build_chain(events: list[tuple[str, dict]], episode_id: str = "ep1") -> list[dict]:
    """Build decision_log-shaped rows the way the drain task would."""
    rows = []
    prev = ""
    for seq, (kind, payload) in enumerate(events):
        digest = fr.compute_hash(prev, episode_id, seq, kind, payload)
        rows.append(
            {
                "episode_id": episode_id,
                "seq": seq,
                "kind": kind,
                "payload": payload,
                "prev_hash": prev,
                "hash": digest,
            }
        )
        prev = digest
    return rows


SAMPLE = [
    ("status", {"type": "status", "phase": "analyzing", "message": "looking"}),
    ("tool_call", {"type": "tool_call", "tool": "run_kubectl", "command": "get pods"}),
    ("tool_result", {"type": "tool_result", "tool": "run_kubectl", "output": "OK"}),
    ("final", {"type": "final"}),
]


class TestHashChain:
    def test_verify_intact_chain(self):
        assert fr.verify_chain(_build_chain(SAMPLE)) is True

    def test_empty_chain_is_valid(self):
        assert fr.verify_chain([]) is True

    def test_tampered_payload_detected(self):
        rows = _build_chain(SAMPLE)
        rows[1]["payload"] = {"type": "tool_call", "tool": "run_kubectl", "command": "delete ns prod"}
        assert fr.verify_chain(rows) is False

    def test_deleted_row_detected(self):
        rows = _build_chain(SAMPLE)
        del rows[1]
        assert fr.verify_chain(rows) is False

    def test_reordered_rows_detected(self):
        rows = _build_chain(SAMPLE)
        rows[1], rows[2] = rows[2], rows[1]
        assert fr.verify_chain(rows) is False

    def test_forged_hash_detected(self):
        rows = _build_chain(SAMPLE)
        rows[2]["hash"] = "0" * 64
        assert fr.verify_chain(rows) is False

    def test_jsonb_string_payload_accepted(self):
        # asyncpg returns JSONB columns as str — verify_chain must handle both.
        rows = _build_chain(SAMPLE)
        for row in rows:
            row["payload"] = json.dumps(row["payload"], sort_keys=False)
        assert fr.verify_chain(rows) is True

    def test_chain_is_deterministic(self):
        a = _build_chain(SAMPLE)
        b = _build_chain(SAMPLE)
        assert [r["hash"] for r in a] == [r["hash"] for r in b]


class TestScrub:
    def test_secret_lines_redacted(self):
        scrubbed = fr._scrub({"command": "kubectl create secret generic x --from-literal=password=hunter2"})
        assert "hunter2" not in scrubbed["command"]

    def test_non_string_fields_pass_through(self):
        payload = {"ts": 123.4, "steps": [{"a": 1}], "flag": True}
        assert fr._scrub(payload) == payload


class TestRecordDiscipline:
    def test_record_without_init_is_noop(self):
        # _queue is None when the recorder was never initialised (e.g. SQLite mode).
        fr.record("ep", "status", {"type": "status"})  # must not raise

    def test_token_events_skipped(self):
        fr._queue = asyncio.Queue()
        try:
            fr.record("ep", "token", {"type": "token", "content": "x"})
            assert fr._queue.empty()
            fr.record("ep", "status", {"type": "status"})
            assert fr._queue.qsize() == 1
        finally:
            fr._queue = None

    def test_record_overhead_under_gate(self):
        # P1 exit gate: p95 request overhead < 5 ms. The request-path cost is
        # one put_nowait; assert a generous bound to avoid CI flakiness.
        fr._queue = asyncio.Queue()
        try:
            n = 1000
            start = time.perf_counter()
            for i in range(n):
                fr.record("ep", "status", {"type": "status", "message": f"m{i}"})
            avg_ms = (time.perf_counter() - start) * 1000 / n
            assert avg_ms < 1.0, f"record() averaged {avg_ms:.3f} ms"
        finally:
            fr._queue = None


class TestEmitterIntegration:
    async def test_emit_records_non_token_events(self, mocker):
        from app.streaming import emitter

        recorded = []
        mocker.patch.object(
            emitter.flight_recorder,
            "record",
            side_effect=lambda eid, kind, payload: recorded.append((eid, kind)),
        )
        sid = "fr-test-session"
        emitter.prepare_session(sid)
        await emitter.emit(sid, emitter.StatusEvent(phase="analyzing", message="x", session_id=sid))
        await emitter.emit(sid, emitter.TokenEvent(content="tok", session_id=sid))
        await emitter.close_session(sid)

        kinds = [k for (_, k) in recorded]
        assert kinds == ["status", "token", "final"]  # emitter forwards all; recorder itself skips tokens
        assert all(eid == sid for (eid, _) in recorded)


class TestReplayEndpoint:
    async def test_replay_streams_meta_and_events(self, mocker):
        from httpx import ASGITransport, AsyncClient

        from app.main import app

        rows = _build_chain(SAMPLE, episode_id="ep-replay")
        mocker.patch("app.api.v1.endpoints.episodes.fetch_episode", return_value=rows)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.get("/v1/episodes/ep-replay/replay")
        assert response.status_code == 200

        frames = [
            json.loads(line[len("data: "):])
            for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        assert frames[0]["type"] == "replay_meta"
        assert frames[0]["chain_valid"] is True
        assert frames[0]["records"] == len(SAMPLE)
        assert [f["type"] for f in frames[1:]] == [k for k, _ in SAMPLE]

    async def test_replay_flags_broken_chain(self, mocker):
        from httpx import ASGITransport, AsyncClient

        from app.main import app

        rows = _build_chain(SAMPLE, episode_id="ep-bad")
        rows[1]["payload"] = {"type": "tool_call", "tool": "run_kubectl", "command": "tampered"}
        mocker.patch("app.api.v1.endpoints.episodes.fetch_episode", return_value=rows)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.get("/v1/episodes/ep-bad/replay")
        meta = json.loads(response.text.splitlines()[0][len("data: "):])
        assert meta["chain_valid"] is False

    async def test_replay_unknown_episode_404(self, mocker):
        from httpx import ASGITransport, AsyncClient

        from app.main import app

        mocker.patch("app.api.v1.endpoints.episodes.fetch_episode", return_value=[])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.get("/v1/episodes/nope/replay")
        assert response.status_code == 404


class TestDrainFlush:
    async def test_flush_builds_contiguous_chain_across_batches(self, mocker):
        inserted: list[tuple] = []

        class FakePool:
            async def executemany(self, _sql, rows):
                inserted.extend(rows)

            async def fetchrow(self, _sql, _eid):
                return None  # no prior rows — genesis chain

        mocker.patch.object(fr, "_pool", FakePool())
        fr._chains.clear()
        try:
            await fr._flush([("ep-x", "status", {"type": "status", "message": "a"})])
            await fr._flush([("ep-x", "final", {"type": "final"})])
        finally:
            fr._chains.clear()
            fr._pool = None

        rows = [
            {
                "episode_id": r[0], "seq": r[1], "kind": r[2],
                "payload": json.loads(r[3]), "prev_hash": r[4], "hash": r[5],
            }
            for r in inserted
        ]
        assert [r["seq"] for r in rows] == [0, 1]
        assert fr.verify_chain(rows) is True
