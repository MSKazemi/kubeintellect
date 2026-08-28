"""The claim is about what Postgres holds after a delete, so a fake pool cannot make it.

`test_a_declared_gap_is_not_tampering.py` proves the refusals fire before anything is written
and that the verifier honours a record it is handed. Neither says the thing an operator
actually needs: that after `chain-export` and `chain-truncate` have run against their database,
the server's own verifier reports the chain **intact** — and still reports TAMPERED the moment
the record stops matching the rows.

It is also the first place the two drivers meet on the same chain. The CLI truncates through
**psycopg** and the server verifies through **asyncpg**, and they disagree about JSONB (`dict`
vs `str`). A truncation written by one and verified by the other is exactly where that would
show up as a chain that reads as tampered on a healthy install.

The `chain_truncation` DDL is read out of the shipped `schema.sql` rather than written here,
so a column added to the product and not to the record path fails this file instead of an
install.

Skips without docker.
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from app.db import chain_export
from app.db import flight_recorder as fr
from app.memory import liveness, security, service

pytestmark = pytest.mark.asyncio

CLUSTER = "trunc-test-cluster"
EPISODE = "ep-trunc-test"
_SCHEMA = (Path(__file__).resolve().parents[1] / "packages" / "kubeintellect-server" / "app" /
           "db" / "schema.sql")


def _shipped_chain_truncation_ddl() -> str:
    text = _SCHEMA.read_text(encoding="utf-8")
    match = re.search(r"CREATE TABLE IF NOT EXISTS chain_truncation \(.*?\n\);", text, re.S)
    assert match, "schema.sql no longer defines chain_truncation — the record path is unshipped"
    return match.group(0)


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
    """CREATE TABLE decision_log (
           id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
           episode_id TEXT NOT NULL, seq BIGINT NOT NULL, kind TEXT NOT NULL,
           payload JSONB NOT NULL DEFAULT '{}', prev_hash TEXT NOT NULL, hash TEXT NOT NULL,
           created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE (episode_id, seq))""",
    """CREATE TABLE decision_log_head (
           episode_id TEXT PRIMARY KEY, seq BIGINT NOT NULL, hash TEXT NOT NULL,
           updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    _shipped_chain_truncation_ddl(),
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
    name = f"ki-trunc-test-{uuid.uuid4().hex[:8]}"
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
                for ddl in _DDL:
                    await con.execute(ddl)
            await pool.close()

        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_setup())
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture
async def pool(dsn, monkeypatch):
    """The server's asyncpg pool, bound where both verifiers look, over empty chains."""
    import asyncpg
    p = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    await p.execute("TRUNCATE memory_audit, memory_chain_head, decision_log, "
                    "decision_log_head, chain_truncation")
    security._audit_chains.pop(CLUSTER, None)
    liveness.reset_chain_state()
    monkeypatch.setattr(service, "_pool", p, raising=False)
    monkeypatch.setattr(fr, "_pool", p, raising=False)
    monkeypatch.setattr("app.cluster_id.get_cluster_id", lambda: CLUSTER)
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
def cli(dsn):
    """The CLI's own psycopg connection — the driver `chain-export`/`chain-truncate` use."""
    import psycopg
    conn = psycopg.connect(dsn)
    conn.autocommit = True

    def query(sql: str):
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()

    def execute(sql: str) -> None:
        with conn.cursor() as cur:
            cur.execute(sql)

    yield query, execute
    conn.close()


async def append_audit(pool, n: int) -> None:
    for i in range(n):
        digest = await security.record_memory_audit(
            pool, cluster_id=CLUSTER, kind="episode_write", ref_id=f"ep-{i}",
            payload={"reason": f"pod api-{i} crashlooped"})
        assert digest, "the append path itself failed — nothing below would mean anything"


async def append_decisions(pool, n: int) -> None:
    prev = ""
    for seq in range(n):
        payload = {"step": seq, "tool": "kubectl get pods"}
        digest = fr.compute_hash(prev, EPISODE, seq, "tool_call", payload)
        await pool.execute(
            "INSERT INTO decision_log (episode_id, seq, kind, payload, prev_hash, hash) "
            "VALUES ($1,$2,$3,$4::jsonb,$5,$6)",
            EPISODE, seq, "tool_call", json.dumps(payload), prev, digest)
        prev = digest
    await pool.execute(
        "INSERT INTO decision_log_head (episode_id, seq, hash) VALUES ($1,$2,$3) "
        "ON CONFLICT (episode_id) DO UPDATE SET seq = EXCLUDED.seq, hash = EXCLUDED.hash",
        EPISODE, n - 1, prev)


async def audit_state(pool) -> str:
    await service.verify_chain_once()
    return liveness.chain_status(enabled=True)["state"]


def prune(cli, *, chain: str, scope: str, through: int, note: str = "") -> dict:
    query, execute = cli
    doc = chain_export.build_export(
        query, chain=chain, scope_id=scope, taken_at="2026-08-28T00:00:00+00:00",
        through_seq=through)
    assert chain_export.verify_export(doc)["ok"], "the archive must verify before deleting"
    return chain_export.truncate_chain(query, execute, doc=doc, note=note), doc


class TestAPrunedMemoryChainReadsAsIntact:
    async def test_the_whole_flow_end_to_end(self, pool, cli):
        await append_audit(pool, 6)
        assert await audit_state(pool) == "intact"
        outcome, _doc = prune(cli, chain="memory_audit", scope=CLUSTER, through=2,
                              note="90-day retention")
        assert outcome["rows_removed"] == 3 and outcome["resume_seq"] == 3
        assert await pool.fetchval(
            "SELECT count(*) FROM memory_audit WHERE cluster_id = $1", CLUSTER) == 3
        assert await audit_state(pool) == "intact"

    async def test_without_the_record_the_same_delete_is_tampering(self, pool, cli):
        """The invariant. An undeclared gap must stay indistinguishable from an edit."""
        await append_audit(pool, 6)
        prune(cli, chain="memory_audit", scope=CLUSTER, through=2)
        await pool.execute("DELETE FROM chain_truncation")
        assert await audit_state(pool) == "TAMPERED"

    async def test_a_record_that_does_not_match_the_rows_is_tampering(self, pool, cli):
        await append_audit(pool, 6)
        prune(cli, chain="memory_audit", scope=CLUSTER, through=2)
        await pool.execute("UPDATE chain_truncation SET resume_seq = 4")
        assert await audit_state(pool) == "TAMPERED"

    async def test_a_forged_resume_hash_is_tampering(self, pool, cli):
        """A record has to be CONSISTENT with rows it does not control to be useful."""
        await append_audit(pool, 6)
        prune(cli, chain="memory_audit", scope=CLUSTER, through=2)
        await pool.execute("UPDATE chain_truncation SET resume_prev_hash = 'forged'")
        assert await audit_state(pool) == "TAMPERED"

    async def test_the_record_does_not_excuse_a_later_edit(self, pool, cli):
        await append_audit(pool, 6)
        prune(cli, chain="memory_audit", scope=CLUSTER, through=2)
        await pool.execute(
            "UPDATE memory_audit SET payload = $1::jsonb WHERE cluster_id = $2 AND seq = 4",
            '{"reason": "nothing happened here"}', CLUSTER)
        assert await audit_state(pool) == "TAMPERED"

    async def test_the_head_anchor_still_catches_the_other_end(self, pool, cli):
        """Truncation removes the oldest rows; the anchor watches the newest. Both still."""
        await append_audit(pool, 6)
        prune(cli, chain="memory_audit", scope=CLUSTER, through=2)
        await pool.execute("DELETE FROM memory_audit WHERE cluster_id = $1 AND seq = 5",
                           CLUSTER)
        assert await audit_state(pool) == "TAMPERED"

    async def test_appending_after_a_truncation_still_verifies(self, pool, cli):
        """The chain has to keep working — a pruned install is not a read-only one."""
        await append_audit(pool, 6)
        prune(cli, chain="memory_audit", scope=CLUSTER, through=2)
        await append_audit(pool, 2)
        assert await pool.fetchval(
            "SELECT max(seq) FROM memory_audit WHERE cluster_id = $1", CLUSTER) == 7
        assert await audit_state(pool) == "intact"

    async def test_an_unreadable_record_is_not_verified_rather_than_intact(self, pool, cli):
        """Losing the table must not silently bless the gap — nor accuse the operator."""
        await append_audit(pool, 6)
        prune(cli, chain="memory_audit", scope=CLUSTER, through=2)
        await pool.execute("ALTER TABLE chain_truncation RENAME TO chain_truncation_gone")
        try:
            await service.verify_chain_once()
            assert liveness.chain_status(enabled=True)["state"] == "unverified"
        finally:
            await pool.execute("ALTER TABLE chain_truncation_gone RENAME TO chain_truncation")


class TestAPrunedEpisodeStillReplays:
    async def test_the_recorder_chain_verifies_after_a_declared_truncation(self, pool, cli):
        await append_decisions(pool, 6)
        rows = [dict(r) for r in await pool.fetch(
            "SELECT episode_id, seq, kind, payload, prev_hash, hash FROM decision_log "
            "WHERE episode_id = $1 ORDER BY seq", EPISODE)]
        assert (await fr.verify_episode(EPISODE, rows)) == fr.ChainVerdict(True, True)

        prune(cli, chain="decision_log", scope=EPISODE, through=2)
        rows = [dict(r) for r in await pool.fetch(
            "SELECT episode_id, seq, kind, payload, prev_hash, hash FROM decision_log "
            "WHERE episode_id = $1 ORDER BY seq", EPISODE)]
        assert fr.verify_chain(rows) is False, "the link check alone cannot know"
        assert (await fr.verify_episode(EPISODE, rows)) == fr.ChainVerdict(True, True)

    async def test_an_undeclared_truncation_of_an_episode_is_still_caught(self, pool):
        await append_decisions(pool, 6)
        await pool.execute("DELETE FROM decision_log WHERE episode_id = $1 AND seq <= 2",
                           EPISODE)
        rows = [dict(r) for r in await pool.fetch(
            "SELECT episode_id, seq, kind, payload, prev_hash, hash FROM decision_log "
            "WHERE episode_id = $1 ORDER BY seq", EPISODE)]
        assert (await fr.verify_episode(EPISODE, rows)) == fr.ChainVerdict(False, True)

    async def test_another_episodes_record_does_not_excuse_this_one(self, pool, cli):
        """The record is scoped; a shared database must not launder across episodes."""
        await append_decisions(pool, 6)
        prune(cli, chain="decision_log", scope=EPISODE, through=2)
        await pool.execute("UPDATE chain_truncation SET scope_id = 'some-other-episode'")
        rows = [dict(r) for r in await pool.fetch(
            "SELECT episode_id, seq, kind, payload, prev_hash, hash FROM decision_log "
            "WHERE episode_id = $1 ORDER BY seq", EPISODE)]
        assert (await fr.verify_episode(EPISODE, rows)) == fr.ChainVerdict(False, True)


class TestTheTwoDriversAgree:
    async def test_psycopg_wrote_what_asyncpg_reads(self, pool, cli):
        """JSONB is a dict to one driver and a str to the other; the seam hash is not."""
        await append_audit(pool, 5)
        _outcome, doc = prune(cli, chain="memory_audit", scope=CLUSTER, through=1)
        record = await pool.fetchrow("SELECT * FROM chain_truncation")
        assert record["resume_prev_hash"] == doc["end_hash"]
        assert record["archive_hash"] == doc["archive_hash"]
        assert record["chain"] == "memory_audit" and record["scope_id"] == CLUSTER
        first = await pool.fetchrow(
            "SELECT prev_hash FROM memory_audit WHERE cluster_id = $1 ORDER BY seq LIMIT 1",
            CLUSTER)
        assert first["prev_hash"] == record["resume_prev_hash"]
