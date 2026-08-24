"""A server that audits nothing must not report that it is fine.

THE DEFECT
----------
`init_audit_pool` caught every connection error, logged one WARNING and left `_pool = None`.
On Kubernetes the API pod is routinely scheduled before Postgres accepts connections, so that
one failed attempt was the *expected* case — and it disabled the audit log for the entire life
of the process. Every request after it, including every privileged one, hit `if _pool is None:
return` and vanished. There was no retry, so only a restart could ever fix it.

Reproduced before the fix: three admin requests dropped, no exception, no log line, and

* `/healthz` answered `status="ok"` with no field about the audit log at all,
* `kubeintellect status` had no audit row,
* `request_log` was empty — indistinguishable from a server that took no traffic,
* the module exported no accessor to ask with.

`docs/operations.md` said "Every API request is recorded fire-and-forget in `request_log`",
and `app/core/leader.py` justified the advisory-lock design on the premise that
"`init_audit_pool` exits the process when [Postgres] is unreachable". It never did — main.py's
`sys.exit(1)` wrapper around it is unreachable, because the function catches everything.

WHAT IS ASSERTED
----------------
1. The state is *askable*: `audit_status()` distinguishes ready / sqlite / unavailable and says
   why — asserted in both directions, because a status that always says "down" is as useless
   as one that always says "ok".
2. The outage is not permanent: the connect is retried from the write path, throttled to
   `_RETRY_INTERVAL_S`, and never retried in SQLite mode (there is nothing to connect to).
3. Every unrecorded request is counted, including a row the database rejects — and a
   *successful* write moves no counter.
4. `/healthz` carries it, so the outage survives the startup log scrolling away.
"""

from __future__ import annotations

import pytest
from app.api.v1.endpoints.health import router as health_router
from app.core import readiness
from app.core.config import settings
from app.db import audit
from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakePool:
    """Records what was written; `fail_with` makes the database reject the row."""

    def __init__(self, fail_with: Exception | None = None):
        self.rows: list[tuple] = []
        self.fail_with = fail_with
        self.closed = False

    async def execute(self, *args):
        if self.fail_with:
            raise self.fail_with
        self.rows.append(args)

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _clean_module_state():
    """Module-level state is shared; every test starts from a cold process."""
    saved = (audit._pool, audit._state, audit._reason, audit._dropped, audit._last_attempt)
    audit._pool, audit._state, audit._reason = None, "starting", ""
    audit._dropped, audit._last_attempt = 0, 0.0
    yield
    audit._pool, audit._state, audit._reason, audit._dropped, audit._last_attempt = saved


@pytest.fixture
def postgres_down(mocker):
    """A real deployment on Postgres, with Postgres refusing connections."""
    mocker.patch.object(settings, "USE_SQLITE", False)
    return mocker.patch.object(
        audit.asyncpg, "create_pool",
        mocker.AsyncMock(side_effect=OSError("Connect call failed ('10.0.0.5', 5432)")),
    )


@pytest.fixture
def postgres_up(mocker):
    mocker.patch.object(settings, "USE_SQLITE", False)
    pool = FakePool()
    mocker.patch.object(audit.asyncpg, "create_pool", mocker.AsyncMock(return_value=pool))
    return pool


async def _one_request(**over):
    kw = dict(request_id="r1", session_id="s1", user_id="u1", user_role="admin",
              path="/v1/chat/completions", method="POST", status_code=200, duration_ms=12.0)
    kw.update(over)
    await audit.log_request(**kw)


# ── 1. the state is askable ───────────────────────────────────────────────────
class TestTheAuditStateIsAskable:
    @pytest.mark.asyncio
    async def test_a_failed_connect_reports_disabled_and_why(self, postgres_down):
        await audit.init_audit_pool()
        st = audit.audit_status()
        assert st["enabled"] is False, "the audit log is dead and status says it is enabled"
        assert st["state"] == "unavailable"
        assert "10.0.0.5" in st["reason"], (
            "the reason must carry the actual failure — 'unavailable' with no cause "
            "sends an operator back to a startup log that has already rotated"
        )

    @pytest.mark.asyncio
    async def test_a_working_pool_reports_enabled(self, postgres_up):
        """Vacuity guard: a status that always says 'down' proves nothing about the failure case."""
        await audit.init_audit_pool()
        st = audit.audit_status()
        assert st["enabled"] is True
        assert st["state"] == "ready"
        assert st["reason"] == ""

    @pytest.mark.asyncio
    async def test_sqlite_mode_is_a_distinct_state_not_an_outage(self, mocker):
        """Configured-off and broken are different facts; collapsing them pages someone at 3am."""
        mocker.patch.object(settings, "USE_SQLITE", True)
        await audit.init_audit_pool()
        st = audit.audit_status()
        assert st["enabled"] is False
        assert st["state"] == "sqlite"
        assert st["state"] != "unavailable"
        assert "USE_SQLITE" in st["reason"]

    @pytest.mark.asyncio
    async def test_closing_the_pool_stops_claiming_enabled(self, postgres_up):
        await audit.init_audit_pool()
        assert audit.audit_status()["enabled"] is True
        await audit.close_audit_pool()
        assert audit.audit_status()["enabled"] is False, (
            "a shut-down pool still reporting 'enabled' is the same lie, one lifecycle later"
        )


# ── 2. the outage is not permanent ────────────────────────────────────────────
class TestTheOutageIsNotPermanent:
    @pytest.mark.asyncio
    async def test_a_startup_race_heals_without_a_restart(self, mocker, postgres_down):
        """The whole point: Postgres arriving late must not cost the process its audit log."""
        await audit.init_audit_pool()
        await _one_request()
        assert audit.audit_status()["enabled"] is False

        pool = FakePool()
        mocker.patch.object(audit.asyncpg, "create_pool", mocker.AsyncMock(return_value=pool))
        audit._last_attempt -= audit._RETRY_INTERVAL_S + 1        # the interval elapses
        await _one_request(request_id="r2")

        assert audit.audit_status()["enabled"] is True
        assert len(pool.rows) == 1, (
            "the request that triggered the reconnect must itself be recorded, "
            "not spent on the handshake"
        )

    @pytest.mark.asyncio
    async def test_the_retry_is_throttled(self, postgres_down):
        """Without a throttle, a Postgres outage turns every request into a connect attempt."""
        await audit.init_audit_pool()
        assert postgres_down.call_count == 1
        for i in range(5):
            await _one_request(request_id=f"r{i}")
        assert postgres_down.call_count == 1, (
            f"{postgres_down.call_count} connect attempts for 5 requests — the interval is not held"
        )

    @pytest.mark.asyncio
    async def test_the_throttle_expires(self, postgres_down):
        """Vacuity guard for the test above: the throttle must be a delay, not a permanent stop."""
        await audit.init_audit_pool()
        audit._last_attempt -= audit._RETRY_INTERVAL_S + 1
        await _one_request()
        assert postgres_down.call_count == 2

    @pytest.mark.asyncio
    async def test_sqlite_mode_never_dials_postgres(self, mocker):
        connect = mocker.patch.object(audit.asyncpg, "create_pool", mocker.AsyncMock())
        mocker.patch.object(settings, "USE_SQLITE", True)
        await audit.init_audit_pool()
        await _one_request()
        assert connect.call_count == 0, (
            "SQLite mode has no Postgres to reach; retrying it is a per-request DNS lookup "
            "for a server that is not meant to exist"
        )


# ── 3. every unrecorded request is counted ────────────────────────────────────
class TestEveryUnrecordedRequestIsCounted:
    @pytest.mark.asyncio
    async def test_dropped_requests_are_counted(self, postgres_down):
        await audit.init_audit_pool()
        for i in range(3):
            await _one_request(request_id=f"r{i}")
        assert audit.audit_status()["dropped"] == 3, (
            "'the table looks empty' is a guess; the count is the fact that replaces it"
        )

    @pytest.mark.asyncio
    async def test_a_recorded_request_is_not_counted_as_dropped(self, postgres_up):
        """Vacuity guard: a counter that only ever goes up measures traffic, not loss."""
        await audit.init_audit_pool()
        await _one_request()
        assert len(postgres_up.rows) == 1
        assert audit.audit_status()["dropped"] == 0

    @pytest.mark.asyncio
    async def test_a_row_the_database_rejects_is_also_a_drop(self, mocker):
        """It reached the pool and still was not recorded — that is the same loss."""
        mocker.patch.object(settings, "USE_SQLITE", False)
        pool = FakePool(fail_with=RuntimeError('relation "request_log" does not exist'))
        mocker.patch.object(audit.asyncpg, "create_pool", mocker.AsyncMock(return_value=pool))
        await audit.init_audit_pool()
        await _one_request()
        assert audit.audit_status()["enabled"] is True     # the pool is fine; the write was not
        assert audit.audit_status()["dropped"] == 1

    @pytest.mark.asyncio
    async def test_the_silence_is_broken_at_least_once(self, postgres_down, caplog):
        """One WARNING at boot is not a signal — the drop itself has to say something."""
        await audit.init_audit_pool()
        caplog.clear()
        with caplog.at_level("WARNING"):
            await _one_request()
        assert any("not recorded in request_log" in r.message for r in caplog.records), (
            f"nothing warned that a request went unaudited: {[r.message for r in caplog.records]}"
        )

    @pytest.mark.asyncio
    async def test_the_warning_does_not_flood(self, postgres_down, caplog):
        """Vacuity guard for the test above: per-request warnings are their own outage."""
        await audit.init_audit_pool()
        caplog.clear()
        with caplog.at_level("WARNING"):
            for i in range(20):
                await _one_request(request_id=f"r{i}")
        drops = [r for r in caplog.records if "not recorded in request_log" in r.message]
        assert len(drops) == 1, f"20 dropped requests produced {len(drops)} warnings"


# ── 4. the probe carries it ───────────────────────────────────────────────────
@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(health_router)
    readiness.set_ready(True)
    return TestClient(app)


class TestHealthzCarriesIt:
    def test_healthz_reports_a_dead_audit_log(self, client, mocker):
        mocker.patch.object(audit, "_state", "unavailable")
        mocker.patch.object(audit, "_reason", "Connect call failed ('10.0.0.5', 5432)")
        mocker.patch.object(audit, "_dropped", 41)
        body = client.get("/healthz").json()
        assert body["audit"]["enabled"] is False, (
            "/healthz answered 'ok' for a server auditing nothing — the exact signal "
            "an operator uses to decide the deployment is healthy"
        )
        assert body["audit"]["dropped"] == 41
        assert "10.0.0.5" in body["audit"]["reason"]

    def test_healthz_reports_a_live_audit_log(self, client, mocker):
        """Vacuity guard: the field must track the state, not be a constant."""
        mocker.patch.object(audit, "_state", "ready")
        body = client.get("/healthz").json()
        assert body["audit"]["enabled"] is True
        assert body["status"] == "ok"

    def test_healthz_still_answers_200_while_the_audit_log_is_down(self, client, mocker):
        """Liveness must not follow the audit log — an unaudited pod is degraded, not wedged,
        and failing liveness here would have Kubernetes restart-loop it during a DB outage."""
        mocker.patch.object(audit, "_state", "unavailable")
        assert client.get("/healthz").status_code == 200
