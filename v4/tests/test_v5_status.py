"""GET /v5/status — v5 trust-plane observability endpoint."""
from __future__ import annotations

from app.api.v1.endpoints.v5_status import router
from app.autonomy.budget import disengage_kill_switch, engage_kill_switch
from app.core.config import settings
from fastapi import FastAPI
from starlette.testclient import TestClient


def _client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestV5Status:
    def teardown_method(self):
        disengage_kill_switch()

    def test_reports_version_and_flags(self, mocker):
        mocker.patch.object(settings, "CORTEX_V5_ENABLED", True)
        mocker.patch.object(settings, "KI_V5_HARNESS_FANOUT", True)
        body = _client().get("/v5/status").json()
        assert body["cortex_v5_enabled"] is True
        assert "KI_V5_HARNESS_FANOUT" in body["active_flags"]
        assert body["version"] and body["arm"]

    def test_reports_kill_switch(self, mocker):
        mocker.patch.object(settings, "KI_V5_KILL_SWITCH", False)
        engage_kill_switch()
        assert _client().get("/v5/status").json()["kill_switch_engaged"] is True

    def test_reports_change_freeze_and_spend_cap(self, mocker):
        mocker.patch.object(settings, "KI_V5_CHANGE_FREEZE", True)
        mocker.patch.object(settings, "KI_V5_SPEND_CAP_USD", 25.0)
        body = _client().get("/v5/status").json()
        assert body["change_freeze"] is True and body["spend_cap_usd"] == 25.0

    def test_baseline_when_all_off(self, mocker):
        mocker.patch.object(settings, "CORTEX_V5_ENABLED", False)
        disengage_kill_switch()
        mocker.patch.object(settings, "KI_V5_KILL_SWITCH", False)
        body = _client().get("/v5/status").json()
        assert body["cortex_v5_enabled"] is False and body["kill_switch_engaged"] is False
