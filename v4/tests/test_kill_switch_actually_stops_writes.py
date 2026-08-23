"""The brakes an operator can see must be the brakes that actually hold.

`GET /v1/v5/status` reports `kill_switch_engaged` — annotated in the response model as
"⇒ all autonomous writes denied" — and `kq v5-status` prints it in red. Until 2026-08-20
`auto_write_permitted()` returned *allow* when `KI_V5_BLAST_RADIUS_BUDGET` was false, **before**
consulting the kill switch, and that flag defaults to False. So in the default configuration an
operator who broke glass mid-incident was shown a red, engaged kill switch by both the API and the
CLI while the watchtower went on auto-fixing. A declared change freeze was ignored the same way.

The property under test is not "the kill switch denies writes" (the old suite asserted that, but
only with the experimental flag forced on — a configuration nobody runs). It is the *consistency*
of the reported state with the actual behaviour, across the whole cross-product of the four inputs
that feed it: what /v5/status says is engaged must be what the watchtower obeys. A brake that
reports itself engaged while writes continue is worse than no brake, because it stops the operator
from reaching for a real one.

Related: `tests/test_budget_gate.py` covers the composable `gate_write` and the pure helpers.
"""
from __future__ import annotations

import itertools

import pytest
from app.api.v1.endpoints.v5_status import router
from app.autonomy import watchtower
from app.autonomy.budget import (
    auto_write_permitted,
    disengage_kill_switch,
    engage_kill_switch,
)
from app.core.config import settings
from app.detectors.models import Finding
from fastapi import FastAPI
from starlette.testclient import TestClient


def _status_client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _finding() -> Finding:
    return Finding(
        playbook="CrashLoopBackOff",
        cluster_id="cl-1",
        namespace="dev",
        object_name="pod/x",
        evidence="crashloop",
        severity="warning",
    )


@pytest.fixture(autouse=True)
def _clean_runtime_switch():
    """The kill switch is process-global; never let one case leak into the next."""
    disengage_kill_switch()
    yield
    disengage_kill_switch()


@pytest.fixture()
def a3_ready(mocker):
    """A finding the ladder would auto-fix, so the only thing left to say no is the brake."""
    mocker.patch.object(watchtower, "a3_allowed", return_value=True)


# (runtime_kill, settings_kill, change_freeze, blast_flag)
_COMBOS = list(itertools.product([False, True], repeat=4))


def _apply(mocker, runtime_kill, settings_kill, freeze, blast_flag):
    mocker.patch.object(settings, "KI_V5_KILL_SWITCH", settings_kill)
    mocker.patch.object(settings, "KI_V5_CHANGE_FREEZE", freeze)
    mocker.patch.object(settings, "KI_V5_BLAST_RADIUS_BUDGET", blast_flag)
    if runtime_kill:
        engage_kill_switch()


class TestTheReportedStateIsTheRealState:
    """What /v5/status shows must be what the write path obeys — in every configuration."""

    @pytest.mark.parametrize("combo", _COMBOS)
    def test_a_reported_brake_actually_denies_the_write(self, mocker, a3_ready, combo):
        runtime_kill, settings_kill, freeze, blast_flag = combo
        _apply(mocker, *combo)

        body = _status_client().get("/v5/status").json()
        brake_reported = body["kill_switch_engaged"] or body["change_freeze"]

        allowed = auto_write_permitted().allow
        auto_fixes = watchtower._should_auto_fix(_finding(), "A3")

        if brake_reported:
            assert not allowed, (
                f"/v5/status reported a brake engaged {body} but the budget gate allows the "
                f"write (config: runtime_kill={runtime_kill} settings_kill={settings_kill} "
                f"freeze={freeze} blast_radius_flag={blast_flag})"
            )
            assert not auto_fixes, (
                "the watchtower auto-fixed while /v5/status reported a brake engaged — the "
                "operator was told the agent had stopped writing"
            )
        else:
            assert allowed and auto_fixes, "nothing was engaged, yet the write was denied"

    @pytest.mark.parametrize("combo", _COMBOS)
    def test_the_gate_and_the_endpoint_never_disagree(self, mocker, combo):
        """Same property stated as an equivalence, so an over-blocking bug fails too."""
        _apply(mocker, *combo)
        body = _status_client().get("/v5/status").json()
        expect_denied = body["kill_switch_engaged"] or body["change_freeze"]
        assert auto_write_permitted().allow is (not expect_denied)


class TestBreakGlassInTheDefaultConfiguration:
    """The regression that started this: every case below ran with the flag at its default."""

    def test_the_runtime_kill_switch_stops_auto_fix_without_a_redeploy(self, mocker, a3_ready):
        mocker.patch.object(settings, "KI_V5_BLAST_RADIUS_BUDGET", False)  # the default
        mocker.patch.object(settings, "KI_V5_KILL_SWITCH", False)
        assert watchtower._should_auto_fix(_finding(), "A3") is True
        engage_kill_switch()
        assert watchtower._should_auto_fix(_finding(), "A3") is False

    def test_disengaging_restores_auto_fix(self, mocker, a3_ready):
        mocker.patch.object(settings, "KI_V5_BLAST_RADIUS_BUDGET", False)
        mocker.patch.object(settings, "KI_V5_KILL_SWITCH", False)
        engage_kill_switch()
        assert watchtower._should_auto_fix(_finding(), "A3") is False
        disengage_kill_switch()
        assert watchtower._should_auto_fix(_finding(), "A3") is True

    def test_the_settings_kill_switch_stops_auto_fix(self, mocker, a3_ready):
        mocker.patch.object(settings, "KI_V5_BLAST_RADIUS_BUDGET", False)
        mocker.patch.object(settings, "KI_V5_KILL_SWITCH", True)
        assert watchtower._should_auto_fix(_finding(), "A3") is False

    def test_a_declared_change_freeze_stops_auto_fix(self, mocker, a3_ready):
        mocker.patch.object(settings, "KI_V5_BLAST_RADIUS_BUDGET", False)
        mocker.patch.object(settings, "KI_V5_CHANGE_FREEZE", True)
        assert watchtower._should_auto_fix(_finding(), "A3") is False

    def test_the_denial_names_which_brake_fired(self, mocker):
        mocker.patch.object(settings, "KI_V5_BLAST_RADIUS_BUDGET", False)
        mocker.patch.object(settings, "KI_V5_CHANGE_FREEZE", False)
        engage_kill_switch()
        assert "kill switch" in auto_write_permitted().reason
        disengage_kill_switch()
        mocker.patch.object(settings, "KI_V5_CHANGE_FREEZE", True)
        assert "freeze" in auto_write_permitted().reason


class TestTheExperimentalFlagStillChangesNothingByItself:
    """The invariant the old tests were reaching for, stated so it cannot license a dead brake.

    `KI_V5_BLAST_RADIUS_BUDGET` gates the *spend/budget* machinery. Flipping it must not alter the
    ladder on its own — but it was never meant to be able to disable an operator's stop command.
    """

    @pytest.mark.parametrize("flag", [False, True])
    def test_with_no_brake_engaged_the_flag_does_not_touch_a3(self, mocker, a3_ready, flag):
        mocker.patch.object(settings, "KI_V5_BLAST_RADIUS_BUDGET", flag)
        mocker.patch.object(settings, "KI_V5_KILL_SWITCH", False)
        mocker.patch.object(settings, "KI_V5_CHANGE_FREEZE", False)
        assert auto_write_permitted().allow is True
        assert watchtower._should_auto_fix(_finding(), "A3") is True

    @pytest.mark.parametrize("flag", [False, True])
    def test_the_flag_cannot_re_enable_writes_an_operator_stopped(self, mocker, a3_ready, flag):
        mocker.patch.object(settings, "KI_V5_BLAST_RADIUS_BUDGET", flag)
        engage_kill_switch()
        assert watchtower._should_auto_fix(_finding(), "A3") is False


class TestTheBrakeOnlyBindsTheAgent:
    """04-trust §6: fail-closed for agent write authority, never for a human or a workload."""

    def test_a_non_a3_finding_is_unaffected_by_the_brake(self, mocker):
        mocker.patch.object(watchtower, "a3_allowed", return_value=False)
        engage_kill_switch()
        assert watchtower._should_auto_fix(_finding(), "A1") is False

    def test_the_gate_is_consulted_not_the_flag(self, mocker, a3_ready):
        """Wiring check: the watchtower must ask the budget module, not read the flag itself."""
        called = {"n": 0}
        real = auto_write_permitted

        def counting():
            called["n"] += 1
            return real()

        mocker.patch("app.autonomy.budget.auto_write_permitted", side_effect=counting)
        watchtower._should_auto_fix(_finding(), "A3")
        assert called["n"] == 1, "the watchtower did not consult the blast-radius gate at all"
