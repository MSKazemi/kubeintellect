"""The break-glass page promised a stop button the product exposes nowhere.

`docs/autonomy.md#stopping-the-agent-break-glass` is the page an operator reads *during an
incident*. Until 2026-08-20 it said the two brakes deny every autonomous write **"without a
redeploy"**, and listed the kill switch as engageable by *"`KI_V5_KILL_SWITCH=true`, or the runtime
toggle (no restart)"*.

Measured 2026-08-20 — every operator surface the product has:

    API routes (OpenAPI, all 19)      no path matching kill/stop/freeze/brake/pause/halt
    kq commands                       v5-status *reports* the brakes; nothing sets one
    Helm values                       the ConfigMap is an explicit key allowlist; neither
                                      brake appears, and there is no extraEnv/range escape
    engage_kill_switch() callers      none in app/ outside budget.py (tests + one probe only)
    settings after start              os.environ["KI_V5_KILL_SWITCH"]="true" leaves
                                      settings.KI_V5_KILL_SWITCH False and
                                      kill_switch_engaged() False — read once at import

So there was no way to engage either brake without restarting the process, which is the redeploy
the page said you did not need. The in-process `engage_kill_switch()` is real but unreachable, and
it sets a module global in **one** process — even wired to a route it would stop only the replica
that served the request.

Nothing was wrong with the *gates*; passes 102–104 left those correct. What was wrong was the
sentence, on the one page whose reader has no time to check it. Fixed by describing what exists —
set the env var and roll the pods — and by saying plainly what is missing, so the gap is a known
one rather than a discovery made at 3am.

This file is the gate that keeps the sentence honest: the claim may come back the moment the
mechanism does, and not before.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from app.api.v1.endpoints.v5_status import router
from app.autonomy.budget import (
    change_freeze_active,
    disengage_kill_switch,
    engage_kill_switch,
    kill_switch_engaged,
)
from app.core.config import settings
from fastapi import FastAPI
from starlette.testclient import TestClient

_ROOT = Path(__file__).resolve().parents[1]
_APP = _ROOT / "packages" / "kubeintellect-server" / "app"
_AUTONOMY_DOC = _ROOT / "docs" / "autonomy.md"
_CHART = _ROOT / "deploy" / "helm" / "kubeintellect"

#: Phrases that promise engaging a brake needs no process restart. Allowed back only when the
#: runtime toggle actually has an operator surface — see `_runtime_toggle_is_exposed`.
_NO_RESTART_CLAIMS = [
    "without a redeploy",
    "runtime toggle (no restart)",
    "no restart",
    "without a restart",
    "without restarting",
]

_BRAKE_RE = re.compile(r"kill|stop|freeze|brake|pause|halt", re.I)


def _break_glass_section() -> str:
    text = _AUTONOMY_DOC.read_text()
    start = text.index("## Stopping the agent")
    rest = text[start + 3:]
    end = rest.index("\n## ") if "\n## " in rest else len(rest)
    return text[start:start + 3 + end]


def _runtime_toggle_is_exposed() -> bool:
    """True once anything in production calls the in-process toggle.

    The whole point of the gate: build the endpoint and the documentation claim becomes legal
    again automatically, with no test to remember to unpick.
    """
    return any(
        "engage_kill_switch" in p.read_text()
        for p in _APP.rglob("*.py")
        if p.name != "budget.py"
    )


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestThePageClaimsOnlyWhatExists:

    @pytest.mark.parametrize("claim", _NO_RESTART_CLAIMS)
    def test_no_no_restart_promise_while_the_toggle_has_no_surface(self, claim):
        if _runtime_toggle_is_exposed():
            pytest.skip("the runtime toggle now has a production caller — the claim is legal")
        assert claim not in _break_glass_section(), (
            f"the break-glass page promises {claim!r}, but nothing outside tests can engage a "
            "brake without restarting the process"
        )

    def test_the_page_says_the_settings_are_read_at_start(self):
        section = _break_glass_section()
        assert "once, when the process starts" in section

    def test_the_page_gives_a_recipe_that_works(self):
        section = _break_glass_section()
        assert "kubectl set env" in section and "KI_V5_KILL_SWITCH=true" in section
        assert "kq v5-status" in section, "an operator must be told how to confirm it took"

    def test_the_recipe_names_the_deployment_the_chart_actually_creates(self):
        """`fullname` defaults to `.Chart.Name`, so the recipe is only right if it matches it."""
        chart_name = re.search(r"^name:\s*(\S+)", (_CHART / "Chart.yaml").read_text(), re.M)
        assert chart_name, "chart has no name"
        helpers = (_CHART / "templates" / "_helpers.tpl").read_text()
        assert "default .Chart.Name .Values.fullnameOverride" in helpers, (
            "the fullname helper changed — recheck the deployment name in the break-glass recipe"
        )
        assert f"deploy/{chart_name.group(1)}" in _break_glass_section()

    def test_the_page_states_the_missing_surfaces(self):
        section = _break_glass_section()
        for missing in ("no API route", "`kq` command"):
            assert missing in section, f"the page does not say {missing!r} is absent"

    def test_the_module_docstring_records_the_missing_surface(self):
        """The same false claim lived in the code too, and that is where the next reader looks."""
        src = (_APP / "autonomy" / "budget.py").read_text()
        head = src[:src.index('"""', 3) + 3]
        assert "no operator surface" in head, (
            "budget.py still describes the runtime toggle as an operator break-glass; nothing "
            "outside tests can reach it"
        )

    def test_the_page_states_the_per_replica_caveat(self):
        assert "replica" in _break_glass_section(), (
            "a module-global toggle behaves per-pod; an operator must know that before relying "
            "on it"
        )


class TestTheSurfacesReallyAreAbsent:
    """The premise of the rewrite. If one of these starts failing, the page can say more."""

    def test_no_api_route_engages_a_brake(self):
        from app.main import app as real_app
        paths = list(real_app.openapi()["paths"])
        assert paths, "no routes collected — the check would pass vacuously"
        offenders = [p for p in paths if _BRAKE_RE.search(p)]
        assert offenders == [], f"a brake route exists now: {offenders}"

    def test_the_status_route_is_read_only(self):
        from app.main import app as real_app
        methods = {m.upper() for m in real_app.openapi()["paths"]["/v1/v5/status"]}
        assert methods == {"GET"}

    def test_no_production_caller_of_the_runtime_toggle(self):
        callers = sorted(
            p.relative_to(_APP).as_posix()
            for p in _APP.rglob("*.py")
            if "engage_kill_switch" in p.read_text() and p.name != "budget.py"
        )
        assert callers == [], (
            f"the toggle is now reachable from {callers} — update the break-glass page, the "
            "budget.py module docstring, and delete this test"
        )

    def test_a_brake_cannot_be_set_through_helm_values(self):
        """The property, asserted directly rather than through the absence of a mechanism.

        This began as `assert "extraEnv" not in templates` — a proxy that held only while the
        ConfigMap was a closed key allowlist. It stopped holding on 2026-08-23, when
        `config.extraEnv` was added so an experiment arm's additive default-off flags
        (`MEMORY_*`, `KI_V5_*`, ADR-019) could be deployed by Helm at all; without it those flags
        were reachable only by editing the ConfigMap out of band, which the next `helm upgrade`
        silently reverted.

        The proxy changed. The property did not: carrying an experiment flag and operating a
        safety brake are different jobs, and `docs/autonomy.md` is the page an operator reads
        during an incident. A second, undocumented way to engage a brake that works only through
        a values file is precisely what nobody finds at 3am. So the template refuses both brake
        keys by name, and this test asserts that refusal — which is stronger than the old
        assertion, because it survives the next person adding a third escape hatch.
        """
        templates = "\n".join(p.read_text() for p in (_CHART / "templates").glob("*.yaml"))
        configmap = (_CHART / "templates" / "configmap.yaml").read_text()

        # Neither brake may be rendered as a ConfigMap key by any template.
        for brake in ("KI_V5_KILL_SWITCH", "KI_V5_CHANGE_FREEZE"):
            assert f"{brake}:" not in templates, f"{brake} is templated as a ConfigMap key"

        # The only loop over caller-supplied keys must refuse them, loudly, before emitting.
        assert "range $key, $value := .Values.config.extraEnv" in configmap, (
            "the extraEnv loop was renamed or removed — re-check that the guard still covers it"
        )
        guard = configmap.split("range $key, $value := .Values.config.extraEnv", 1)[1]
        emit = guard.index("{{ $key }}:")
        assert "fail" in guard[:emit], "extraEnv emits a key before checking it against the brakes"
        for brake in ("KI_V5_KILL_SWITCH", "KI_V5_CHANGE_FREEZE"):
            assert brake in guard[:emit], f"the extraEnv guard does not name {brake}"

        # No template may loop over the whole config map, which would bypass the named guard.
        assert "range $key, $value := .Values.config }}" not in templates


class TestTheSettingsAreReadOnceAtStart:
    """Why a restart is unavoidable today — the claim the rewritten page rests on."""

    @pytest.mark.parametrize("flag", ["KI_V5_KILL_SWITCH", "KI_V5_CHANGE_FREEZE"])
    def test_setting_the_env_var_after_start_does_not_move_the_gate(self, flag, monkeypatch):
        assert getattr(settings, flag) is False, "test needs the flag off to start"
        monkeypatch.setitem(os.environ, flag, "true")
        assert getattr(settings, flag) is False, (
            "settings picked the env var up live — the break-glass page can say so"
        )

    def test_neither_reader_moves_either(self, monkeypatch):
        disengage_kill_switch()
        monkeypatch.setitem(os.environ, "KI_V5_KILL_SWITCH", "true")
        monkeypatch.setitem(os.environ, "KI_V5_CHANGE_FREEZE", "true")
        assert kill_switch_engaged() is False and change_freeze_active() is False

    def test_the_in_process_toggle_does_move_the_gate(self):
        """It works — it is just unreachable. That distinction is the finding."""
        disengage_kill_switch()
        assert kill_switch_engaged() is False
        engage_kill_switch()
        assert kill_switch_engaged() is True
        disengage_kill_switch()
        assert kill_switch_engaged() is False


class TestTheStatusSurfaceUsesTheSameReadersAsTheGates:
    """104 gave the freeze a shared reader; the reporting surface still read a raw source."""

    def teardown_method(self):
        disengage_kill_switch()

    def test_the_reported_freeze_goes_through_the_shared_reader(self, mocker):
        """Patch the reader; the endpoint follows — as it already did for the kill switch."""
        mocker.patch.object(settings, "KI_V5_CHANGE_FREEZE", False)
        mocker.patch("app.api.v1.endpoints.v5_status.change_freeze_active", return_value=True)
        assert _client().get("/v5/status").json()["change_freeze"] is True, (
            "/v1/v5/status reads a brake source directly instead of its reader"
        )

    def test_the_reported_kill_switch_goes_through_its_reader(self, mocker):
        mocker.patch.object(settings, "KI_V5_KILL_SWITCH", False)
        mocker.patch("app.api.v1.endpoints.v5_status.kill_switch_engaged", return_value=True)
        assert _client().get("/v5/status").json()["kill_switch_engaged"] is True

    @pytest.mark.parametrize("declared", [True, False])
    def test_the_declared_flag_is_still_reported_verbatim(self, mocker, declared):
        """The behaviour that already worked must survive the indirection."""
        mocker.patch.object(settings, "KI_V5_CHANGE_FREEZE", declared)
        assert _client().get("/v5/status").json()["change_freeze"] is declared

    def test_what_is_reported_is_what_the_write_gates_enforce(self, mocker):
        from app.autonomy.budget import auto_write_permitted, gate_write
        for declared in (True, False):
            mocker.patch.object(settings, "KI_V5_CHANGE_FREEZE", declared)
            body = _client().get("/v5/status").json()
            assert body["change_freeze"] is declared
            assert gate_write().allow is (not declared)
            assert auto_write_permitted().allow is (not declared)
