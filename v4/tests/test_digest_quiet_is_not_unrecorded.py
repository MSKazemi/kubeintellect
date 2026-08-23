"""A quiet watch and an unrecorded one must not share a sentence.

`kq digest` is the operator's morning check — *what did the agent do while I was away*. An empty
digest is only reassuring if the sources were actually readable. Measured 2026-08-20, four
materially different states produced the identical, confident line:

    Quiet watch: no findings in the last 24h.

  1. a genuinely quiet night — recorder on, table present, no rows;
  2. the `decision_log` query raised (`except Exception: rows = []`);
  3. **SQLite mode** — a supported, documented configuration in which, per
     `docs/flight-recorder.md`, "there is no `decision_log` in the SQLite schema", so the digest
     structurally cannot have data;
  4. `FLIGHT_RECORDER_ENABLED=false` — nothing is ever written.

Only a missing connection pool was reported honestly. `kq digest` renders the sentence and exits
0 in every case, so a night during which recording was off reads exactly like a night on which
nothing went wrong.

The fix does not suppress the digest — an operator with the recorder off still wants the sections
that *are* readable. It names every source that could not answer, in `degraded_reasons`, which is
empty exactly when the digest is a real observation of the window.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from app.core.config import settings
from app.digest.builder import build_digest, render_markdown


class _Pool:
    """A pool whose fetch either raises or returns rows — the two shapes that matter."""

    def __init__(self, exc: Exception | None = None, rows: list | None = None):
        self.exc, self.rows = exc, rows or []

    async def fetch(self, *a, **k):
        if self.exc:
            raise self.exc
        return self.rows


def _digest(pool=None, **flags) -> dict:
    defaults = {"FLIGHT_RECORDER_ENABLED": True, "USE_SQLITE": False, "WATCHTOWER_ENABLED": True}
    with patch("app.memory.service._pool", pool):
        with patch.multiple(settings, **{**defaults, **flags}):
            return asyncio.run(build_digest(24.0))


@pytest.fixture(autouse=True)
def _something_is_watching(monkeypatch):
    """CHANGED-2026-08-20: a fifth indistinguishable state was found — a perfectly
    recorded window in which **nothing was watching**. "Quiet" is now a claim about
    perception too, so these cases stand up a connected watch stream; the new case has
    its own file (`test_digest_quiet_requires_watching.py`).
    """
    from app.detectors import service as detector_service
    from app.sensorium import k8s_watcher
    from app.sensorium.k8s_watcher import StreamHealth, reset_stream_health

    class _Engine:
        detectors = tuple(range(20))
        trend_blind_since = None
        last_trend_error = None

    reset_stream_health()
    monkeypatch.setattr(detector_service, "_engine", _Engine())
    health = StreamHealth("get pods -A")
    health.connected = True
    k8s_watcher._streams["get pods -A"] = health
    yield
    reset_stream_health()


@pytest.fixture
def quiet():
    """The one state that genuinely earns the reassuring sentence."""
    return _digest(_Pool(rows=[]))


class TestAGenuinelyQuietWatchStillReadsAsOne:
    def test_it_says_quiet(self, quiet):
        assert quiet["summary"] == "Quiet watch: no findings in the last 24h."

    def test_it_is_not_marked_degraded(self, quiet):
        assert quiet["degraded"] is False
        assert quiet["degraded_reasons"] == []

    def test_the_rendered_digest_carries_no_warning(self, quiet):
        assert "incomplete" not in render_markdown(quiet).lower()


class TestEveryUnrecordedStateRefusesTheWord:
    """The four cases that used to be indistinguishable from the one above."""

    CASES = {
        "decision_log query failed": dict(
            pool=_Pool(exc=RuntimeError('relation "decision_log" does not exist'))),
        "sqlite mode has no decision_log": dict(
            pool=_Pool(exc=RuntimeError("no such table: decision_log")), USE_SQLITE=True),
        "flight recorder disabled": dict(pool=_Pool(rows=[]), FLIGHT_RECORDER_ENABLED=False),
        "no pool at all": dict(pool=None),
    }

    @pytest.mark.parametrize("case", CASES, ids=list(CASES))
    def test_it_is_marked_degraded(self, case):
        d = _digest(**self.CASES[case])
        assert d["degraded"] is True
        assert d["degraded_reasons"], "a degraded digest must say which source failed"

    @pytest.mark.parametrize("case", CASES, ids=list(CASES))
    def test_it_never_claims_a_quiet_watch(self, case):
        d = _digest(**self.CASES[case])
        assert "Quiet watch" not in d["summary"], d["summary"]
        assert "INCOMPLETE" in d["summary"], d["summary"]

    @pytest.mark.parametrize("case", CASES, ids=list(CASES))
    def test_the_rendered_digest_warns_before_the_sections(self, case):
        md = render_markdown(_digest(**self.CASES[case]))
        assert "do not read it as an all-clear" in md
        head = md.split("## ")[0]
        assert "⚠️" in head, "the warning must precede any section, not trail it"

    def test_the_reason_names_the_setting_an_operator_would_change(self):
        d = _digest(pool=_Pool(rows=[]), FLIGHT_RECORDER_ENABLED=False)
        assert "FLIGHT_RECORDER_ENABLED" in d["degraded_reasons"][0]

    def test_sqlite_mode_is_named_as_the_cause_first(self):
        """SQLite also makes the query raise; the configuration is the useful reason."""
        d = _digest(pool=_Pool(exc=RuntimeError("no such table")), USE_SQLITE=True)
        assert "SQLite" in d["degraded_reasons"][0]


class TestADisabledWatchtowerIsAlsoNamed:
    def test_it_is_degraded(self):
        d = _digest(_Pool(rows=[]), WATCHTOWER_ENABLED=False)
        assert d["degraded"] is True
        assert any("WATCHTOWER_ENABLED" in r for r in d["degraded_reasons"])

    def test_an_enabled_watchtower_adds_no_reason(self):
        d = _digest(_Pool(rows=[]), WATCHTOWER_ENABLED=True)
        assert not any("WATCHTOWER" in r for r in d["degraded_reasons"])


class TestTheDigestStillReportsWhatItCanRead:
    """Degraded must not mean suppressed — the readable sections still render."""

    def test_partial_data_survives_a_failed_second_query(self):
        rows = [{
            "episode_id": "auto-1", "kind": "finding",
            "payload": '{"playbook": "CrashLoopBackOff", "namespace": "prod", "object": "web-1"}',
            "created_at": __import__("datetime").datetime.now(__import__("datetime").UTC),
        }]

        class Half(_Pool):
            def __init__(self):
                super().__init__(rows=rows)
                self.calls = 0
            async def fetch(self, *a, **k):
                self.calls += 1
                if self.calls == 1:
                    return self.rows
                raise RuntimeError("episodes table gone")

        d = _digest(Half())
        assert len(d["findings"]) == 1, "the readable section must survive"
        assert d["degraded"] is True
        assert "1 finding(s)" in d["summary"], d["summary"]
        assert "CrashLoopBackOff" in render_markdown(d)
