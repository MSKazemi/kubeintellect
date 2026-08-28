"""Every test of the memory tamper detector so far has driven a fake pool.

`verify_memory_chain` gained a caller in a running server on 2026-08-28. The tests that proved
it hand `service` an object with `fetch`/`fetchrow` methods returning lists this file wrote —
which proves the *verdict logic*, and cannot prove the thing the verdict is about: that the SQL
selects the rows it thinks it does, that `record_memory_audit` and the verifier agree on the
hash inputs, and that a `DELETE` in psql is what the head anchor is compared against.

That gap has bitten this exact subsystem before. The sibling
`test_the_lexical_channel_of_hybrid_recall_never_matched.py` records a flag that shipped, was
unit-tested against a `FakePool`, and returned **zero rows on 225 of 225 real queries** — the
fake accepted any SQL and returned whatever the test handed it, so no test ever asked Postgres
what the query admits. A tamper detector is a worse place for the same hole: a chain that
verifies `intact` because the SELECT found nothing is indistinguishable, to every caller, from
a chain that verifies `intact` because it is.

So these run the real append path against a real Postgres, then tamper with it in SQL:

    append 5 entries with record_memory_audit  -> intact
    UPDATE one payload                          -> TAMPERED   (a link breaks)
    DELETE the two newest rows                  -> TAMPERED   (no link breaks; the anchor does)
    DROP the audit table                        -> unverified (NOT tampered)

The third is the one only a database can prove. Truncation leaves a shorter chain in which
every link still verifies; nothing about the rows says they are incomplete. Only the persisted
head contradicts them.

Skips without docker, on purpose — the point is what the database does, and a fixture that
needed the full product schema would be skipped far more often than it needs to be.
"""
from __future__ import annotations

import asyncio
import subprocess
import time
import uuid

import pytest

from app.memory import liveness, security, service

pytestmark = pytest.mark.asyncio

CLUSTER = "chain-test-cluster"

_DDL = (
    """CREATE TABLE memory_audit (
           id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
           cluster_id TEXT NOT NULL, seq BIGINT NOT NULL, kind TEXT NOT NULL,
           ref_id TEXT, payload JSONB NOT NULL DEFAULT '{}',
           prev_hash TEXT NOT NULL, hash TEXT NOT NULL,
           created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
           UNIQUE (cluster_id, seq))""",
    """CREATE TABLE memory_chain_head (
           cluster_id TEXT PRIMARY KEY, seq BIGINT NOT NULL, hash TEXT NOT NULL,
           updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
)


def _docker_ok() -> bool:
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.fixture(scope="module")
def dsn():
    """A throwaway Postgres carrying only the two tables the chain lives in."""
    if not _docker_ok():
        pytest.skip("docker is not available")
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    name = f"ki-chain-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=t",
         "-e", "POSTGRES_USER=t", "-e", "POSTGRES_DB=t", "-p", f"{port}:5432",
         "postgres:16-alpine"], check=True, capture_output=True)
    try:
        import asyncpg
        url = f"postgresql://t:t@127.0.0.1:{port}/t"

        async def _setup():
            # Wait for a connection that SURVIVES: the image runs a temporary server for
            # initdb, so the first accepted TCP connection can still be reset under us.
            deadline, last = time.time() + 90, None
            pool = None
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
                for ddl in _DDL:
                    await con.execute(ddl)
            await pool.close()

        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_setup())
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture
async def pool(dsn, monkeypatch):
    """A live pool with an empty chain, bound where `service.verify_chain_once` looks."""
    import asyncpg
    p = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    await p.execute("TRUNCATE memory_audit, memory_chain_head")
    security._audit_chains.pop(CLUSTER, None)      # the in-process seq cache
    liveness.reset_chain_state()
    monkeypatch.setattr(service, "_pool", p, raising=False)
    monkeypatch.setattr("app.cluster_id.get_cluster_id", lambda: CLUSTER)
    try:
        yield p
    finally:
        await p.close()
        security._audit_chains.pop(CLUSTER, None)
        liveness.reset_chain_state()


async def _append(pool, n: int) -> None:
    for i in range(n):
        digest = await security.record_memory_audit(
            pool, cluster_id=CLUSTER, kind="episode_write", ref_id=f"ep-{i}",
            payload={"reason": f"pod api-{i} crashlooped"},
        )
        assert digest, "the append path itself failed — nothing below would mean anything"


async def _state(pool) -> str:
    await service.verify_chain_once()
    return liveness.chain_status(enabled=True)["state"]


class TestTheAppendPathAndTheVerifierAgree:
    async def test_an_appended_chain_verifies_against_the_database(self, pool):
        await _append(pool, 5)
        assert await _state(pool) == "intact"

    async def test_the_rows_really_are_in_postgres(self, pool):
        """Guards the failure the fake pool cannot have: an `intact` verdict over no rows."""
        await _append(pool, 5)
        assert await pool.fetchval(
            "SELECT count(*) FROM memory_audit WHERE cluster_id = $1", CLUSTER) == 5
        head = await pool.fetchrow(
            "SELECT seq FROM memory_chain_head WHERE cluster_id = $1", CLUSTER)
        assert head["seq"] == 4

    async def test_an_empty_chain_is_intact_not_tampered(self, pool):
        """Nothing has been recorded yet. A new cluster must not read as an incident."""
        assert await _state(pool) == "intact"

    async def test_another_cluster_id_is_not_this_chain(self, pool):
        """The verifier scopes by cluster; a shared database must not cross-accuse."""
        await _append(pool, 3)
        await pool.execute(
            "INSERT INTO memory_audit (cluster_id, seq, kind, payload, prev_hash, hash) "
            "VALUES ('other', 0, 'episode_write', '{}'::jsonb, '', 'not-a-real-hash')")
        assert await _state(pool) == "intact"


class TestTamperingWithItInSqlIsCaught:
    async def test_an_edited_payload_breaks_a_link(self, pool):
        await _append(pool, 5)
        await pool.execute(
            "UPDATE memory_audit SET payload = $1::jsonb WHERE cluster_id = $2 AND seq = 2",
            '{"reason": "nothing happened here"}', CLUSTER)
        assert await _state(pool) == "TAMPERED"

    async def test_deleting_the_newest_rows_breaks_no_link_and_is_caught_anyway(self, pool):
        """The case a hash chain is structurally blind to. Only the anchor sees it."""
        await _append(pool, 5)
        await pool.execute(
            "DELETE FROM memory_audit WHERE cluster_id = $1 AND seq >= 3", CLUSTER)
        rows = await pool.fetch(security._SQL_AUDIT_ROWS, CLUSTER)
        from app.db import flight_recorder as fr
        adapted = [{"episode_id": CLUSTER, **dict(r)} for r in rows]
        assert fr.verify_chain(adapted) is True, "every remaining link still verifies"
        assert await _state(pool) == "TAMPERED"

    async def test_deleting_an_interior_row_is_caught(self, pool):
        await _append(pool, 5)
        await pool.execute(
            "DELETE FROM memory_audit WHERE cluster_id = $1 AND seq = 2", CLUSTER)
        assert await _state(pool) == "TAMPERED"

    async def test_reordering_is_caught(self, pool):
        await _append(pool, 5)
        await pool.execute(
            "UPDATE memory_audit SET seq = 99 WHERE cluster_id = $1 AND seq = 1", CLUSTER)
        assert await _state(pool) == "TAMPERED"

    async def test_forging_the_head_alone_does_not_launder_a_truncation(self, pool):
        """Stated as a limit in `docs/security.md`: forging BOTH places still wins. This
        proves only that forging one of them does not — which is what the anchor buys."""
        await _append(pool, 5)
        await pool.execute(
            "DELETE FROM memory_audit WHERE cluster_id = $1 AND seq >= 3", CLUSTER)
        assert await _state(pool) == "TAMPERED"
        row = await pool.fetchrow(
            "SELECT seq, hash FROM memory_audit WHERE cluster_id = $1 ORDER BY seq DESC LIMIT 1",
            CLUSTER)
        await pool.execute(
            "UPDATE memory_chain_head SET seq = $1, hash = $2 WHERE cluster_id = $3",
            row["seq"], row["hash"], CLUSTER)
        assert await _state(pool) == "intact", "two forged places is the documented limit"


class TestADatabaseThatCannotAnswerIsNotAnAccusation:
    async def test_a_missing_table_is_unverified(self, pool):
        await _append(pool, 5)
        await pool.execute("ALTER TABLE memory_audit RENAME TO memory_audit_hidden")
        try:
            assert await _state(pool) == "unverified"
        finally:
            await pool.execute("ALTER TABLE memory_audit_hidden RENAME TO memory_audit")

    async def test_a_missing_head_table_keeps_the_link_verdict(self, pool):
        """The rows were read and they verify; only the anchor could not be. Not a tamper."""
        await _append(pool, 5)
        await pool.execute("ALTER TABLE memory_chain_head RENAME TO head_hidden")
        try:
            assert await _state(pool) == "unverified"
        finally:
            await pool.execute("ALTER TABLE head_hidden RENAME TO memory_chain_head")

    async def test_a_closed_pool_is_unverified(self, pool):
        await _append(pool, 5)
        await pool.close()
        assert await _state(pool) == "unverified"
