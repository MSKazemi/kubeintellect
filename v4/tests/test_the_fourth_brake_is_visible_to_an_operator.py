"""A brake an operator cannot see is a brake they cannot rely on.

Pass 271 put a fourth gate on the autonomous-write path: with `KI_V5_STATISTICAL_PROMOTION` on,
the watchtower asks `promotion_outcomes` whether `watchtower-autofix` still holds its A3 authority
and closes the gate when the record has collapsed. `/v5/status` — the surface whose docstring
promises "which v5 slices are active, and whether the fail-closed brakes are engaged" — listed the
other three (kill switch, change freeze, spend cap) and could not show this one at all.

Two failures fell out of that, and the second is the sharper:

  * the flag appeared under `active_flags` with nothing saying which **direction** it acts in. A
    switch named *statistical promotion* reported as active reads as *rungs are being earned here*,
    which is the one thing this build does not do;
  * `degraded_experimental_flags()` — the function that exists precisely to catch "you set it and
    nothing happened, because the subsystem is dead" — filtered on the `MEMORY_` prefix, and both
    halves of this flag read through the memory hierarchy's pool. Flag on, hierarchy down: the
    status surface said active, and the A3 gate was governed by the allowlist alone.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.autonomy import promotion_source
from app.autonomy.promotion_stats import Event
from app.api.v1.endpoints.v5_status import router as v5_status_router
from app.core import version
from app.core.config import settings
from app.memory import service


def _now_days() -> float:
    """The endpoint stamps the window from the wallclock; fixtures have to sit inside it."""
    import time

    return time.time() / 86400.0


def clean(n: int, *, end: float | None = None) -> list[Event]:
    end = _now_days() if end is None else end
    return [Event(ts_days=end - (n - i) * 0.01, success=True, incident_id=f"inc-{i}",
                  incident_type="CrashLoopBackOff") for i in range(n)]


class FakePool:
    def __init__(self, events: list[Event] | None = None, raises: Exception | None = None):
        self._events, self._raises = events or [], raises

    async def fetch(self, sql, *args):
        if self._raises:
            raise self._raises
        return [{"ts_days": e.ts_days, "success": e.success, "incident_id": e.incident_id,
                 "incident_type": e.incident_type, "critical": e.critical} for e in self._events]


@pytest.fixture
def client():
    # Only the router under test — the full app's lifespan reaches for Postgres.
    app = FastAPI()
    app.include_router(v5_status_router)
    return TestClient(app)


def status(client) -> dict:
    response = client.get("/v5/status")
    assert response.status_code == 200
    return response.json()["autonomy_promotion"]


class TestTheBlockIsThere:
    def test_the_surface_reports_the_fourth_brake_at_all(self, client):
        assert "autonomy_promotion" in client.get("/v5/status").json()

    def test_it_names_the_direction_the_flag_does_not(self, client):
        """`active_flags: [KI_V5_STATISTICAL_PROMOTION]` cannot say 'revoke-only'. This can."""
        assert status(client)["direction"] == "revoke-only"

    def test_it_names_the_action_class_so_the_row_can_be_looked_up(self, client):
        assert status(client)["action_class"] == promotion_source.WATCHTOWER_AUTOFIX


class TestEnabledAndOperatingAreDifferentQuestions:
    def test_flag_off_is_not_enabled_and_not_operating(self, client, mocker):
        mocker.patch.object(settings, "KI_V5_STATISTICAL_PROMOTION", False)
        block = status(client)
        assert block["enabled"] is False and block["operating"] is False
        assert block["reason"] == "flag off"

    def test_flag_on_with_no_store_is_enabled_but_NOT_operating(self, client, mocker):
        """The footgun this block exists for: a promotion flag set on a box without Postgres."""
        mocker.patch.object(settings, "KI_V5_STATISTICAL_PROMOTION", True)
        mocker.patch.object(service, "_pool", None)
        block = status(client)
        assert block["enabled"] is True
        assert block["operating"] is False
        assert "not operating" in block["reason"] and "allowlist" in block["reason"]

    def test_an_unreadable_store_is_not_operating_and_not_clean(self, client, mocker):
        mocker.patch.object(settings, "KI_V5_STATISTICAL_PROMOTION", True)
        mocker.patch.object(service, "_pool", FakePool(raises=RuntimeError("db down")))
        block = status(client)
        assert block["operating"] is False
        assert "unreadable" in block["reason"]
        assert block["authority_revoked"] is False   # reported as unknown-by-not-operating …
        assert block["samples"] == 0                 # … never as a clean record


class TestItReportsWhatTheRecordSays:
    @pytest.fixture(autouse=True)
    def _on(self, mocker):
        mocker.patch.object(settings, "KI_V5_STATISTICAL_PROMOTION", True)

    def test_a_clean_record_is_operating_and_not_revoked(self, client, mocker):
        mocker.patch.object(service, "_pool", FakePool(clean(40)))
        block = status(client)
        assert block["operating"] is True and block["authority_revoked"] is False
        assert block["samples"] == 40

    def test_a_collapsed_record_shows_the_gate_shut_and_why(self, client, mocker):
        now = _now_days()
        events = clean(30, end=now - 1.0) + [
            Event(ts_days=now - 0.5, success=False, incident_id="x"),
            Event(ts_days=now - 0.2, success=False, incident_id="y")]
        mocker.patch.object(service, "_pool", FakePool(events))
        block = status(client)
        assert block["authority_revoked"] is True
        assert "CUSUM" in block["reason"]

    def test_samples_is_the_window_not_the_table(self, client, mocker):
        """Every ADR-102 threshold is measured against the rolling window; reporting the table
        size would overstate the evidence behind the verdict on the same line."""
        from app.autonomy.promotion_stats import WINDOW_MAX_EVENTS

        mocker.patch.object(service, "_pool", FakePool(clean(WINDOW_MAX_EVENTS + 50)))
        assert status(client)["samples"] == WINDOW_MAX_EVENTS


class TestTheFlagIsReportedDegradedWhenItCannotAct:
    def test_it_is_degraded_when_the_hierarchy_is_not_ready(self, mocker):
        mocker.patch.object(settings, "KI_V5_STATISTICAL_PROMOTION", True)
        mocker.patch("app.memory.service.memory_status",
                     lambda: {"state": "unavailable", "enabled": False})
        assert "KI_V5_STATISTICAL_PROMOTION" in version.degraded_experimental_flags()

    def test_it_is_not_degraded_when_the_hierarchy_is_ready(self, mocker):
        mocker.patch.object(settings, "KI_V5_STATISTICAL_PROMOTION", True)
        mocker.patch("app.memory.service.memory_status", lambda: {"state": "ready", "enabled": True})
        assert version.degraded_experimental_flags() == []

    def test_it_stays_in_the_active_set(self, mocker):
        """Degraded is not unwired: rollout identity must not flap on a Postgres blip."""
        mocker.patch.object(settings, "KI_V5_STATISTICAL_PROMOTION", True)
        mocker.patch("app.memory.service.memory_status", lambda: {"state": "unavailable"})
        assert "KI_V5_STATISTICAL_PROMOTION" in version.active_experimental_flags()

    def test_the_dependency_is_listed_explicitly_not_matched_by_prefix(self):
        """A pattern would silently adopt the next `KI_V5_*` flag; this must be a deliberate edit."""
        assert version._HIERARCHY_DEPENDENT == frozenset({"KI_V5_STATISTICAL_PROMOTION"})


class TestTheEngineNoLongerClaimsWhatItDoesNotDo:
    def test_the_docstring_does_not_advertise_earned_autonomy_as_shipped(self):
        from app.autonomy import promotion_engine

        doc = " ".join((promotion_engine.__doc__ or "").split())   # the prose is line-wrapped
        assert "never grant it" in doc
        # The old sentence is quoted, so a bare substring check would pass on the claim itself.
        # What must be true is that every occurrence sits inside the correction.
        assert doc.count("no published tool ships") == 1
        assert "used to claim otherwise" in doc

    def test_it_says_which_half_ships(self):
        from app.autonomy import promotion_engine

        assert "DOWN half only" in (promotion_engine.__doc__ or "")
