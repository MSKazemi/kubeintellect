"""The ledger recorded the change under one cluster id and the prior looked under another.

Change-first RCA has two halves in `cortex/graph.py`, and they resolved the cluster id
differently:

    write (the ledger append)   cid = state.get("cluster_id") or get_cluster_id()
    read  (the prompt prior)    recent_changes(state.get("cluster_id") or "")

`""` is a key nothing is ever recorded under. Any state whose `cluster_id` is empty therefore
wrote to the real cluster and read from nowhere — and `app/sensorium/watchdog_dispatch.py`
builds exactly that state (`"cluster_id": ""`) for every watchdog-dispatched investigation,
which is the automated path this feature exists for.

Measured 2026-08-24, with `get_cluster_id()` resolving to `prod-eu-1`:

    write  keyed on 'prod-eu-1'   recorded=1
    read   keyed on ''            found=0
    prior injected into the prompt: ''

An empty prior renders as no block at all, so the model was never told about the change that
had just been applied — the silence is the same silence a cluster with no changes makes.

`app/cluster_id.py` already carries the warning this fix needed: `UNRESOLVED_CLUSTER_ID` is
exported *"so callers can recognise it rather than hardcoding the string (the SQL guards
elsewhere exclude `''`, which this function never returns — a mismatch that made those guards
look like they covered this)"*. The read side had invented a third value on top of that.

Scope, stated plainly: both halves sit behind `CORTEX_V5_ENABLED` + `KI_V5_CHANGE_LEDGER` /
`KI_V5_CHANGE_FIRST_RCA`, all default-off, so this was wrong on the v5 path only.
"""

from __future__ import annotations

import time

import pytest

from app.cortex.change_rca import ChangeRecord, recent_changes, render_change_prior
from app.memory import change_ledger

RESOLVED = "prod-eu-1"
CMD = "kubectl set image deploy/web web=web:v3"


@pytest.fixture(autouse=True)
def _clean_ledger(mocker):
    """The ledger is process-global, and so is the `_installed` latch that wires it into
    `change_rca`. Clearing only the dict leaves the latch set, so a later
    `record_from_commands` skips `install_as_change_source()` and the source stays whatever
    the previous test left behind — `_clear()` resets all three."""
    change_ledger._clear()
    mocker.patch("app.cluster_id.get_cluster_id", lambda: RESOLVED)
    yield
    change_ledger._clear()


def _write(state: dict) -> str:
    """The ledger append exactly as `gather_once`'s mutation branch does it."""
    from app.cluster_id import get_cluster_id
    cid = state.get("cluster_id") or get_cluster_id()
    change_ledger.record_from_commands(cid, [CMD], time.time())
    return cid


def _read(state: dict) -> str:
    """The prompt prior exactly as `gather_once` does it."""
    from app.cluster_id import get_cluster_id
    cluster_id = state.get("cluster_id") or get_cluster_id()
    return render_change_prior(recent_changes(cluster_id))


# ── 1. the two halves agree ───────────────────────────────────────────────────────────────────


class TestTheReadFindsWhatTheWriteRecorded:
    def test_an_empty_cluster_id_still_finds_the_change(self):
        """The defect, in one test: this returned '' while the ledger held the change."""
        state = {"cluster_id": ""}
        assert _write(state) == RESOLVED
        prior = _read(state)
        assert prior, "the prior was empty for a state whose change had just been recorded"
        assert "deploy/web" in prior

    def test_a_missing_cluster_id_key_behaves_the_same(self):
        state: dict = {}
        assert _write(state) == RESOLVED
        assert "deploy/web" in _read(state)

    def test_an_explicit_cluster_id_is_honoured_over_the_fallback(self):
        """The fallback must not override a state that knows its own cluster."""
        state = {"cluster_id": "staging-us"}
        assert _write(state) == "staging-us"
        assert "deploy/web" in _read(state)
        # and it stays scoped — the resolved id must not see the staging change
        assert recent_changes(RESOLVED) == []

    def test_the_watchdog_state_is_the_one_that_was_broken(self):
        """Not a hypothetical: this is the literal state `watchdog_dispatch` builds."""
        from app.sensorium.watchdog_dispatch import _watchdog_state
        from app.sensorium.change_watchdog import WatchdogTask

        task = WatchdogTask(target="deploy/web", kind="image",
                            objective="did the rollout hold?", created_epoch=0.0,
                            ttl_seconds=600, dedup_key="default/image/deploy/web")
        state = _watchdog_state(task)
        assert state["cluster_id"] == "", "the premise changed — re-read this test"
        _write(state)
        assert "deploy/web" in _read(state)


# ── 2. the prior still stays silent when it should ────────────────────────────────────────────


class TestSilenceIsStillEarned:
    """Vacuity guard: a prior that always rendered would pass everything above."""

    def test_a_cluster_with_no_changes_injects_nothing(self):
        assert _read({"cluster_id": ""}) == ""

    def test_another_clusters_change_is_not_borrowed(self):
        _write({"cluster_id": "some-other-cluster"})
        assert _read({"cluster_id": ""}) == ""


# ── 3. the resolution is shared, not duplicated ───────────────────────────────────────────────


class TestBothHalvesResolveTheSameWay:
    def test_the_source_no_longer_mentions_the_empty_key(self):
        """The literal `or ""` is what made the two halves disagree; a future edit that
        reintroduces it on this line reintroduces the defect."""
        from pathlib import Path
        src = Path(__file__).resolve().parents[1]
        graph = (src / "packages/kubeintellect-server/app/cortex/graph.py").read_text()
        line = next(line for line in graph.splitlines() if "render_change_prior(recent_changes(" in line)
        assert 'or ""' not in line, line

    def test_an_unresolvable_cluster_uses_the_shared_sentinel(self, mocker):
        """When nothing identifies the cluster, `get_cluster_id()` returns the sentinel — and
        both halves then agree on *that*, so the prior is degraded but not silently lost."""
        from app.cluster_id import UNRESOLVED_CLUSTER_ID
        mocker.patch("app.cluster_id.get_cluster_id", lambda: UNRESOLVED_CLUSTER_ID)
        state = {"cluster_id": ""}
        assert _write(state) == UNRESOLVED_CLUSTER_ID
        assert "deploy/web" in _read(state)


# ── 4. the renderer's own contract, unchanged ─────────────────────────────────────────────────


class TestTheRendererIsUnaffected:
    def test_no_changes_is_still_an_empty_block(self):
        assert render_change_prior([]) == ""

    def test_changes_still_render_most_recent_first(self):
        old = ChangeRecord("image", "deploy/a", 100.0, "", "old")
        new = ChangeRecord("image", "deploy/b", 200.0, "", "new")
        block = render_change_prior([old, new])
        assert block.index("deploy/b") < block.index("deploy/a")


# ── 5. the real node, not a replica ───────────────────────────────────────────────────────────


class TestTheGatherNodeItself:
    """Everything above re-creates `gather_once`'s two lines. This class runs the node.

    A replica can drift from the code it models — and a replica that drifted would have kept
    passing while the defect came back. These capture the system prompt the specialist LLM is
    actually handed.
    """

    @pytest.fixture
    def prompt_for(self, mocker):
        from app.cortex import graph as cortex_graph

        mocker.patch.object(cortex_graph.settings, "CORTEX_V5_ENABLED", True)
        mocker.patch.object(cortex_graph.settings, "KI_V5_CHANGE_FIRST_RCA", True)
        mocker.patch.object(cortex_graph.settings, "KI_V5_RUNBOOK_SKILLS", False)
        mocker.patch.object(cortex_graph, "emit", mocker.AsyncMock())

        captured: list[str] = []

        class _LLM:
            def bind_tools(self, _tools):
                return self

            async def ainvoke(self, messages, _config):
                captured.append(messages[0].content)
                from langchain_core.messages import AIMessage
                return AIMessage(content="ok")

        mocker.patch("app.cortex.models.get_specialist_llm", lambda: _LLM())

        async def _run(cluster_id):
            from langchain_core.messages import HumanMessage
            state = {
                "session_id": "s1",
                "messages": [HumanMessage(content="why is web down?")],
                "cluster_id": cluster_id,
            }
            await cortex_graph.gather_once(state, {})
            return captured[-1]

        return _run

    @pytest.mark.asyncio
    async def test_the_prior_reaches_the_prompt_for_an_empty_cluster_id(self, prompt_for):
        change_ledger.record_from_commands(RESOLVED, [CMD], time.time())
        system = await prompt_for("")
        assert "Recent changes (consider these FIRST" in system
        assert "deploy/web" in system

    @pytest.mark.asyncio
    async def test_no_changes_still_leaves_the_prompt_alone(self, prompt_for):
        system = await prompt_for("")
        assert "Recent changes" not in system

    @pytest.mark.asyncio
    async def test_a_state_that_knows_its_cluster_is_not_overridden(self, prompt_for):
        """The fallback is a fallback. A multi-cluster deployment would otherwise read one
        cluster's changes into another cluster's investigation — a worse failure than the
        empty prior this pass fixed, so it gets its own guard on the real node."""
        change_ledger.record_from_commands("staging-us", [CMD], time.time())
        system = await prompt_for("staging-us")
        assert "deploy/web" in system
