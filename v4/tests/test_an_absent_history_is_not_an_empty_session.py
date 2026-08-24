"""tests/test_an_absent_history_is_not_an_empty_session.py

`GET /v1/events/replay/{session_id}` answered three materially different states with one
identical response — HTTP 200 and a lone `data: [DONE]`. Measured 2026-08-24:

    a session id that was never seen here      HTTP 200  frames=1  ['data: [DONE]']
    prepared, genuinely emitted nothing        HTTP 200  frames=1  ['data: [DONE]']
    history lost to a restart / other replica  HTTP 200  frames=1  ['data: [DONE]']

The third one is the expensive case. `_histories` is an in-process dict, and `api-reference.md`
advertises this endpoint for "UIs that reconnect" — so a UI reconnecting to a different replica,
or after a rollout, rendered a real investigation as an investigation that produced nothing.

`/v1/episodes/{id}/replay`, one file over, treats precisely this conflation as "the one wrong
answer this endpoint must not give" and separates 404 / 409 / 503 to avoid it. This endpoint had
none of that. It now answers 404 when the process holds no history, in a body that refuses to
claim anything about the session itself, and prefixes a `replay_meta` frame so that a genuinely
empty session is expressible as `records: 0`.
"""

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.endpoints.events import router
from app.streaming import emitter


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_registry():
    histories, queues = dict(emitter._histories), dict(emitter._queues)
    emitter._histories.clear()
    emitter._queues.clear()
    yield
    emitter._histories.clear()
    emitter._histories.update(histories)
    emitter._queues.clear()
    emitter._queues.update(queues)


def frames(response) -> list[str]:
    return [f for f in response.text.split("\n\n") if f.strip()]


def payloads(response) -> list[dict]:
    out = []
    for f in frames(response):
        body = f.removeprefix("data: ")
        if body != "[DONE]":
            out.append(json.loads(body))
    return out


def _with_events(sid, n=2):
    emitter.prepare_session(sid)
    for i in range(n):
        emitter._histories[sid].append({"type": "token", "data": {"content": f"c{i}"}})


class TestTheThreeStatesNoLongerGiveOneAnswer:
    def test_a_session_this_process_never_saw_is_not_an_empty_replay(self, client):
        r = client.get("/v1/events/replay/never-existed")
        assert r.status_code == 404, "an absent history answered 200 with a bare [DONE]"

    def test_a_session_that_emitted_nothing_is_still_a_successful_replay(self, client):
        emitter.prepare_session("silent")
        r = client.get("/v1/events/replay/silent")
        assert r.status_code == 200
        assert payloads(r)[0]["records"] == 0

    def test_history_lost_to_a_restart_reads_as_absent_not_as_empty(self, client):
        _with_events("restarted")
        emitter._histories.pop("restarted")  # what a rollout leaves behind
        assert client.get("/v1/events/replay/restarted").status_code == 404

    def test_the_two_states_no_longer_share_a_status_code(self, client):
        emitter.prepare_session("silent")
        assert client.get("/v1/events/replay/silent").status_code != \
            client.get("/v1/events/replay/absent").status_code


class TestThe404DoesNotClaimAnythingAboutTheSession:
    """The pass-233 rule: a 404 that means 'not here' must not be read as 'never happened'."""

    def detail(self, client):
        return client.get("/v1/events/replay/gone").json()["detail"]

    def test_it_says_the_absence_is_this_process_only(self, client):
        assert "this process" in self.detail(client)

    def test_it_denies_the_inference_a_reader_would_otherwise_make(self, client):
        d = self.detail(client)
        assert "NOT evidence" in d
        assert "never ran or produced nothing" in d

    def test_it_names_the_volatility_that_causes_this(self, client):
        d = self.detail(client)
        assert "restart" in d and "replicas" in d

    def test_it_points_at_the_durable_endpoint_for_that_session(self, client):
        assert "/v1/episodes/gone/replay" in self.detail(client)


class TestTheMetaFrameMakesAnEmptyReplayExpressible:
    def test_the_first_frame_is_meta(self, client):
        _with_events("s1")
        assert payloads(client.get("/v1/events/replay/s1"))[0]["type"] == "replay_meta"

    def test_the_record_count_matches_what_follows(self, client):
        _with_events("s1", n=3)
        got = payloads(client.get("/v1/events/replay/s1"))
        assert got[0]["records"] == 3 == len(got) - 1

    def test_the_stream_never_claims_to_be_durable(self, client):
        _with_events("s1")
        assert payloads(client.get("/v1/events/replay/s1"))[0]["durable"] is False

    def test_the_events_themselves_are_unchanged(self, client):
        _with_events("s1", n=2)
        got = payloads(client.get("/v1/events/replay/s1"))
        assert [e["data"]["content"] for e in got[1:]] == ["c0", "c1"]

    def test_the_stream_still_ends_with_done(self, client):
        _with_events("s1")
        assert frames(client.get("/v1/events/replay/s1"))[-1] == "data: [DONE]"


class TestHasHistoryIsTheDistinctionItself:
    def test_it_separates_prepared_from_unknown(self):
        emitter.prepare_session("known")
        assert emitter.has_history("known") is True
        assert emitter.has_history("unknown") is False

    def test_an_emitted_session_has_history(self):
        asyncio.run(emitter.emit("emitted", emitter.FinalEvent(session_id="emitted")))
        assert emitter.has_history("emitted") is True

    def test_get_history_still_cannot_express_it(self):
        """Documents why `has_history` had to exist rather than a truthiness check."""
        emitter.prepare_session("known")
        assert emitter.get_history("known") == emitter.get_history("unknown") == []

    def test_it_is_exported(self):
        assert "has_history" in emitter.__all__
