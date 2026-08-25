"""The P6 importance-ranked *baseline* recall query is not a query Postgres can run.

Found offline 2026-08-24 while diagnosing the OpsMemBench H1 lane, by replaying the
campaign's own exported episode rows through the shipped SQL against a real Postgres 17.

`_SQL_RECALL_TRGM_IMP` is built by rewriting the baseline's ORDER BY to weight the
relevance score by importance (ADR-017 R6.1)::

    ORDER BY sim DESC, started_at DESC
 -> ORDER BY sim * (0.5 + 0.5 * COALESCE(importance, 0.5)) DESC, started_at DESC

`sim` is a SELECT-list *alias*. SQL lets an output alias stand alone as a sort key, but
never inside a larger expression, so the rewritten query raises
`UndefinedColumnError: column "sim" does not exist` on every call. It is the query used
whenever `MEMORY_IMPORTANCE` is on and `MEMORY_HYBRID_RETRIEVAL` is off — i.e. the whole
`V5--MEMORY_HYBRID_RETRIEVAL` ablation arm.

Two things hid it. The unit tests drive `recall_episodes` through a `FakePool` that
accepts any string as SQL, so no test ever asked Postgres to parse it. And the failure
was inaudible: on the non-hybrid path `recall_episodes` caught the exception and
returned `[]`, which `render_recall_block` turns into an empty prompt section and the
log line reports as `episodes=0` — indistinguishable from "this cluster has no similar
incidents". That is precisely the split `MemoryUnavailable` was introduced to make, and
the hybrid path already makes it; the baseline path did not.
"""
from __future__ import annotations

import re

import pytest

from app.memory import episodes

RECALL_SQL = {
    "_SQL_RECALL_TRGM": episodes._SQL_RECALL_TRGM,
    "_SQL_RECALL_TRGM_IMP": episodes._SQL_RECALL_TRGM_IMP,
    "_SQL_RECALL_HYBRID": episodes._SQL_RECALL_HYBRID,
    "_SQL_RECALL_HYBRID_IMP": episodes._SQL_RECALL_HYBRID_IMP,
}


def _final_select_aliases(sql: str) -> set[str]:
    """Names introduced by `AS x` in the statement's OUTERMOST select list.

    Only those are affected: a CTE's output column (`fused.rrf`) is a real column to the
    outer query and may be used in an expression, which is why the hybrid variant's
    `ORDER BY f.rrf * …` is legal while the baseline's `ORDER BY sim * …` is not.
    """
    head = sql[: sql.rindex("ORDER BY")]
    body = head[head.rindex("SELECT") :]
    body = body[: body.index("FROM")] if "FROM" in body else body
    return set(re.findall(r"\bAS\s+(\w+)", body))


def _aliases_used_inside_an_expression(sql: str) -> list[str]:
    """Outer-select aliases that appear in the ORDER BY as anything but a bare sort key."""
    clause = sql[sql.rindex("ORDER BY") + len("ORDER BY") :]
    clause = clause[: clause.index("LIMIT")] if "LIMIT" in clause else clause
    bad = []
    for alias in _final_select_aliases(sql):
        for m in re.finditer(rf"(?<!\.)\b{re.escape(alias)}\b", clause):
            rest = clause[m.end() :].lstrip()
            if not re.match(r"(?i)^(DESC\b|ASC\b|,|$)", rest):
                bad.append(f"{alias} -> followed by {rest[:24]!r}")
    return bad


@pytest.mark.parametrize("name", sorted(RECALL_SQL))
def test_no_recall_query_sorts_by_an_expression_over_a_select_list_alias(name):
    """A SELECT-list alias inside an ORDER BY expression is not runnable SQL.

    This is the defect's shape, checked statically so it is caught in CI, where no
    Postgres is available to reject the query for us.
    """
    bad = _aliases_used_inside_an_expression(RECALL_SQL[name])
    assert not bad, (
        f"{name} sorts by an expression over its own output alias, which Postgres "
        f"rejects with 'column does not exist': {bad}. Repeat the underlying "
        f"expression (or promote it to a CTE column) instead of reusing the alias."
    )


class _RaisingPool:
    """A pool whose `fetch` fails the way a malformed query does."""

    def __init__(self):
        self.calls: list[str] = []

    async def fetch(self, sql, *args):
        self.calls.append(sql)
        raise RuntimeError('column "sim" does not exist')


async def test_a_baseline_recall_that_failed_is_not_reported_as_no_similar_incidents(mocker):
    """The baseline path must say it could not answer, not answer "nothing".

    `MemoryUnavailable` exists to keep a failed lookup distinct from an empty one; the
    hybrid path raises it once both channels are gone. The baseline path returned `[]`,
    so a query Postgres cannot even parse reached the model as an *absence of prior
    incidents* — the one signal this product is differentiated on, silently missing.
    """
    mocker.patch.object(episodes.settings, "MEMORY_HYBRID_RETRIEVAL", False)
    pool = _RaisingPool()
    episodes.init_episodes(pool)
    try:
        with pytest.raises(episodes.MemoryUnavailable):
            await episodes.recall_episodes("payments-api OOMKilled again", "c1")
    finally:
        episodes.close_episodes()


async def test_a_healthy_baseline_recall_still_returns_its_rows(mocker):
    """CONTROL — must pass before and after the fix, so the two above pin the defect."""
    mocker.patch.object(episodes.settings, "MEMORY_HYBRID_RETRIEVAL", False)

    class _Pool:
        async def fetch(self, sql, *args):
            return [{"id": "ep-1", "summary": "oomkilled payments-api", "root_cause": None,
                     "outcome": "resolved", "verified": True, "confidence": 0.8,
                     "playbooks": [], "namespace": "payments", "started_at": None,
                     "sim": 0.42}]

    episodes.init_episodes(_Pool())
    try:
        out = await episodes.recall_episodes("payments-api OOMKilled again", "c1")
        assert [r["id"] for r in out] == ["ep-1"]
    finally:
        episodes.close_episodes()
