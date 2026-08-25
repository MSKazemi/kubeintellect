"""Every layer that shortens output must say so in the words its reader was told to watch for.

Pass 253 found the coordinator's trimmer emitting "chars trimmed" while the instruction four
hundred lines up the same file named `[truncated` and `chars omitted`. This file is the general
form of that defect: two of the six shortening sites in the codebase used wording no prompt
names — `helm_tool` wrote `[... N chars truncated]`, and the cortex subagent bound wrote
`…[summary truncated …]` — and the three cortex prompts named no vocabulary at all, so on that
route a conforming marker was still read by a model that had never been told what it meant.

The tools are enumerated from `ALL_TOOLS` rather than listed here, so a tool added later with a
cap of its own is covered by this file on the day it is registered, not on the day someone
remembers to add it.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from app.tools.output_policy import (
    MARKER_PATTERNS,
    PARTIAL_CONTEXT_CLAUSE,
    POLICY_LINE_RE,
    TRUNCATION_CLAUSE,
    truncation_marker,
)
from app.tools.registry import ALL_TOOLS

# The contract, written out here rather than imported, because every other test in this file uses
# `MARKER_PATTERNS` as its oracle — an oracle that imports the thing it checks gets weaker exactly
# when the thing under test gets weaker.
VOCABULARY = ("[truncated", "chars omitted")

# Enough rows to blow past every cap in the codebase (the largest is 8 000 chars).
FLOOD = "\n".join(f"line-{i:05d} some text that makes this row wide enough" for i in range(600))


def conforms(text: str) -> bool:
    return any(p in text for p in MARKER_PATTERNS)


def marker_of(output: str) -> str | None:
    """The line in `output` that claims something was left out, if any."""
    hits = [ln for ln in output.splitlines() if "truncat" in ln.lower() or "omitted" in ln.lower()]
    return hits[-1] if hits else None


def drive(tool_name: str) -> str:
    """Run one registered tool over its cap and return what it really produced."""
    proc = MagicMock(stdout=FLOOD, stderr="", returncode=0)
    if tool_name == "run_kubectl":
        from app.tools.kubectl_tool import run_kubectl
        with patch("subprocess.run", return_value=proc):
            return run_kubectl.invoke({"command": "kubectl get pods -n default", "stdin": None})
    if tool_name == "run_helm":
        from app.tools.helm_tool import run_helm
        with patch("subprocess.run", return_value=proc):
            return run_helm.invoke({"command": "helm list -n default"})
    if tool_name == "query_prometheus":
        from app.tools.prometheus_tool import query_prometheus
        payload = {"status": "success", "data": {"resultType": "vector", "result": [
            {"metric": {"__name__": "up", "instance": f"instance-{i:05d}.example.internal",
                        "job": "kubelet", "namespace": "monitoring"}, "value": [0, "1"]}
            for i in range(400)]}}
        with patch("httpx.Client", return_value=_client(payload)):
            return query_prometheus.invoke({"promql": "up"})
    if tool_name == "query_loki":
        from app.tools.loki_tool import query_loki
        payload = {"status": "success", "data": {"resultType": "streams", "result": [
            {"stream": {"app": "x"}, "values": [[str(i), f"log line {i} " + "x" * 40]
                                                for i in range(400)]}]}}
        with patch("httpx.Client", return_value=_client(payload)):
            return query_loki.invoke({"logql": '{app="x"}'})
    pytest.skip(f"no driver for {tool_name} — add one rather than deleting this skip")


def _client(payload: dict) -> MagicMock:
    resp = MagicMock(status_code=200, text=json.dumps(payload))
    resp.json.return_value = payload
    client = MagicMock()
    client.__enter__.return_value.get.return_value = resp
    client.__enter__.return_value.post.return_value = resp
    return client


TOOL_NAMES = [t.name for t in ALL_TOOLS]


class TestEveryRegisteredToolThatShortensSaysSo:
    @pytest.mark.parametrize("name", TOOL_NAMES)
    def test_a_tool_that_shortened_its_output_used_the_shared_vocabulary(self, name):
        out = drive(name)
        marker = marker_of(out)
        if marker is None:
            # Not shortening is a fine answer — this test is about what a shortener says.
            return
        assert conforms(marker), f"{name} announced a loss in words no prompt names: {marker!r}"

    @pytest.mark.parametrize("name", TOOL_NAMES)
    def test_that_announcement_is_also_protected_from_the_downstream_trims(self, name):
        marker = marker_of(drive(name))
        if marker is None:
            return
        assert POLICY_LINE_RE.search(marker), (
            f"{name}'s marker is not recognised as a policy line, so the coordinator and cortex "
            f"bounds will treat it as an ordinary row and cut it: {marker!r}"
        )

    @pytest.mark.parametrize("name", TOOL_NAMES)
    def test_a_reported_loss_is_never_zero(self, name):
        # "[truncated: 0 chars omitted]" conforms to every pattern and tells the reader nothing
        # was lost, on output that was shortened.
        marker = marker_of(drive(name))
        if marker is None or "[truncated: " not in marker:
            return
        reported = marker.split("[truncated: ")[1].split(" ")[0]
        assert reported.isdigit() and int(reported) > 0, marker

    def test_helm_reports_exactly_what_it_dropped(self):
        from app.tools.helm_tool import _OUTPUT_CAP
        marker = marker_of(drive("run_helm"))
        reported = int(marker.split("[truncated: ")[1].split(" ")[0])
        assert reported == len(FLOOD) - _OUTPUT_CAP

    def test_the_registry_is_not_empty(self):
        # The parametrized tests above pass vacuously if this ever collapses.
        assert len(TOOL_NAMES) >= 4


class TestTheSitesThatAreNotTools:
    def test_the_subagent_summary_bound(self):
        from app.cortex.harness.subagent import bound_summary
        text, truncated = bound_summary(FLOOD)
        assert truncated
        assert conforms(marker_of(text) or "")

    def test_the_subagent_marker_counts_what_it_dropped(self):
        from app.cortex.harness.subagent import bound_summary
        text, _ = bound_summary(FLOOD)
        marker = marker_of(text)
        omitted = int(marker.split("[truncated: ")[1].split(" ")[0])
        assert omitted > 0
        assert omitted <= len(FLOOD)

    def test_the_coordinator_trimmer(self):
        from app.agent.nodes.coordinator import _trim_tool_output
        out = _trim_tool_output("NAME  STATUS\n" + FLOOD)
        assert conforms(marker_of(out) or "")


class TestEveryPromptThatReadsToolOutputNamesTheVocabulary:
    @pytest.mark.parametrize("attr", ["_GATHER_SYSTEM", "_SYNTHESIS_SYSTEM"])
    def test_the_cortex_tiers_that_see_tool_results(self, attr):
        from app.cortex import graph
        assert TRUNCATION_CLAUSE in getattr(graph, attr)

    def test_the_coordinator(self):
        from app.agent.nodes.coordinator import _COORDINATOR_SYSTEM
        assert TRUNCATION_CLAUSE in _COORDINATOR_SYSTEM

    def test_the_clause_names_exactly_the_strings_the_emitters_produce(self):
        for pattern in MARKER_PATTERNS:
            assert pattern in TRUNCATION_CLAUSE

    def test_the_vocabulary_itself_has_not_been_narrowed(self):
        assert MARKER_PATTERNS == VOCABULARY

    def test_triage_is_told_what_partial_context_means(self):
        # Triage answers in strict JSON; "include a visible warning" would be an instruction to
        # corrupt its own output, so it gets the inference rule instead of the phrasing.
        assert "investigate" in PARTIAL_CONTEXT_CLAUSE
        assert "[truncated" in PARTIAL_CONTEXT_CLAUSE and "[Protected]" in PARTIAL_CONTEXT_CLAUSE


class TestTheTriageSnapshotIsBoundedOutLoud:
    def _triage_system(self, snapshot: str) -> str:
        import asyncio
        from app.cortex.graph import triage
        captured = {}

        class FakeLLM:
            async def ainvoke(self, messages, *a, **k):
                captured["system"] = messages[0].content
                return MagicMock(content=json.dumps({"mode": "chat", "plan": []}))

        class Human:
            type = "human"
            content = "how many pods are running?"

        async def noemit(*a, **k):
            return None

        with patch("app.cortex.models.get_triage_llm", return_value=FakeLLM()), \
             patch("app.cortex.graph.emit", new=noemit):
            asyncio.run(triage({"session_id": "s", "messages": [Human()],
                                "cluster_snapshot": snapshot, "memory_context": "",
                                "matched_playbooks": []}, {}))
        return captured["system"]

    def _snapshot_block(self, snapshot: str) -> str:
        """Only the snapshot part of the prompt.

        The prompt now *also* contains `PARTIAL_CONTEXT_CLAUSE`, which quotes `[truncated` — so a
        test that searched the whole system message would find a marker whether or not the
        snapshot carried one, and pass while proving nothing.
        """
        system = self._triage_system(snapshot)
        return system[system.index("## Cluster snapshot"):]

    def test_a_cut_snapshot_says_it_was_cut(self):
        assert conforms(marker_of(self._snapshot_block(FLOOD)) or "")

    def test_the_withheld_sentence_reaches_triage(self):
        from app.tools.namespace_guard import withheld_sentence
        assert "[Protected]" in self._snapshot_block(FLOOD + withheld_sentence(3, "namespace"))

    def test_triage_actually_receives_the_partial_context_rule(self):
        # Asserting the constant's wording proves the constant; this proves the wiring. The two
        # have come apart before — a fix whose test only checked that the import existed.
        assert PARTIAL_CONTEXT_CLAUSE in self._triage_system(FLOOD)

    def test_a_snapshot_that_fits_is_not_annotated(self):
        assert marker_of(self._snapshot_block("NAME  STATUS\npod-1  Running")) is None

    def test_the_snapshot_is_still_bounded(self):
        from app.cortex.graph import _TRIAGE_SNAPSHOT_MAX_CHARS
        assert len(self._snapshot_block(FLOOD)) < len(FLOOD)
        assert _TRIAGE_SNAPSHOT_MAX_CHARS < len(FLOOD)


class TestTheMarkerHelper:
    def test_the_default_unit_matches_both_patterns(self):
        m = truncation_marker(12)
        assert all(p in m for p in MARKER_PATTERNS)

    @pytest.mark.parametrize("unit", ["chars", "rows", "lines", "namespaces"])
    def test_every_unit_still_matches_the_first_pattern(self, unit):
        assert MARKER_PATTERNS[0] in truncation_marker(3, unit)

    def test_the_hint_is_optional_and_never_swallows_the_count(self):
        assert truncation_marker(7) == "[truncated: 7 chars omitted]"
        assert "7 chars omitted" in truncation_marker(7, hint="narrow the query")

    def test_it_is_recognised_as_a_policy_line(self):
        assert POLICY_LINE_RE.search(truncation_marker(1, "rows", "x"))
