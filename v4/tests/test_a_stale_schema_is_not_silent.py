"""A11 — the database now says which schema it has, and drift is loud.

`schema.sql` is idempotent (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`) and applied
by hand with `kubeintellect db-init`. Until 2026-08-28 nothing recorded that it had ever been
run, so three states were indistinguishable:

* upgraded the image, forgot `db-init` — the columns this build writes to do not exist;
* rolled the Deployment back while the database kept the newer schema;
* correct.

The first two do not fail loudly, and that is the point of this file. Every memory, recorder and
audit write is fire-and-forget by design, so a missing column is a logged warning inside a
swallowed exception: memory quietly stops recording while `/healthz` keeps reporting
`enabled: true`. The same silent-subsystem failure that `memory_status()` and `audit_status()`
were each built to end, arriving through a door nobody had covered.
"""
from __future__ import annotations

import inspect

import pytest

from app.db import schema_version
from app.db.schema_version import (
    PINNED_FINGERPRINT,
    SCHEMA_VERSION,
    check_schema,
    record_args,
    schema_fingerprint,
    schema_status,
)


class FakePool:
    """Returns one `schema_migrations` row, or raises."""

    def __init__(self, row: dict | None = None, raises: Exception | None = None) -> None:
        self.row = row
        self.raises = raises
        self.queries: list[str] = []

    async def fetchrow(self, sql: str, *args):
        self.queries.append(sql)
        if self.raises is not None:
            raise self.raises
        return self.row


@pytest.fixture(autouse=True)
def _clean():
    schema_version.reset_for_tests()
    yield
    schema_version.reset_for_tests()


def _row(version: int, fingerprint: str) -> dict:
    return {"version": version, "fingerprint": fingerprint, "applied_at": None}


class TestTheFingerprintMeasuresTheSchemaNotItsProse:
    def test_it_is_stable_across_a_reworded_comment(self):
        sql = "-- old wording\nCREATE TABLE t (id INT);\n"
        reworded = "-- a much longer, clearer explanation of the same table\nCREATE TABLE t (id INT);\n"
        assert schema_fingerprint(sql) == schema_fingerprint(reworded)

    def test_it_is_stable_across_reindentation(self):
        assert schema_fingerprint("CREATE TABLE t (id INT);\n") == \
               schema_fingerprint("   CREATE TABLE t (id INT);   \n\n")

    def test_it_changes_when_the_ddl_changes(self):
        assert schema_fingerprint("CREATE TABLE t (id INT);") != \
               schema_fingerprint("CREATE TABLE t (id BIGINT);")

    def test_the_shipped_schema_fingerprints(self):
        fp = schema_fingerprint()
        assert len(fp) == 64 and fp == schema_fingerprint()


class TestTheFourVerdicts:
    @pytest.mark.asyncio
    async def test_matching_version_and_fingerprint_is_current(self):
        state = await check_schema(FakePool(_row(SCHEMA_VERSION, schema_fingerprint())))
        assert state["state"] == "current"
        assert state["matches"] is True
        assert state["reason"] == ""

    @pytest.mark.asyncio
    async def test_a_changed_fingerprint_is_stale(self):
        state = await check_schema(FakePool(_row(SCHEMA_VERSION, "0" * 64)))
        assert state["state"] == "stale"
        assert state["matches"] is False
        assert "db-init" in state["reason"]

    @pytest.mark.asyncio
    async def test_the_stale_reason_names_the_reason_it_is_silent(self):
        """An operator must be told why nothing errored — that is the whole failure mode."""
        state = await check_schema(FakePool(_row(SCHEMA_VERSION, "0" * 64)))
        assert "fire-and-forget" in state["reason"]
        assert "swallowed" in state["reason"]

    @pytest.mark.asyncio
    async def test_a_newer_database_is_ahead_not_stale(self):
        """A rollback is the dangerous direction: this build's DDL cannot restore the old shape."""
        state = await check_schema(FakePool(_row(SCHEMA_VERSION + 3, "0" * 64)))
        assert state["state"] == "ahead"
        assert state["applied_version"] == SCHEMA_VERSION + 3
        assert "rolled back" in state["reason"]

    @pytest.mark.asyncio
    async def test_ahead_wins_over_stale(self):
        """Both conditions hold on a rollback; reporting 'run db-init' would be wrong advice."""
        state = await check_schema(FakePool(_row(SCHEMA_VERSION + 1, "0" * 64)))
        assert state["state"] == "ahead"

    @pytest.mark.asyncio
    async def test_an_empty_ledger_is_unrecorded(self):
        state = await check_schema(FakePool(row=None))
        assert state["state"] == "unrecorded"
        assert state["applied_version"] is None


class TestAFailedCheckIsNotAVerdict:
    @pytest.mark.asyncio
    async def test_a_read_failure_is_unknown_never_current(self):
        """'We could not look' must never be reported as 'the schema is fine'."""
        state = await check_schema(FakePool(raises=RuntimeError("relation does not exist")))
        assert state["state"] == "unknown"
        assert state["matches"] is None
        assert "relation does not exist" in state["reason"]

    @pytest.mark.asyncio
    async def test_it_never_raises_into_startup(self):
        """This runs while the memory pool opens; raising here would break the boot path."""
        await check_schema(FakePool(raises=RuntimeError("boom")))

    def test_the_default_before_any_check_is_unknown(self):
        assert schema_status()["state"] == "unknown"


class TestItIsReportedWhereAnOperatorLooks:
    @pytest.mark.asyncio
    async def test_healthz_carries_the_block(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api.v1.endpoints.health import router

        await check_schema(FakePool(_row(SCHEMA_VERSION, schema_fingerprint())))
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as c:
            body = c.get("/healthz").json()
        assert body["db_schema"]["state"] == "current"
        assert body["db_schema"]["expected_version"] == SCHEMA_VERSION

    @pytest.mark.asyncio
    async def test_healthz_reports_stale_without_failing_liveness(self):
        """A stale schema must not restart the pod — that would be a crash loop, not a fix."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api.v1.endpoints.health import router

        await check_schema(FakePool(_row(SCHEMA_VERSION, "0" * 64)))
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as c:
            resp = c.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert resp.json()["db_schema"]["state"] == "stale"

    def test_healthz_does_not_query_postgres_to_answer(self):
        """Liveness must not touch a dependency; the verdict is cached from startup."""
        src = inspect.getsource(schema_version.schema_status)
        assert "await" not in src and "fetch" not in src


class TestTheLedgerIsActuallyWritten:
    def test_db_init_records_what_it_applied(self):
        from app import cli
        src = inspect.getsource(cli.cmd_db_init)
        assert "RECORD_SQL" in src and "record_args" in src

    def test_the_recorded_args_are_this_build(self):
        version, fingerprint, by = record_args("db-init")
        assert version == SCHEMA_VERSION
        assert fingerprint == schema_fingerprint()
        assert by == "db-init"

    def test_the_ledger_table_is_in_the_schema(self):
        assert "CREATE TABLE IF NOT EXISTS schema_migrations" in schema_version.schema_sql()

    def test_the_check_runs_when_the_pool_opens(self):
        from app.memory import service
        assert "check_schema" in inspect.getsource(service._try_connect)


class TestWhatItIsNotIsWrittenDown:
    def test_the_module_states_it_is_not_a_migration_tool(self):
        doc = " ".join((schema_version.__doc__ or "").split())
        assert "there is still no migration *tool* here" in doc
        assert "down-migrations" in doc
        assert "Alembic" in doc

    def test_the_refusal_is_dated(self):
        assert "2026-08-28" in (schema_version.__doc__ or "")


class TestTheBuildTimeGateIsWhatEnforcesTheDiscipline:
    """`SCHEMA_VERSION` is a number a human has to remember to bump. This is what remembers."""

    def test_the_shipped_schema_matches_the_pinned_fingerprint(self):
        assert schema_fingerprint() == PINNED_FINGERPRINT, (
            "schema.sql changed. Bump SCHEMA_VERSION, update PINNED_FINGERPRINT and the literal "
            "in schema.sql's stamping INSERT, and re-run the Helm ConfigMap copy."
        )

    def test_the_schema_stamps_its_own_version_and_it_agrees(self):
        """The chart's db-init Job pipes schema.sql into psql and never runs the Python CLI."""
        import re
        sql = schema_version.schema_sql()
        m = re.search(r"INSERT INTO schema_migrations \(version, fingerprint, applied_by\)\s*"
                      r"VALUES \((\d+),", sql)
        assert m, "schema.sql does not stamp the ledger — a chart install would read `unrecorded`"
        assert int(m.group(1)) == SCHEMA_VERSION

    @pytest.mark.asyncio
    async def test_a_sql_stamped_row_is_current_not_stale(self):
        """Empty fingerprint = stamped by schema.sql, which cannot hash the file it is running."""
        state = await check_schema(FakePool(_row(SCHEMA_VERSION, "")))
        assert state["state"] == "current"
        assert "fingerprint not recorded" in state["reason"]

    @pytest.mark.asyncio
    async def test_a_sql_stamped_row_at_an_older_version_is_still_stale(self):
        state = await check_schema(FakePool(_row(SCHEMA_VERSION - 1, "")))
        assert state["state"] == "stale"
