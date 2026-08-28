"""A12 against a real PostgreSQL — because the fake could not hold the failure.

`backup.verify` is driver-agnostic by design and its unit tests drive it with a dict. That dict
has no transaction, and **two real defects lived in exactly the gap the dict cannot cover**:

* a `--out` that wrote nothing and exited 0 (SQLite branch, fixed 2026-08-28);
* one missing table poisoning the psycopg transaction so that six healthy tables were reported
  as `the restore did not create it` — 9 findings, 1 true (fixed 2026-08-28).

The chain subsystem next door has had a real-database test since it shipped and a live
adversarial rehearsal of ten cases found nothing wrong with it. That contrast is the argument
for this file: the backup path is the one that never adopted the harness.

So these tests drive **`app.cli._backup_query`** — the actual psycopg adapter, not a stand-in —
against a real server. `test_one_missing_table_is_reported_once` is the regression test for the
autocommit fix and fails without it.

Skips without docker.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import subprocess
import time
import uuid

import pytest

from app.db.backup import COUNTED_TABLES, build_manifest, verify

_COUNTED_DDL = tuple(
    f"CREATE TABLE {t} (id SERIAL PRIMARY KEY, v TEXT)" for t in COUNTED_TABLES
)
_ANCHOR_DDL = (
    """CREATE TABLE decision_log_head (
           episode_id TEXT PRIMARY KEY, seq BIGINT NOT NULL, hash TEXT NOT NULL)""",
    """CREATE TABLE memory_chain_head (
           cluster_id TEXT PRIMARY KEY, seq BIGINT NOT NULL, hash TEXT NOT NULL)""",
)


def _docker_ok() -> bool:
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.fixture(scope="module")
def dsn():
    if not _docker_ok():
        pytest.skip("docker is not available")
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    name = f"ki-a12-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=t",
         "-e", "POSTGRES_USER=t", "-e", "POSTGRES_DB=t", "-p", f"{port}:5432",
         "postgres:16-alpine"], check=True, capture_output=True)
    try:
        import asyncpg
        url = f"postgresql://t:t@127.0.0.1:{port}/t"

        async def _setup():
            deadline, last, pool = time.time() + 90, None, None
            while time.time() < deadline:
                try:
                    pool = await asyncpg.create_pool(url, min_size=1, max_size=2)
                    break
                except (OSError, asyncpg.PostgresError) as exc:
                    last = exc
                    await asyncio.sleep(0.5)
            if pool is None:
                raise AssertionError(f"postgres never accepted a connection: {last}")
            async with pool.acquire() as con:
                for ddl in (*_COUNTED_DDL, *_ANCHOR_DDL):
                    await con.execute(ddl)
                # Two anchors on decision_log; the chain table itself is a counted table above,
                # so `unreached` is computed over real rows.
                await con.execute(
                    "ALTER TABLE decision_log ADD COLUMN episode_id TEXT, ADD COLUMN seq BIGINT")
                await con.execute(
                    "ALTER TABLE memory_audit ADD COLUMN cluster_id TEXT, ADD COLUMN seq BIGINT")
                for ep, n in (("ep-1", 3), ("ep-2", 2)):
                    for i in range(n):
                        await con.execute(
                            "INSERT INTO decision_log (v, episode_id, seq) VALUES ($1,$2,$3)",
                            "x", ep, i)
                    await con.execute(
                        "INSERT INTO decision_log_head (episode_id, seq, hash) "
                        "VALUES ($1,$2,'h')", ep, n - 1)
            await pool.close()

        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_setup())
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture
def measured(dsn):
    """`(manifest, fresh_query_factory)` over the real server, through the real adapter."""
    from app.cli import _backup_query

    conn, query = _backup_query(dsn)
    try:
        manifest = build_manifest(query, taken_at=_dt.datetime.now(_dt.UTC).isoformat())
    finally:
        conn.close()
    return manifest, (lambda: _backup_query(dsn))


def _verify(factory, manifest):
    conn, query = factory()
    try:
        return verify(query, manifest)
    finally:
        conn.close()


class TestTheHappyPath:
    def test_a_database_that_did_not_change_verifies(self, measured):
        manifest, factory = measured
        assert _verify(factory, manifest)["ok"] is True

    def test_the_manifest_measured_the_real_chain(self, measured):
        manifest, _ = measured
        assert manifest["row_counts"]["decision_log"] == 5
        assert manifest["chains"]["decision_log"]["anchors"] == 2
        assert manifest["chains"]["decision_log"]["unreached_at_backup"] == 0


class TestTheFailuresThatMatter:
    def test_a_truncated_tail_is_seen_where_the_link_check_cannot(self, measured, dsn):
        """The whole reason A12 exists — proven against Postgres, not a dict."""
        import psycopg
        manifest, factory = measured
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("DELETE FROM decision_log WHERE episode_id='ep-1' AND seq=2")
        result = _verify(factory, manifest)
        assert result["ok"] is False
        joined = " | ".join(result["problems"])
        assert "decision_log" in joined and "MISSING" in joined
        assert "do not reach their recorded head" in joined
        with psycopg.connect(dsn, autocommit=True) as c:   # restore for the next test
            c.execute("INSERT INTO decision_log (v, episode_id, seq) VALUES ('x','ep-1',2)")

    def test_one_missing_table_is_reported_once(self, measured, dsn):
        """Regression test for the autocommit fix: without it this reports eight more."""
        import psycopg
        manifest, factory = measured
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("DROP TABLE runbooks")
        try:
            result = _verify(factory, manifest)
            offenders = {p.split(":")[0] for p in result["problems"]}
            assert offenders == {"runbooks"}, result["problems"]
            assert not [p for p in result["problems"]
                        if "the restore did not create it" in p
                        and not p.startswith("runbooks")]
        finally:
            with psycopg.connect(dsn, autocommit=True) as c:
                c.execute("CREATE TABLE runbooks (id SERIAL PRIMARY KEY, v TEXT)")

    def test_the_adapter_really_is_the_one_the_cli_uses(self):
        import inspect

        from app import cli

        assert "_backup_query(dsn)" in inspect.getsource(cli.cmd_verify_restore)
