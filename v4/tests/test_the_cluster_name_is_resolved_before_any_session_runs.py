"""The cluster's name is worked out by the server before any session runs, never inside one.

Pilot run 3 (2026-11-15, pre-registered check P13: "every kubectl call made inside an investigation ran as that
namespace's session identity; none as the server") failed for all three models on exactly two calls:
`kubectl config current-context` and `kubectl config view --minify …` from `app/cluster_id.py`. `get_cluster_id` is
cached, but it was computed lazily, so the first caller paid for it — the context snapshot of the first investigation,
which then ran it with the server's kubeconfig. In the pilot those two calls only read a local file. In a Helm
deployment without `CLUSTER_ID` there is no kubeconfig to read, and the same code falls through to
`kubectl get namespace kube-system`: an API request made with the server's permissions, inside an investigation.

So the server resolves it at startup, and the watchtower resolves it before an investigation's first turn; inside a
session it is only ever read from the cache.
"""

from __future__ import annotations

from unittest import mock

import pytest

from app import cluster_id as cid


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(cid.settings, "CLUSTER_ID", "")
    cached = (
        cid.get_cluster_id
    )  # a test may replace it; clear the real cache either way
    cached.cache_clear()
    yield
    cached.cache_clear()


def _recording(events: list[str]):
    def fake(args, timeout=5):
        events.append("cluster-id:" + " ".join(args[:2]))
        return {"current-context": "kind-x"}.get(args[1], "https://127.0.0.1:6443")

    return fake


async def test_the_watchtower_resolves_it_before_the_first_turn(monkeypatch):
    from app.autonomy import watchtower as wt
    from app.detectors.models import Finding

    events: list[str] = []
    monkeypatch.setattr(cid, "_kubectl", _recording(events))
    monkeypatch.setattr(
        wt, "a3_allowed", lambda playbook, ns: False
    )  # A2: one turn, nothing written
    monkeypatch.setattr(wt.settings, "SESSION_IDENTITY_ENABLED", False)

    async def turn(*a, **k):
        events.append("turn")
        # What the context snapshot does inside the turn: it must find the name cached.
        cid.get_cluster_id()

    monkeypatch.setattr(wt, "_run_turn", turn)
    finding = Finding(
        playbook="CrashLoopBackOff",
        cluster_id="c",
        namespace="b3-c1",
        object_name="web",
        evidence="x",
    )
    await wt._investigate(finding, "A2")
    assert "turn" in events
    first_turn = events.index("turn")
    assert any(e.startswith("cluster-id:") for e in events[:first_turn]), (
        "resolved before the turn"
    )
    assert not [e for e in events[first_turn:] if e.startswith("cluster-id:")], (
        "never inside the session"
    )


async def test_a_failed_resolution_does_not_stop_the_investigation(monkeypatch):
    from app.autonomy import watchtower as wt
    from app.detectors.models import Finding

    def broken(*a, **k):
        raise RuntimeError("kubectl vanished")

    monkeypatch.setattr(cid, "get_cluster_id", broken)
    monkeypatch.setattr(wt, "a3_allowed", lambda playbook, ns: False)
    monkeypatch.setattr(wt.settings, "SESSION_IDENTITY_ENABLED", False)
    turns: list = []

    async def turn(*a, **k):
        turns.append(a)

    monkeypatch.setattr(wt, "_run_turn", turn)
    finding = Finding(
        playbook="CrashLoopBackOff",
        cluster_id="c",
        namespace="b3-c1",
        object_name="web",
        evidence="x",
    )
    await wt._investigate(finding, "A2")
    assert turns, (
        "the name is a label for memory; failing to resolve it is not a reason to skip an incident"
    )


async def test_server_startup_resolves_it(monkeypatch):
    """Before the API serves a chat turn or the sensorium hands the watchtower a finding."""
    from app import main

    events: list[str] = []
    monkeypatch.setattr(cid, "_kubectl", _recording(events))

    class _Stop(Exception):
        pass

    async def init_memory():
        events.append("memory")

    async def sensorium():  # the next step after the name: stop here
        events.append("workers")
        raise _Stop

    monkeypatch.setattr(main.settings, "LEADER_ELECTION_ENABLED", False)
    monkeypatch.setattr(main.settings, "SENSORIUM_ENABLED", True)
    with (
        mock.patch("app.agent.workflow.init_graph", mock.AsyncMock()),
        mock.patch("app.db.audit.init_audit_pool", mock.AsyncMock()),
        mock.patch("app.db.flight_recorder.init_recorder", mock.AsyncMock()),
        mock.patch("app.memory.service.init_memory", init_memory),
        mock.patch("app.detectors.service.start_sensorium", sensorium),
        mock.patch(
            "app.detectors.service.record_start_failure",
            lambda exc: events.append("workers-failed"),
        ),
    ):
        try:
            async with main.lifespan(None):
                pass
        except BaseException:  # a later startup or shutdown stage needs services this test does not provide
            pass
    assert "memory" in events and any(e.startswith("cluster-id:") for e in events), (
        events
    )
    assert events.index("memory") < next(
        i for i, e in enumerate(events) if e.startswith("cluster-id:")
    )
    if "workers" in events:
        assert next(
            i for i, e in enumerate(events) if e.startswith("cluster-id:")
        ) < events.index("workers")
