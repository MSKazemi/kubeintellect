"""`MEMORY_HYBRID_RETRIEVAL` fused one channel with nothing, and cost 2.6x the latency for it.

Found 2026-08-26 by the F6 shadow lane (`evaluation/f6/shadow_recall.py`), sweeping the flag over
a 208-episode corpus with 225 natural-language questions against a real Postgres. Every arm
returned a **byte-identical ranked list of corpus keys** — 0 of 225 queries moved at depth 10 —
while the flag-on arms paid a p95 of 94.6 ms against the baseline's 36.9 ms.

The cause is one word of tsquery semantics. `plainto_tsquery` **ANDs** every lexeme of its input,
and `recall_episodes` is called with the user's last message verbatim
(`app/agent/nodes/memory_loader.py`). So the `fts` CTE's `@@` filter demanded that a single
episode summary contain every non-stopword term of a question — including "explain", "clearly",
"safest", "tell" and "find". No summary ever does. The channel returned **zero rows on 225 of 225
queries**, the RRF sum therefore had exactly one term per document (`1/(60+rank)`, the trigram
channel's alone), and the fused ordering was the trigram ordering by construction. On the same
queries an OR'd query over the same lexemes matched a median of 200 of the 208 episodes.

Three things hid it for as long as the flag has existed:

  * **The unit tests drive a `FakePool`** that accepts any string as SQL and returns whatever the
    test hands it, so no test ever asked Postgres which rows the `@@` clause admits.
  * **`test_recall_hybrid_keeps_lexical_only_match` asserts the RRF *fusion arithmetic*** — that a
    row present only in the lexical channel survives — which is true of the SQL and says nothing
    about whether the channel is ever non-empty on a real query.
  * **The failure is silent by design.** An empty channel is not an error; RRF over one ranking is
    a perfectly good ranking. The flag did exactly what a working flag looks like from outside.

These tests need a real Postgres and skip without one, because that is the whole point: the
defect lives in what the database does with the query, not in what the query looks like.
"""
from __future__ import annotations

import os
import subprocess
import uuid

import pytest

from app.memory import episodes

pytestmark = pytest.mark.asyncio

DOC = ("CrashLoopBackOff on pod api-7f4 in namespace payments. The container exited 3 on every "
       "start and the readiness probe never passed.")
QUESTION = ("A pod in namespace payments is crashing repeatedly. Find the root cause, explain it "
            "clearly, and tell me the safest fix.")


def _docker_ok() -> bool:
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.fixture(scope="module")
def pg():
    """A throwaway Postgres with just the columns these queries touch.

    Deliberately not the full `schema.sql`: this asks one question about tsquery semantics, and a
    fixture that can only run where the whole product schema applies would be skipped far more
    often than it needs to be.
    """
    if not _docker_ok():
        pytest.skip("docker is not available")
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    name = f"ki-fts-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=t",
         "-e", "POSTGRES_USER=t", "-e", "POSTGRES_DB=t", "-p", f"{port}:5432",
         "postgres:16-alpine"], check=True, capture_output=True)
    try:
        import asyncio
        import time

        import asyncpg
        dsn = f"postgresql://t:t@127.0.0.1:{port}/t"

        async def _connect_when_ready():
            """Wait for a connection that SURVIVES, not for `pg_isready` to say yes.

            During initialisation the postgres image runs a temporary server so
            initdb can seed the cluster, then shuts it down and starts the real one.
            `pg_isready` inside the container answers for that bootstrap server over
            the unix socket and returns 0 while nothing is listening on TCP yet, so
            connecting on the first yes loses the race about half the time and the
            fixture errors with `ConnectionResetError` instead of skipping or waiting.
            """
            deadline = time.time() + 90
            last: Exception | None = None
            while time.time() < deadline:
                try:
                    return await asyncpg.create_pool(dsn, min_size=1, max_size=2)
                except (OSError, asyncpg.PostgresError) as exc:
                    last = exc
                    await asyncio.sleep(0.5)
            raise AssertionError(f"postgres never accepted a connection on {port}: {last}")

        async def _setup():
            pool = await _connect_when_ready()
            async with pool.acquire() as con:
                await con.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
                await con.execute(
                    "CREATE TABLE episodes (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
                    "cluster_id text, summary text, root_cause text, outcome text, "
                    "verified bool, confidence real, playbooks text[], namespace text, "
                    "started_at timestamptz DEFAULT now(), importance real)")
                await con.execute(
                    "INSERT INTO episodes (cluster_id, summary, root_cause) VALUES ($1,$2,$3)",
                    "c", DOC, "the container's entrypoint exits 3")
                for i in range(9):
                    await con.execute(
                        "INSERT INTO episodes (cluster_id, summary, root_cause) VALUES ($1,$2,$3)",
                        "c", DOC.replace("api-7f4", f"other-{i}"), "unrelated")
            return pool

        pool = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_setup())
        yield dsn
        pool.terminate()
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


async def _fetch(dsn, sql, *args):
    import asyncpg
    con = await asyncpg.connect(dsn)
    try:
        return await con.fetch(sql, *args)
    finally:
        await con.close()


@pytest.mark.skipif(os.environ.get("KI_SKIP_DOCKER_TESTS") == "1", reason="docker tests disabled")
class TestTheLexicalChannelMustBeAbleToMatch:
    async def test_the_and_form_matches_nothing_on_a_natural_language_question(self, pg):
        """The defect, pinned. Not a claim about our SQL — a claim about `plainto_tsquery`."""
        rows = await _fetch(pg,
                            "SELECT id FROM episodes WHERE cluster_id = 'c' AND "
                            "to_tsvector('english', summary || ' ' || COALESCE(root_cause,'')) "
                            "@@ plainto_tsquery('english', $1)", QUESTION)
        assert rows == [], ("if this ever passes, plainto_tsquery has stopped ANDing and the "
                            "reasoning behind the fix below no longer applies")

    async def test_the_shipped_channel_now_matches(self, pg):
        """The same question, through the query the product actually issues."""
        sql = (f"SELECT id FROM episodes WHERE cluster_id = 'c' AND "
               f"to_tsvector('english', summary || ' ' || COALESCE(root_cause,'')) "
               f"@@ {episodes._FTS_QUERY}")
        rows = await _fetch(pg, sql, QUESTION)
        assert rows, "the lexical channel is empty again — RRF has nothing to fuse"

    async def test_the_channel_ranks_rather_than_merely_admits(self, pg):
        """OR-matching is only useful because `ts_rank` orders what it admits.

        A channel that returns ten rows in insertion order is not a second opinion. The episode
        that names the pod in the question must come first.
        """
        sql = (f"SELECT summary FROM episodes WHERE cluster_id = 'c' AND "
               f"to_tsvector('english', summary || ' ' || COALESCE(root_cause,'')) "
               f"@@ {episodes._FTS_QUERY} "
               f"ORDER BY ts_rank(to_tsvector('english', summary || ' ' || "
               f"COALESCE(root_cause,'')), {episodes._FTS_QUERY}) DESC, started_at DESC")
        rows = await _fetch(pg, sql, QUESTION + " The workload is api-7f4.")
        assert "api-7f4" in rows[0]["summary"], [r["summary"][:60] for r in rows[:3]]

    async def test_the_whole_hybrid_query_runs_against_a_real_database(self, pg):
        """The FakePool cannot reject malformed SQL; Postgres can. Both variants, both parsed."""
        for sql in (episodes._SQL_RECALL_HYBRID, episodes._SQL_RECALL_HYBRID_IMP):
            rows = await _fetch(pg, sql, QUESTION, "c", 3, episodes._RRF_K, 0.02)
            assert len(rows) <= 3

    async def test_a_query_with_no_content_words_is_an_empty_channel_not_an_error(self, pg):
        """`string_agg` over no lexemes is NULL, and `@@ NULL` matches nothing.

        That is the right answer for a question with nothing in it, and it must not raise — the
        non-hybrid path turns an exception into `MemoryUnavailable` and degrades the turn.
        """
        rows = await _fetch(pg, episodes._SQL_RECALL_HYBRID, "the a of and", "c", 3,
                            episodes._RRF_K, 0.02)
        assert isinstance(rows, list)

    async def test_a_lexeme_that_needs_quoting_does_not_break_the_cast(self, pg):
        """`tsvector_to_array` yields raw lexemes; `-01` and `crashloop-gold` are not bare-legal."""
        rows = await _fetch(pg, episodes._SQL_RECALL_HYBRID,
                            "namespace h6-01-crashloop-gold workload 01-crashloop-gold",
                            "c", 3, episodes._RRF_K, 0.02)
        assert isinstance(rows, list)


class TestTheChannelIsDefinedOnceSoTheFilterAndTheRankingCannotDisagree:
    async def test_both_uses_come_from_the_same_constant(self):
        # A filter that admits one set and a ranking that scores another is a silent reordering.
        assert episodes._SQL_RECALL_HYBRID.count(episodes._FTS_QUERY) == 2

    async def test_the_and_form_is_gone_from_every_recall_query(self):
        for name in ("_SQL_RECALL_TRGM", "_SQL_RECALL_TRGM_IMP",
                     "_SQL_RECALL_HYBRID", "_SQL_RECALL_HYBRID_IMP"):
            assert "plainto_tsquery" not in getattr(episodes, name), name

    async def test_the_importance_variant_still_carries_the_channel(self):
        # `_SQL_RECALL_HYBRID_IMP` is built by string-replacing the plain variant; a change to
        # the fts CTE that broke that derivation would leave the P6 arm on the old query.
        assert episodes._SQL_RECALL_HYBRID_IMP.count(episodes._FTS_QUERY) == 2
        assert "e.importance" in episodes._SQL_RECALL_HYBRID_IMP
