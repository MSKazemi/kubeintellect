"""`sensorium: disabled` named two causes as fact, for four unrelated situations.

THE DEFECT
----------
Four different paths end at `detectors.service._engine is None`, and `perception_gaps` described
all four with one sentence:

    "the sensorium is not running (SENSORIUM_ENABLED=false, or no compiled detectors loaded)
     — no detector finding could have been produced"

Driving each real path and reading the output showed the same string every time. For two of the
four that sentence is **false**, and they are the two that matter:

* **a start that raised** — `main.py` catches every exception from `start_sensorium` and logs one
  `WARNING` (correctly: perception failing must not cost availability). The operator reading
  `/v1/findings` or `kq digest` was then told their blindness was a configuration choice.
* **a leader-election standby** — a replica that lost the lock serves the API normally and
  watches nothing *by design*. Half the responses in a two-replica deployment carried an
  explanation naming two causes that were both untrue, and made a healthy standby read as an
  unmonitored cluster.

This module was already the product of an earlier audit — it correctly separates
`starting`/`active`/`stopped`/`reconnecting` from stream health. The hole was one level up: it
never asked *why* there was no engine at all.

WHAT IS ASSERTED
----------------
1. Each of the four paths yields its own reason, driven through the real code, not by setting
   the flag by hand.
2. The two that used to be lied about now say what they are: an outage says it is an outage,
   and a standby says another replica is perceiving.
3. A perceiving sensorium carries no reason at all — the vacuity guard, without which every
   assertion above is satisfied by a constant string.
4. `/v1/findings` carries it on both of its branches.
"""

from __future__ import annotations

import pytest
from app.detectors import perception, service
from app.detectors.perception import ACTIVE, DISABLED


@pytest.fixture(autouse=True)
def _clean_module_state():
    saved = (service._engine, service._absence, service._absence_detail)
    service._engine = None
    service._absence, service._absence_detail = service.NOT_STARTED, ""
    yield
    service._engine, service._absence, service._absence_detail = saved


class _Det:
    trend_predicates = None


class _Engine:
    """A real stub, not a Mock: `perception_state` reads `last_trend_error` off the engine and
    a Mock attribute there sends FastAPI's encoder into infinite recursion."""

    def __init__(self):
        self.detectors = [_Det()]
        self.last_trend_error = None
        self.trend_blind_since = None

    def recent_findings(self, **_kw):
        return []

    async def run(self):
        import asyncio
        await asyncio.sleep(3600)

    def process(self, _obs):
        ...


async def _drive_no_detectors(mocker):
    mocker.patch.object(service, "load_detectors", lambda: [])
    await service.start_sensorium()


class TestEachPathSaysWhichOneItWas:
    def test_switched_off(self):
        service.record_disabled_by_flag()
        st = perception.perception_state()
        assert st.sensorium == DISABLED
        assert "SENSORIUM_ENABLED=false" in st.sensorium_reason

    @pytest.mark.asyncio
    async def test_no_compiled_detectors(self, mocker):
        await _drive_no_detectors(mocker)
        st = perception.perception_state()
        assert "no compiled detectors" in st.sensorium_reason
        assert "SENSORIUM_ENABLED" not in st.sensorium_reason, (
            "naming the flag here is the original defect: nothing is switched off"
        )

    def test_a_failed_start_says_it_is_an_outage(self):
        """The lifespan swallows the exception on purpose; the reason must survive it."""
        service.record_start_failure(RuntimeError("kubectl not found on PATH"))
        st = perception.perception_state()
        reason = st.sensorium_reason
        assert "FAILED to start" in reason
        assert "kubectl not found on PATH" in reason, (
            "an outage with no cause sends the operator back to a startup WARNING that has "
            "already scrolled away"
        )
        assert "outage rather than a setting" in reason
        assert "SENSORIUM_ENABLED=false" not in reason, (
            "this is the false statement the fix exists to remove"
        )

    @pytest.mark.asyncio
    async def test_a_standby_says_another_replica_is_perceiving(self):
        """A standby is behaving correctly. Describing it as 'not running, probably switched
        off' turns a healthy two-replica deployment into a reported monitoring outage."""
        await service.stop_sensorium(service.STANDBY, "another replica holds the singleton lock")
        reason = perception.perception_state().sensorium_reason
        assert "standby" in reason
        assert "by design" in reason
        assert "SENSORIUM_ENABLED=false" not in reason
        assert "no compiled detectors" not in reason

    @pytest.mark.asyncio
    async def test_shutdown_is_its_own_reason(self):
        await service.stop_sensorium()
        assert "shutting down" in perception.perception_state().sensorium_reason

    def test_before_startup_is_its_own_reason(self):
        assert "not started yet" in perception.perception_state().sensorium_reason

    @pytest.mark.asyncio
    async def test_the_four_reasons_are_all_different(self, mocker):
        """The property the single sentence violated, stated directly."""
        seen = []
        service.record_disabled_by_flag()
        seen.append(perception.perception_state().sensorium_reason)
        service._engine = None
        await _drive_no_detectors(mocker)
        seen.append(perception.perception_state().sensorium_reason)
        service.record_start_failure(RuntimeError("boom"))
        seen.append(perception.perception_state().sensorium_reason)
        await service.stop_sensorium(service.STANDBY, "lock held elsewhere")
        seen.append(perception.perception_state().sensorium_reason)
        assert len(set(seen)) == 4, f"causes collapsed onto the same sentence: {seen}"
        assert all(seen), "an empty reason is the old silence in a new field"


class TestTheLifespanActuallyRecordsThem:
    """Two of the four reasons are set by the app lifespan, not by this module, and testing the
    setters alone leaves the wiring unasserted — a mutation run proved it: deleting either call
    from `main.py` left every other test in this file green. These read the source structurally
    (AST, not a text scan) because the two functions are closures inside `lifespan` and cannot
    be imported.
    """

    @staticmethod
    def _fn(name):
        import ast
        import inspect

        import app.main as main_mod
        tree = ast.parse(inspect.getsource(main_mod))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
                return node
        raise AssertionError(f"{name} no longer exists in app/main.py")

    def test_a_swallowed_start_failure_is_recorded(self):
        import ast
        src = ast.unparse(self._fn("_start_singleton_workers"))
        assert "record_start_failure" in src, (
            "the lifespan swallows every exception from start_sensorium; without this call "
            "the outage is invisible and perception reports it as a configuration choice"
        )

    def test_losing_the_lock_is_recorded_as_standby(self):
        import ast
        src = ast.unparse(self._fn("_stop_singleton_workers"))
        assert "STANDBY" in src, (
            "on_lose must say *why* it stopped, or a healthy standby reads as an "
            "unmonitored cluster"
        )

    def test_the_flag_path_is_recorded(self):
        import ast
        src = ast.unparse(self._fn("_start_singleton_workers"))
        assert "record_disabled_by_flag" in src


class TestAPerceivingSensoriumCarriesNoReason:
    """Vacuity guard for the whole file: every assertion above would also pass if the field
    were a constant string, so the running case must produce none."""

    @pytest.mark.asyncio
    async def test_a_successful_start_clears_the_absence(self, mocker):
        """`sensorium_absence()` promises `RUNNING` when the engine is present. Nothing asserted
        it, so the module could carry a stale reason forever and only a direct caller would see."""
        mocker.patch.object(service, "load_detectors", lambda: [_Det()])
        mocker.patch.object(service, "DetectorEngine", lambda **_kw: _Engine())
        import app.sensorium.k8s_watcher as watcher
        mocker.patch.object(watcher, "start_watchers", mocker.AsyncMock(return_value=[]))
        service.record_start_failure(RuntimeError("an earlier attempt failed"))

        await service.start_sensorium()

        assert service.sensorium_absence() == (service.RUNNING, "")
        for task in service._tasks:
            task.cancel()
        service._tasks.clear()

    def test_no_reason_while_watching(self, mocker):
        engine = _Engine()
        import app.sensorium.k8s_watcher as watcher
        mocker.patch.object(watcher, "stream_health", lambda: [{"name": "pods", "stopped": False}])
        mocker.patch.object(watcher, "any_stream_connected", lambda: True)
        st = perception.perception_state(engine)
        assert st.sensorium == ACTIVE
        assert st.sensorium_reason == ""
        assert perception.perception_gaps(st) == []


class TestTheFindingsEndpointCarriesIt:
    def test_both_branches_carry_the_reason(self, mocker):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import app.api.v1.endpoints.findings as findings_mod

        app = FastAPI()
        app.include_router(findings_mod.router)
        client = TestClient(app)

        service.record_start_failure(RuntimeError("kubectl not found on PATH"))
        body = client.get("/findings").json()
        assert body["sensorium"] == DISABLED
        assert "FAILED to start" in body["sensorium_reason"], (
            "the short-circuit branch dropped the reason, so the API said 'disabled' "
            "with no way to ask why"
        )

        engine = _Engine()
        mocker.patch.object(findings_mod, "get_engine", lambda: engine)
        import app.sensorium.k8s_watcher as watcher
        mocker.patch.object(watcher, "stream_health", lambda: [{"name": "pods", "stopped": False}])
        mocker.patch.object(watcher, "any_stream_connected", lambda: True)
        body = client.get("/findings").json()
        assert body["sensorium"] == ACTIVE
        assert body["sensorium_reason"] == ""
