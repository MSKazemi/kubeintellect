"""An archive built from a fake `query` proves the archive format, not the export.

`test_a_chain_can_be_archived_before_it_is_pruned.py` drives `build_export` with a callable
that answers by which table the SQL names — which cannot tell you whether the SQL selects the
right rows, whether the scope filter works on a shared table, or whether JSONB survives the
driver in a form that hashes to what the writer computed. That last one is not hypothetical:
asyncpg hands back JSONB as a `str` and psycopg as a `dict`, and a hash taken over the wrong
one is silently wrong — the same class of defect that let a recall channel return zero rows on
225 of 225 real queries while every unit test passed against a `FakePool`.

So these run the real export against a real Postgres, through **psycopg** — the driver the CLI
actually uses (`cli._backup_query`), not the one the server uses. If the two disagree about
JSONB, an archive taken by the CLI would fail to verify and this is where that shows up.

Skips without docker.
"""
from __future__ import annotations

import json
import subprocess
import uuid

import pytest

from app.db import chain_export
from app.db.flight_recorder import compute_hash

EPISODE = "ep-archive-test"

_DDL = """CREATE TABLE decision_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    episode_id TEXT NOT NULL, seq BIGINT NOT NULL, kind TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}', prev_hash TEXT NOT NULL, hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE (episode_id, seq));
CREATE TABLE decision_log_head (
    episode_id TEXT PRIMARY KEY, seq BIGINT NOT NULL, hash TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now());"""


def _docker_ok() -> bool:
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.fixture(scope="module")
def conn():
    """A throwaway Postgres holding one flight-recorder chain, driven by psycopg."""
    if not _docker_ok():
        pytest.skip("docker is not available")
    import socket
    import time

    import psycopg
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    name = f"ki-archive-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=t",
         "-e", "POSTGRES_USER=t", "-e", "POSTGRES_DB=t", "-p", f"{port}:5432",
         "postgres:16-alpine"], check=True, capture_output=True)
    try:
        dsn = f"postgresql://t:t@127.0.0.1:{port}/t"
        # Wait for a connection that SURVIVES: the image runs a temporary server for initdb,
        # so the first accepted TCP connection can still be reset under us.
        deadline, last, c = time.time() + 90, None, None
        while time.time() < deadline:
            try:
                c = psycopg.connect(dsn)
                break
            except psycopg.Error as exc:
                last = exc
                time.sleep(0.5)
        if c is None:
            pytest.fail(f"postgres never accepted a connection: {last}")
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute(_DDL)
        yield c
        c.close()
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture
def query(conn):
    """The CLI's own `query(sql) -> rows`, over a chain rebuilt for each test."""
    with conn.cursor() as cur:
        cur.execute("TRUNCATE decision_log, decision_log_head")

    def _query(sql: str):
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    return _query


def write_chain(conn, n: int, episode: str = EPISODE, payloads=None) -> list[dict]:
    """Append `n` genuinely chained rows, exactly as the recorder would."""
    rows, prev = [], ""
    with conn.cursor() as cur:
        for seq in range(n):
            payload = (payloads or {}).get(seq, {"step": seq, "tool": "kubectl get pods"})
            digest = compute_hash(prev, episode, seq, "tool_call", payload)
            cur.execute(
                "INSERT INTO decision_log (episode_id, seq, kind, payload, prev_hash, hash) "
                "VALUES (%s,%s,%s,%s::jsonb,%s,%s)",
                (episode, seq, "tool_call", json.dumps(payload), prev, digest))
            rows.append({"seq": seq, "payload": payload, "prev_hash": prev, "hash": digest})
            prev = digest
        cur.execute(
            "INSERT INTO decision_log_head (episode_id, seq, hash) VALUES (%s,%s,%s) "
            "ON CONFLICT (episode_id) DO UPDATE SET seq = EXCLUDED.seq, hash = EXCLUDED.hash",
            (episode, n - 1, prev))
    return rows


def build(query, **kw):
    return chain_export.build_export(
        query, chain="decision_log", scope_id=kw.pop("scope", EPISODE),
        taken_at="2026-08-28T00:00:00+00:00", **kw)


class TestTheExportReadsWhatPostgresActuallyHolds:
    def test_a_real_chain_archives_and_verifies(self, conn, query):
        rows = write_chain(conn, 6)
        doc = build(query)
        assert doc["row_count"] == 6
        assert doc["links_verified_at_export"] is True
        assert chain_export.verify_export(doc) == {"ok": True, "problems": [], "checked": 4}
        assert doc["end_hash"] == rows[-1]["hash"]

    def test_jsonb_survives_the_driver_in_a_hashable_form(self, conn, query):
        """psycopg returns JSONB as a dict, asyncpg as a str. One of them hashes wrong."""
        payloads = {0: {"nested": {"b": 2, "a": [1, "x", None]}, "unicode": "café — ✓"}}
        write_chain(conn, 3, payloads=payloads)
        doc = build(query)
        assert doc["rows"][0]["payload"] == payloads[0]
        assert doc["links_verified_at_export"] is True

    def test_the_anchor_comes_from_the_anchor_table(self, conn, query):
        write_chain(conn, 4)
        with conn.cursor() as cur:
            cur.execute("UPDATE decision_log_head SET seq = 99")
        assert build(query)["anchor"]["seq"] == 99

    def test_another_episode_is_not_in_this_archive(self, conn, query):
        """One table, many chains. A scope filter that leaked would fuse two ledgers."""
        write_chain(conn, 4)
        write_chain(conn, 3, episode="ep-someone-else")
        doc = build(query)
        assert doc["row_count"] == 4
        assert chain_export.verify_export(doc)["ok"] is True

    def test_an_unknown_scope_archives_nothing_rather_than_everything(self, conn, query):
        write_chain(conn, 4)
        doc = build(query, scope="no-such-episode")
        assert doc["row_count"] == 0 and doc["anchor"] is None

    def test_a_quote_in_the_scope_id_does_not_break_out_of_the_query(self, conn, query):
        """The scope is interpolated (the callable takes SQL, not params) — so it is escaped."""
        write_chain(conn, 2)
        doc = build(query, scope="ep-'; DROP TABLE decision_log; --")
        assert doc["row_count"] == 0
        assert query("SELECT count(*) FROM decision_log")[0][0] == 2

    def test_through_seq_selects_a_real_prefix(self, conn, query):
        rows = write_chain(conn, 10)
        doc = build(query, through_seq=4)
        assert [r["seq"] for r in doc["rows"]] == [0, 1, 2, 3, 4]
        assert doc["end_hash"] == rows[4]["hash"]
        assert chain_export.verify_export(doc)["problems"] == []


class TestWhatAnArchiveOfADamagedChainSays:
    def test_a_row_edited_in_sql_exports_as_broken(self, conn, query):
        write_chain(conn, 5)
        with conn.cursor() as cur:
            cur.execute("UPDATE decision_log SET payload = '{\"step\": 99}'::jsonb WHERE seq = 2")
        doc = build(query)
        assert doc["links_verified_at_export"] is False
        assert any("do not chain" in p for p in chain_export.verify_export(doc)["problems"])

    def test_a_truncated_chain_exports_intact_and_the_anchor_says_otherwise(self, conn, query):
        """The whole reason the anchor is in the archive: the rows alone cannot tell you."""
        write_chain(conn, 8)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM decision_log WHERE seq >= 5")
        doc = build(query)
        assert doc["links_verified_at_export"] is True, "every surviving link still verifies"
        assert doc["anchor"]["seq"] == 7 and doc["through_seq"] == 4
        assert any("that is what a truncation looks like" in p
                   for p in chain_export.verify_export(doc)["problems"])
