""""Active" must mean a stream is connected, not that an object exists.

`GET /v1/findings` reported `{"sensorium": "active", "detectors": N}` whenever a `DetectorEngine`
had been constructed — a fact about object lifetime, not about perception. Nothing tracked whether
any `kubectl --watch` stream was connected.

Measured 2026-08-20 by starting the real watchers on a machine without kubectl: both watch tasks
hit `FileNotFoundError` and **`return`** — the loop exits permanently, so they never reconnect for
the rest of the process lifetime — and the endpoint still answered

    {"sensorium": "active", "detectors": 20, "findings": []}

which `kq findings` renders as the green line **"No findings · 20 detectors watching"**. Nothing
was watching. An RBAC denial produces the same silence by a different route: kubectl exits
non-zero, the loop retries every 60s forever, and `stderr` was sent to `DEVNULL`, so the one piece
of information that explains it — `pods is forbidden: User "system:serviceaccount:…" cannot watch`
— was discarded.

This is the zero-token detection layer: the part of the system that is supposed to notice trouble
without an LLM. An empty findings list from a deaf sensorium is the most expensive kind of silence.
"""
from __future__ import annotations

import asyncio

import pytest
from app.api.v1.endpoints.findings import list_findings
from app.detectors import service
from app.sensorium import k8s_watcher
from app.sensorium.k8s_watcher import (
    StreamHealth,
    any_stream_connected,
    reset_stream_health,
    stream_health,
)


class _Engine:
    detectors = tuple(range(20))
    def recent_findings(self, **_k):
        return []


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    reset_stream_health()
    monkeypatch.setattr(service, "_engine", _Engine())
    yield
    reset_stream_health()


def _stream(name, *, connected=False, stopped=False, err=None, fails=0):
    h = StreamHealth(name)
    h.connected, h.stopped, h.last_error, h.consecutive_failures = connected, stopped, err, fails
    k8s_watcher._streams[name] = h
    return h


def _get() -> dict:
    return asyncio.run(list_findings(limit=100, since=0.0))


class TestTheEndpointReportsPerceptionNotObjectLifetime:
    def test_a_connected_stream_is_active(self):
        _stream("get pods -A", connected=True)
        assert _get()["sensorium"] == "active"

    def test_a_permanently_stopped_watcher_is_not_active(self):
        """The measured case: kubectl missing ⇒ the loop returns and never retries."""
        _stream("get pods -A", stopped=True, err="kubectl not found on the server")
        _stream("get events -A", stopped=True, err="kubectl not found on the server")
        body = _get()
        assert body["sensorium"] == "stopped"
        assert body["detectors"] == 20, "the detector count is still reported, it is just not watching"

    def test_a_reconnecting_watcher_is_not_active(self):
        """RBAC denial: retries forever at the backoff cap, perceiving nothing meanwhile."""
        _stream("get pods -A", err='pods is forbidden: User "sa" cannot watch', fails=7)
        assert _get()["sensorium"] == "reconnecting"

    def test_one_live_stream_is_enough_to_be_active(self):
        _stream("get pods -A", connected=True)
        _stream("get events -A", stopped=True, err="boom")
        assert _get()["sensorium"] == "active"

    def test_before_any_stream_starts_it_is_not_active(self):
        assert _get()["sensorium"] == "starting"

    def test_no_engine_is_still_disabled(self, monkeypatch):
        monkeypatch.setattr(service, "_engine", None)
        assert _get()["sensorium"] == "disabled"

    def test_the_reason_reaches_the_caller(self):
        _stream("get pods -A", err='pods is forbidden: User "sa" cannot watch', fails=3)
        st = _get()["streams"][0]
        assert "forbidden" in st["last_error"]
        assert st["consecutive_failures"] == 3
        assert st["connected"] is False


class TestTheWatchLoopRecordsWhatHappened:
    """Driving the real `_watch_loop`, not a model of it."""

    def test_a_missing_kubectl_is_recorded_as_stopped(self, monkeypatch):
        # The absence is FORCED, not inherited from the machine. This test used to rely on the
        # host simply not having kubectl installed: where kubectl exists, `_watch_loop` spawns it,
        # gets a connection error instead of FileNotFoundError, and retries with backoff FOREVER —
        # so the test did not fail, it hung, with no timeout to stop it. On a developer laptop
        # that is a wedged terminal; in CI it is a job that burns until the six-hour ceiling.
        async def no_kubectl(*a, **k):
            raise FileNotFoundError(2, "No such file or directory", "kubectl")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", no_kubectl)

        async def go():
            await k8s_watcher._watch_loop(
                ["get", "pods", "-A", "--watch"], lambda d, c: None, "c", lambda o: None)
        asyncio.run(go())          # returns — the loop gives up permanently
        health = stream_health()
        assert len(health) == 1
        assert health[0]["stopped"] is True
        assert health[0]["connected"] is False
        assert "kubectl not found" in health[0]["last_error"]
        assert any_stream_connected() is False

    def test_stderr_is_captured_not_discarded(self):
        """The reason a watch failed is the whole explanation for zero findings."""
        src = (__import__("pathlib").Path(k8s_watcher.__file__)).read_text()
        assert "stderr=asyncio.subprocess.DEVNULL" not in src
        assert "stderr=asyncio.subprocess.PIPE" in src

    def test_a_cancelled_watcher_is_not_left_marked_connected(self):
        async def go():
            h = _stream("get pods -A", connected=True)
            task = asyncio.create_task(k8s_watcher._watch_loop(
                ["get", "pods", "-A", "--watch"], lambda d, c: None, "c", lambda o: None))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return h
        h = asyncio.run(go())
        assert h.connected is False
