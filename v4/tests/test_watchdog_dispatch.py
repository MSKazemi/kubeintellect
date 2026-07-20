"""Watchdog → fan-out dispatch (v5 P4 closed loop) — investigation wiring."""
from __future__ import annotations

import asyncio

from app.sensorium import change_watchdog
from app.sensorium.change_watchdog import WatchdogTask
from app.sensorium.watchdog_dispatch import _watchdog_state, install, investigate


def _task():
    return WatchdogTask(target="deploy/web", kind="image",
                        objective="A image change to deploy/web just happened — verify health.",
                        created_epoch=100.0, ttl_seconds=300, dedup_key="demo/image/deploy/web")


class TestState:
    def test_builds_investigate_state(self):
        s = _watchdog_state(_task())
        assert s["triage_mode"] == "investigate"
        assert s["messages"][0].content.startswith("A image change")
        assert s["investigation_plan"][0].status == "in_progress"
        assert s["session_id"] == "watchdog-demo/image/deploy/web"


class TestInvestigate:
    async def test_runs_the_runner_with_state(self):
        seen = {}
        async def fake_runner(state, config):
            seen["objective"] = state["messages"][0].content
            return {"messages": ["evidence"]}
        out = await investigate(_task(), runner=fake_runner)
        assert out == {"messages": ["evidence"]}
        assert "deploy/web" in seen["objective"]

    async def test_runner_failure_is_swallowed(self):
        async def boom(state, config):
            raise RuntimeError("fan-out died")
        assert await investigate(_task(), runner=boom) is None


class TestInstall:
    def teardown_method(self):
        change_watchdog.set_dispatch(change_watchdog._log_dispatch)

    async def test_install_wires_fire_and_forget_dispatch(self, mocker):
        ran = asyncio.Event()

        async def fake_runner(state, config):
            ran.set()
            return {}
        mocker.patch("app.cortex.harness.runner.run_fanout", new=fake_runner)
        install()
        # firing a task should schedule the investigation on the running loop
        change_watchdog.fire([_task()])
        await asyncio.wait_for(ran.wait(), timeout=1.0)
        assert ran.is_set()
