# tests/test_tool_output_summarizer.py
"""
Unit tests for ToolOutputSummarizer.

Tests cover:
- Fast path (already under budget → no-op)
- SUMMARIZE_TOOL_OUTPUTS=False bypass
- LOG tool: anchor extraction preserves traceback + kernel kill line
- LOG tool: tail-only truncation when no anchor present
- Non-LOG tool: head-kept truncation
- Structured fields never truncated
- Non-JSON fallback
- Unknown tool name falls back to GENERIC budget
"""

import json
import re
import unittest
from unittest.mock import patch

import tiktoken

enc = tiktoken.encoding_for_model("gpt-4o")
def count(s):
    return len(enc.encode(s))


def _strip_footer(result: str) -> str:
    """Remove the truncation footer line before JSON parsing."""
    marker = "\n[Output truncated:"
    idx = result.rfind(marker)
    return result[:idx] if idx != -1 else result


# ---------------------------------------------------------------------------
# Helpers to build realistic log payloads
# ---------------------------------------------------------------------------

def _make_log_output(logs: str, pod="test-pod", namespace="default", container="app") -> str:
    return json.dumps({
        "status": "success",
        "data": {"pod": pod, "namespace": namespace, "container": container, "logs": logs},
    }, indent=2)


def _oomkilled_log_lines(n: int = 100) -> list[str]:
    """Produce n lines of a realistic OOMKilled Python log."""
    lines: list[str] = []
    # Startup
    lines += [
        "2026-04-02T09:14:01.000000000Z stdout F INFO:root:Starting worker pid=1",
        "2026-04-02T09:14:01.100000000Z stdout F INFO:root:Loading model weights",
        "2026-04-02T09:14:02.000000000Z stdout F INFO:root:Model loaded (2.1 GB). Ready.",
    ]
    # Memory pressure noise
    for i in range(20):
        lines.append(
            f"2026-04-02T09:14:{3+i:02d}.000000000Z stderr F "
            f"[gc] [info][heap,alloc] Heap allocation failed, heap size: {2800+i*10} MB"
        )
    # Python traceback anchor
    lines += [
        "2026-04-02T09:14:25.001000000Z stderr F Traceback (most recent call last):",
        "2026-04-02T09:14:25.002000000Z stderr F   File \"/app/worker.py\", line 89, in _run",
        "2026-04-02T09:14:25.003000000Z stderr F     tensor = torch.zeros((32, 512, 1024), dtype=torch.float32)",
        "2026-04-02T09:14:25.004000000Z stderr F MemoryError: Unable to allocate 8.2 GiB",
        "2026-04-02T09:14:25.100000000Z stderr F ERROR:root:Recovery failed. Entering shutdown.",
    ]
    # More GC noise between traceback and kill
    for i in range(25):
        lines.append(
            f"2026-04-02T09:14:{26+i//5:02d}.{(i%5)*200000000:09d}Z stderr F "
            f"[gc] [info][heap,alloc] Heap allocation failed, heap size: {3000+i*2} MB"
        )
    # Kernel kill at the very end
    lines += [
        "2026-04-02T09:14:31.001000000Z stderr F kernel: Out of memory: Killed process 1 (python3) "
        "total-vm:4982148kB, anon-rss:3141632kB",
        "2026-04-02T09:14:31.002000000Z stderr F kernel: oom_reaper: reaped process 1 (python3)",
        "2026-04-02T09:14:31.500000000Z stdout F INFO:root:Metrics flushed. Exiting with code 137.",
    ]
    # Pad / trim to exactly n lines
    while len(lines) < n:
        lines.insert(3, f"2026-04-02T09:14:02.{len(lines):09d}Z stdout F INFO:root:Startup step {len(lines)}")
    return lines[:n]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestToolOutputSummarizerFastPath(unittest.TestCase):

    def setUp(self):
        from app.services.tool_output_summarizer import ToolOutputSummarizer
        self.s = ToolOutputSummarizer()

    def test_short_output_unchanged(self):
        """Output under budget passes through verbatim."""
        raw = _make_log_output("just one log line\n")
        result = self.s.summarize("get_pod_logs", raw)
        self.assertEqual(result, raw)

    def test_bypass_flag_skips_truncation(self):
        """SUMMARIZE_TOOL_OUTPUTS=False returns raw output regardless of size."""
        long_logs = "\n".join([f"line {i}: " + "x" * 40 for i in range(300)])
        raw = _make_log_output(long_logs)
        with patch("app.services.tool_output_summarizer.settings") as mock_settings:
            mock_settings.SUMMARIZE_TOOL_OUTPUTS = False
            result = self.s.summarize("get_pod_logs", raw)
        self.assertEqual(result, raw)


class TestToolOutputSummarizerLogTool(unittest.TestCase):

    def setUp(self):
        from app.services.tool_output_summarizer import ToolOutputSummarizer, BUDGET_LOG
        self.s = ToolOutputSummarizer()
        self.budget = BUDGET_LOG

    def _run(self, lines):
        raw = _make_log_output("\n".join(lines))
        result = self.s.summarize("get_pod_logs", raw)
        tokens = count(result)
        return result, tokens

    def test_within_budget_after_truncation(self):
        """Result must be at or below BUDGET_LOG tokens."""
        lines = _oomkilled_log_lines(100)
        _, tokens = self._run(lines)
        self.assertLessEqual(tokens, self.budget + 50)  # +50 slack for marker overhead

    def test_kernel_kill_line_preserved(self):
        """kernel: Out of memory: Killed must always appear in the truncated output."""
        lines = _oomkilled_log_lines(100)
        result, _ = self._run(lines)
        data = json.loads(_strip_footer(result))
        logs = data["data"]["logs"]
        self.assertIn("Out of memory: Killed", logs)

    def test_traceback_anchor_preserved(self):
        """'Traceback (most recent call last):' anchor block must survive truncation."""
        lines = _oomkilled_log_lines(100)
        result, _ = self._run(lines)
        data = json.loads(_strip_footer(result))
        logs = data["data"]["logs"]
        self.assertIn("Traceback (most recent call last):", logs)

    def test_memory_error_line_preserved(self):
        """The specific MemoryError line immediately after the traceback must survive."""
        lines = _oomkilled_log_lines(100)
        result, _ = self._run(lines)
        data = json.loads(_strip_footer(result))
        logs = data["data"]["logs"]
        self.assertIn("MemoryError: Unable to allocate", logs)

    def test_oom_reaper_preserved(self):
        """kernel: oom_reaper line must survive (it appears after the kill line)."""
        lines = _oomkilled_log_lines(100)
        result, _ = self._run(lines)
        data = json.loads(_strip_footer(result))
        logs = data["data"]["logs"]
        self.assertIn("oom_reaper", logs)

    def test_startup_noise_dropped(self):
        """Early startup lines ('Loading model weights') must not appear in truncated output."""
        lines = _oomkilled_log_lines(100)
        result, _ = self._run(lines)
        data = json.loads(_strip_footer(result))
        logs = data["data"]["logs"]
        self.assertNotIn("Loading model weights", logs)

    def test_omission_marker_present(self):
        """When lines are dropped, an explicit omission marker must appear."""
        lines = _oomkilled_log_lines(100)
        result, _ = self._run(lines)
        data = json.loads(_strip_footer(result))
        logs = data["data"]["logs"]
        self.assertIn("lines from startup omitted", logs)

    def test_structured_fields_intact(self):
        """pod, namespace, container, status must pass through unchanged."""
        lines = _oomkilled_log_lines(100)
        raw = _make_log_output(
            "\n".join(lines), pod="my-pod", namespace="prod", container="worker"
        )
        result = self.s.summarize("get_pod_logs", raw)
        data = json.loads(_strip_footer(result))
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["data"]["pod"], "my-pod")
        self.assertEqual(data["data"]["namespace"], "prod")
        self.assertEqual(data["data"]["container"], "worker")

    def test_no_anchor_pure_tail(self):
        """Log with no exception anchor: should keep tail, drop head."""
        lines = [f"2026-04-02T09:14:{i:02d}.000000000Z stdout F INFO:root:Routine step {i}"
                 for i in range(100)]
        last_line = lines[-1]
        result, _ = self._run(lines)
        data = json.loads(_strip_footer(result))
        logs = data["data"]["logs"]
        self.assertIn(last_line, logs)
        self.assertIn("lines from startup omitted", logs)


class TestToolOutputSummarizerNonLogTool(unittest.TestCase):

    def setUp(self):
        from app.services.tool_output_summarizer import ToolOutputSummarizer, BUDGET_STRUCTURED_READ
        self.s = ToolOutputSummarizer()
        self.budget = BUDGET_STRUCTURED_READ

    def test_structured_read_truncates_from_tail(self):
        """STRUCTURED_READ tools truncate from the tail, keeping the head."""
        # Build a describe output with a very long 'describe' free-text field
        long_desc = "\n".join([f"Detail line {i}: some condition data" for i in range(200)])
        raw = json.dumps({
            "status": "success",
            "data": {"pod": "p", "namespace": "n", "describe": long_desc},
        }, indent=2)
        result = self.s.summarize("describe_resource", raw)
        tokens = count(result)
        self.assertLessEqual(tokens, self.budget + 50)
        data = json.loads(_strip_footer(result))
        # Head should be kept
        self.assertIn("Detail line 0:", data["data"]["describe"])
        # Tail should be dropped
        self.assertNotIn("Detail line 199:", data["data"]["describe"])
        self.assertIn("lines truncated", data["data"]["describe"])


class TestToolOutputSummarizerGenericFallback(unittest.TestCase):

    def setUp(self):
        from app.services.tool_output_summarizer import ToolOutputSummarizer, BUDGET_GENERIC
        self.s = ToolOutputSummarizer()
        self.budget = BUDGET_GENERIC

    def test_unknown_tool_uses_generic_budget(self):
        """An unrecognised tool name falls back to BUDGET_GENERIC."""
        long_output = json.dumps({
            "status": "success",
            "data": {"raw_output": "\n".join([f"output line {i}" for i in range(500)])},
        }, indent=2)
        result = self.s.summarize("gen_some_custom_tool", long_output)
        tokens = count(result)
        self.assertLessEqual(tokens, self.budget + 50)


class TestToolOutputSummarizerNonJSON(unittest.TestCase):

    def setUp(self):
        from app.services.tool_output_summarizer import ToolOutputSummarizer, BUDGET_LOG
        self.s = ToolOutputSummarizer()
        self.budget = BUDGET_LOG

    def test_non_json_log_output_truncated_from_head(self):
        """Non-JSON output from a LOG tool is treated as a text blob, tail kept."""
        # Plain text (not JSON) log output
        lines = [f"2026-04-02T09:14:{i:02d}.000Z stderr F log line {i}" for i in range(200)]
        lines.append("2026-04-02T09:14:99.000Z stderr F kernel: Out of memory: Killed process 1")
        raw = "\n".join(lines)
        result = self.s.summarize("get_pod_logs", raw)
        tokens = count(result)
        self.assertLessEqual(tokens, self.budget + 50)
        self.assertIn("Out of memory: Killed", result)
        self.assertIn("lines from startup omitted", result)


class TestTruncationFooter(unittest.TestCase):
    """Footer '[Output truncated: N of M tokens shown ...]' must appear on all
    three truncation paths and must be absent when no truncation occurs."""

    def setUp(self):
        from app.services.tool_output_summarizer import ToolOutputSummarizer
        self.s = ToolOutputSummarizer()

    # ── Path 1: LOG tool, no anchor (pure tail) ──────────────────────────────

    def test_footer_present_log_no_anchor(self):
        """LOG tool with no exception anchor triggers pure-tail truncation → footer required."""
        lines = [f"2026-04-02T09:14:{i:02d}.000Z stdout F INFO:root:step {i}" for i in range(100)]
        raw = _make_log_output("\n".join(lines))
        result = self.s.summarize("get_pod_logs", raw)
        self.assertIn("[Output truncated:", result)
        self.assertIn("tokens shown", result)
        self.assertIn("narrow your query", result)

    # ── Path 2: LOG tool, anchor path ────────────────────────────────────────

    def test_footer_present_log_with_anchor(self):
        """LOG tool with exception anchor triggers anchor+tail truncation → footer required."""
        lines = _oomkilled_log_lines(100)
        raw = _make_log_output("\n".join(lines))
        result = self.s.summarize("get_pod_logs", raw)
        self.assertIn("[Output truncated:", result)
        self.assertIn("tokens shown", result)

    # ── Path 3: non-LOG tool (head-keep) ─────────────────────────────────────

    def test_footer_present_structured_read(self):
        """STRUCTURED_READ tool exceeding budget → head-keep truncation → footer required."""
        long_desc = "\n".join([f"Detail line {i}: data" for i in range(300)])
        raw = json.dumps({
            "status": "success",
            "data": {"pod": "p", "namespace": "n", "describe": long_desc},
        }, indent=2)
        result = self.s.summarize("describe_resource", raw)
        self.assertIn("[Output truncated:", result)
        self.assertIn("tokens shown", result)

    # ── No truncation: footer must be absent ─────────────────────────────────

    def test_footer_absent_when_no_truncation(self):
        """Output already under budget must not get the footer."""
        raw = _make_log_output("just one log line\n")
        result = self.s.summarize("get_pod_logs", raw)
        self.assertNotIn("[Output truncated:", result)

    # ── Token counts in footer are correct ───────────────────────────────────

    def test_footer_token_counts_format(self):
        """Footer must contain 'N of M tokens shown' where both N and M are integers."""
        lines = _oomkilled_log_lines(100)
        raw = _make_log_output("\n".join(lines))
        result = self.s.summarize("get_pod_logs", raw)
        import re
        match = re.search(r"\[Output truncated: (\d+) of (\d+) tokens shown", result)
        self.assertIsNotNone(match, "Footer token-count pattern not found")
        shown, total = int(match.group(1)), int(match.group(2))
        self.assertGreater(total, shown, "total tokens must exceed shown tokens")
        self.assertGreater(shown, 0, "shown tokens must be positive")


class TestToolOutputSummarizerListTool(unittest.TestCase):
    """Tests for ToolType.LIST — item-level truncation with semantic annotation."""

    def setUp(self):
        from app.services.tool_output_summarizer import (
            ToolOutputSummarizer, BUDGET_LIST, BUDGET_BY_TOOL,
        )
        self.s = ToolOutputSummarizer()
        self.budget = BUDGET_LIST
        self.pod_budget = BUDGET_BY_TOOL.get("list_all_pods_across_namespaces", BUDGET_LIST)

    def _make_pod_list(self, n: int, tool="list_all_pods_across_namespaces") -> tuple[str, str]:
        """Return (raw_json, tool_name) for a list of n pods."""
        pods = [
            {
                "name": f"pod-{i:04d}",
                "namespace": f"ns-{i % 5}",
                "status": "Running",
                "node": f"node-{i % 3}",
            }
            for i in range(n)
        ]
        raw = json.dumps({"status": "success", "total_count": n, "data": pods}, indent=2)
        return raw, tool

    def test_small_list_passes_through_unchanged(self):
        """A list already under budget is returned verbatim."""
        raw, tool = self._make_pod_list(5)
        result = self.s.summarize(tool, raw)
        self.assertEqual(result, raw)

    def test_large_list_within_budget_after_truncation(self):
        """Result must be at or below the effective budget for the tool (per-tool override or type default)."""
        raw, tool = self._make_pod_list(200)
        result = self.s.summarize(tool, raw)
        tokens = count(result)
        self.assertLessEqual(tokens, self.pod_budget + 60)  # +60 slack for annotation overhead

    def test_total_count_preserved_in_json(self):
        """total_count in the JSON must always equal the original item count, never the shown count."""
        raw, tool = self._make_pod_list(200)
        result = self.s.summarize(tool, raw)
        # Strip annotation line before parsing
        json_part = result.split("\n[Showing")[0]
        data = json.loads(json_part)
        self.assertEqual(data["total_count"], 200)

    def test_shown_items_fewer_than_total(self):
        """After truncation, data array must have fewer items than total_count."""
        raw, tool = self._make_pod_list(200)
        result = self.s.summarize(tool, raw)
        json_part = result.split("\n[Showing")[0]
        data = json.loads(json_part)
        self.assertLess(len(data["data"]), 200)

    def test_semantic_annotation_present_on_truncation(self):
        """Truncation must emit a '[Showing N of M pods — filter ...]' annotation."""
        raw, tool = self._make_pod_list(200)
        result = self.s.summarize(tool, raw)
        self.assertIn("[Showing", result)
        self.assertIn("of 200 pods", result)
        self.assertIn("filter by namespace", result)

    def test_annotation_absent_when_no_truncation(self):
        """No annotation when all items fit within budget."""
        raw, tool = self._make_pod_list(5)
        result = self.s.summarize(tool, raw)
        self.assertNotIn("[Showing", result)

    def test_annotation_counts_are_correct(self):
        """The 'Showing N of M' numbers in the annotation must be accurate."""
        raw, tool = self._make_pod_list(200)
        result = self.s.summarize(tool, raw)
        match = re.search(r"\[Showing (\d+) of (\d+) pods", result)
        self.assertIsNotNone(match, "Annotation pattern not found")
        shown, total = int(match.group(1)), int(match.group(2))
        self.assertEqual(total, 200)
        self.assertGreater(shown, 0)
        self.assertLess(shown, 200)
        # Verify shown matches actual data length
        json_part = result.split("\n[Showing")[0]
        data = json.loads(json_part)
        self.assertEqual(len(data["data"]), shown)

    def test_resource_type_label_correct_for_nodes(self):
        """Node list tool should emit 'N of M nodes' annotation."""
        nodes = [{"name": f"node-{i}", "status": "True", "roles": [], "age_days": i, "version": "v1.31"}
                 for i in range(100)]
        raw = json.dumps({"status": "success", "total_count": 100, "data": nodes}, indent=2)
        result = self.s.summarize("list_kubernetes_nodes", raw)
        if "[Showing" in result:
            self.assertIn("nodes", result)

    def test_non_list_data_falls_back_to_generic(self):
        """When `data` is a dict (not a list), fall back to GENERIC truncation."""
        from app.services.tool_output_summarizer import BUDGET_GENERIC
        # configmaps returns data as a dict
        data = {
            "status": "success",
            "total_count": 50,
            "data": {
                "configmaps_by_namespace": {},
                "raw_output": "\n".join([f"line {i}: " + "x" * 80 for i in range(300)])
            }
        }
        raw = json.dumps(data, indent=2)
        result = self.s.summarize("list_configmaps", raw)
        tokens = count(result)
        self.assertLessEqual(tokens, BUDGET_GENERIC + 60)


class TestWrappedDictToolSummarization(unittest.TestCase):
    """
    Verify that _wrap_tool_with_summarizer intercepts dict-returning tools.

    This covers the bug where list tools returning Dict[str, Any] bypassed the
    summarizer entirely because the wrapper only handled str results. The fix
    serializes the dict, passes it through the summarizer, and returns the
    annotated string when truncation occurs.
    """

    def _make_fake_list_tool(self, n: int):
        """Build a minimal StructuredTool-like object whose func returns a pod-list dict."""
        from unittest.mock import MagicMock
        tool = MagicMock()
        tool.name = "list_all_pods_across_namespaces"
        tool._summarizer_wrapped = False  # ensure wrapping is not skipped
        pods = [
            {"name": f"pod-{i}", "namespace": "default", "status": "Running", "node": "node-0"}
            for i in range(n)
        ]
        tool.func = lambda *a, **kw: {
            "status": "success",
            "total_count": n,
            "data": pods,
        }
        return tool

    def test_dict_result_is_intercepted_when_large(self):
        """A large dict result from a list tool must be summarized and return a str with annotation."""
        from app.orchestration.agents import _wrap_tool_with_summarizer, _SUMMARIZER_WRAPPED
        tool = self._make_fake_list_tool(200)
        # Manually mark as not wrapped so _wrap_tool_with_summarizer will process it
        setattr(tool, _SUMMARIZER_WRAPPED, False)

        wrapped = _wrap_tool_with_summarizer(tool)
        result = wrapped.func()

        # Must return a string (not dict) because truncation occurred
        self.assertIsInstance(result, str)
        # Must contain the semantic annotation
        self.assertIn("[Showing", result)
        self.assertIn("of 200 pods", result)

    def test_dict_result_unchanged_when_small(self):
        """A small dict result that fits within budget must be returned as the original dict."""
        from app.orchestration.agents import _wrap_tool_with_summarizer, _SUMMARIZER_WRAPPED
        tool = self._make_fake_list_tool(3)
        setattr(tool, _SUMMARIZER_WRAPPED, False)

        wrapped = _wrap_tool_with_summarizer(tool)
        result = wrapped.func()

        # Must return the original dict (no truncation = type preserved)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["total_count"], 3)


class TestListToolTotalCount(unittest.TestCase):
    """Verify that list tool functions include total_count in their return value."""

    def _mock_pod(self, name="test-pod", namespace="default", phase="Running", node="node-1"):
        from unittest.mock import MagicMock
        pod = MagicMock()
        pod.metadata.name = name
        pod.metadata.namespace = namespace
        pod.metadata.labels = {}
        pod.status.phase = phase
        pod.status.container_statuses = []
        pod.spec.node_name = node
        pod.spec.containers = []
        return pod

    def test_list_all_pods_total_count(self):
        """list_all_pods must include total_count == number of pods returned."""
        from unittest.mock import MagicMock, patch
        from app.agents.tools.tools_lib.pod_tools import list_all_pods

        mock_pods = MagicMock()
        mock_pods.items = [self._mock_pod(f"pod-{i}") for i in range(15)]

        with patch("app.agents.tools.tools_lib.pod_tools.get_core_v1_api") as mock_api:
            mock_api.return_value.list_pod_for_all_namespaces.return_value = mock_pods
            result = list_all_pods()

        self.assertEqual(result["total_count"], 15)
        self.assertEqual(len(result["data"]), 15)

    def test_list_pods_in_namespace_total_count(self):
        """list_pods_in_namespace must include total_count == number of pods."""
        from unittest.mock import MagicMock, patch
        from app.agents.tools.tools_lib.pod_tools import list_pods_in_namespace

        mock_pods = MagicMock()
        mock_pods.items = [self._mock_pod(f"pod-{i}") for i in range(7)]

        with patch("app.agents.tools.tools_lib.pod_tools.get_core_v1_api") as mock_api:
            mock_api.return_value.list_namespaced_pod.return_value = mock_pods
            result = list_pods_in_namespace("default")

        self.assertEqual(result["total_count"], 7)
        self.assertEqual(len(result["data"]), 7)


if __name__ == "__main__":
    unittest.main()
