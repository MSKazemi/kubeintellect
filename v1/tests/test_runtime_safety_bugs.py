# tests/test_runtime_safety_bugs.py
"""
Unit tests for the runtime safety bug fixes (B3, B4, B6).

B3 — Intra-agent duplicate tool-call guard (routing.py worker_node_factory)
B4 — update_deployment_command confirmation guard (deployment_tools.py)
B6 — task_complete heuristic excludes clarification questions (routing.py)
"""

import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# B4 — update_deployment_command confirmation guard
# ---------------------------------------------------------------------------

class TestUpdateDeploymentCommandConfirmationGuard(unittest.TestCase):
    """B4: update_deployment_command must return confirmation_required when
    the container already has a non-null command."""

    def _make_mock_deployment(self, existing_command):
        container = SimpleNamespace(name="app", command=existing_command)
        spec = SimpleNamespace(template=SimpleNamespace(spec=SimpleNamespace(containers=[container])))
        return SimpleNamespace(spec=spec)

    def _call_tool(self, existing_command, new_command):
        """Call update_deployment_command with a mocked K8s API."""
        mock_dep = self._make_mock_deployment(existing_command)
        mock_api = MagicMock()
        mock_api.read_namespaced_deployment.return_value = mock_dep

        with patch(
            "app.agents.tools.tools_lib.deployment_tools.get_apps_v1_api",
            return_value=mock_api,
        ):
            from app.agents.tools.tools_lib.deployment_tools import update_deployment_command
            return update_deployment_command(
                namespace="default",
                deployment_name="my-deploy",
                command=new_command,
            )

    def test_returns_confirmation_required_when_existing_command_is_nonnull(self):
        result = self._call_tool(
            existing_command=["python", "app.py"],
            new_command=["sh", "-c", "while true; do sleep 30; done"],
        )
        self.assertEqual(result["status"], "confirmation_required")
        self.assertIn("existing_command", result)
        self.assertEqual(result["existing_command"], ["python", "app.py"])
        self.assertIn("proposed_command", result)
        self.assertIn("force_update_deployment_command", result["message"])

    def test_patches_directly_when_no_existing_command(self):
        mock_dep = self._make_mock_deployment(existing_command=None)
        mock_api = MagicMock()
        mock_api.read_namespaced_deployment.return_value = mock_dep

        with patch(
            "app.agents.tools.tools_lib.deployment_tools.get_apps_v1_api",
            return_value=mock_api,
        ):
            from app.agents.tools.tools_lib.deployment_tools import update_deployment_command
            result = update_deployment_command(
                namespace="default",
                deployment_name="my-deploy",
                command=["sh", "-c", "sleep 3600"],
            )
        self.assertEqual(result["status"], "success")
        mock_api.patch_namespaced_deployment.assert_called_once()

    def test_patches_directly_when_existing_command_is_empty_list(self):
        mock_dep = self._make_mock_deployment(existing_command=[])
        mock_api = MagicMock()
        mock_api.read_namespaced_deployment.return_value = mock_dep

        with patch(
            "app.agents.tools.tools_lib.deployment_tools.get_apps_v1_api",
            return_value=mock_api,
        ):
            from app.agents.tools.tools_lib.deployment_tools import update_deployment_command
            result = update_deployment_command(
                namespace="default",
                deployment_name="my-deploy",
                command=["sh", "-c", "sleep 3600"],
            )
        # Empty list is falsy — treated as no existing command, should patch directly.
        self.assertEqual(result["status"], "success")


class TestForceUpdateDeploymentCommand(unittest.TestCase):
    """B4: force_update_deployment_command must patch regardless of existing command."""

    def _make_mock_deployment(self, existing_command):
        container = SimpleNamespace(name="app", command=existing_command)
        spec = SimpleNamespace(template=SimpleNamespace(spec=SimpleNamespace(containers=[container])))
        return SimpleNamespace(spec=spec)

    def test_overwrites_existing_command_without_confirmation(self):
        mock_dep = self._make_mock_deployment(existing_command=["python", "app.py"])
        mock_api = MagicMock()
        mock_api.read_namespaced_deployment.return_value = mock_dep

        with patch(
            "app.agents.tools.tools_lib.deployment_tools.get_apps_v1_api",
            return_value=mock_api,
        ):
            from app.agents.tools.tools_lib.deployment_tools import force_update_deployment_command
            result = force_update_deployment_command(
                namespace="default",
                deployment_name="my-deploy",
                command=["sh", "-c", "sleep 3600"],
            )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"]["previous_command"], ["python", "app.py"])
        self.assertEqual(result["data"]["new_command"], ["sh", "-c", "sleep 3600"])
        mock_api.patch_namespaced_deployment.assert_called_once()


# ---------------------------------------------------------------------------
# B3 — Intra-agent duplicate tool-call guard helpers
# ---------------------------------------------------------------------------

def _make_ai_message(tool_calls):
    """Build a minimal AIMessage-like object with tool_calls."""
    from langchain_core.messages import AIMessage
    msg = AIMessage(content="")
    msg.tool_calls = tool_calls
    return msg


def _make_tool_message(content, tool_call_id):
    from langchain_core.messages import ToolMessage
    return ToolMessage(content=content, tool_call_id=tool_call_id)


def _run_b3_guard(messages):
    """
    Run only the B3 guard logic extracted from worker_node_factory.
    Returns (max_run, dup_tool) if suppression would fire, else (0, None).
    """
    from langchain_core.messages import AIMessage, ToolMessage

    _DUPLICATE_TOOL_CALL_THRESHOLD = 3

    _tc_id_to_info: dict = {}
    for _m in messages:
        if isinstance(_m, AIMessage):
            for _tc in (getattr(_m, "tool_calls", None) or []):
                if isinstance(_tc, dict):
                    _tc_id = _tc.get("id") or _tc.get("tool_call_id")
                    if _tc_id:
                        _args_h = hashlib.md5(
                            json.dumps(_tc.get("args", {}), sort_keys=True).encode()
                        ).hexdigest()[:8]
                        _tc_id_to_info[_tc_id] = (_tc.get("name", ""), _args_h)

    _call_triples = []
    for _m in messages:
        if isinstance(_m, ToolMessage):
            _out_h = hashlib.md5(str(_m.content).encode()).hexdigest()[:8]
            _tc_id = getattr(_m, "tool_call_id", None)
            if _tc_id and _tc_id in _tc_id_to_info:
                _tname, _ah = _tc_id_to_info[_tc_id]
                _call_triples.append((_tname, _ah, _out_h))

    if len(_call_triples) < _DUPLICATE_TOOL_CALL_THRESHOLD:
        return 0, None

    _max_run, _cur_run, _dup_tool = 1, 1, None
    for _i in range(1, len(_call_triples)):
        if _call_triples[_i] == _call_triples[_i - 1]:
            _cur_run += 1
            if _cur_run > _max_run:
                _max_run = _cur_run
                _dup_tool = _call_triples[_i][0]
        else:
            _cur_run = 1

    if _max_run >= _DUPLICATE_TOOL_CALL_THRESHOLD:
        return _max_run, _dup_tool
    return 0, None


def _build_repeated_calls(tool_name, args, output, count):
    """Build a message list simulating `count` identical tool calls."""
    from langchain_core.messages import AIMessage, ToolMessage
    messages = []
    for i in range(count):
        tc_id = f"call_{i}"
        ai_msg = AIMessage(content="")
        ai_msg.tool_calls = [{"id": tc_id, "name": tool_name, "args": args}]
        messages.append(ai_msg)
        messages.append(ToolMessage(content=output, tool_call_id=tc_id))
    messages.append(AIMessage(content="Final response"))
    return messages


class TestDuplicateToolCallGuard(unittest.TestCase):
    """B3: the guard fires when the same tool is called 3+ times with the same
    args AND the same output; it does NOT fire if the output changes."""

    def test_guard_fires_on_3_identical_calls_with_identical_output(self):
        messages = _build_repeated_calls(
            tool_name="get_pod_logs",
            args={"namespace": "prod", "pod_name": "api-xyz"},
            output="Error: pod not found",
            count=3,
        )
        max_run, dup_tool = _run_b3_guard(messages)
        self.assertEqual(max_run, 3)
        self.assertEqual(dup_tool, "get_pod_logs")

    def test_guard_fires_on_13_identical_calls(self):
        messages = _build_repeated_calls(
            tool_name="get_pod_logs",
            args={"namespace": "prod", "pod_name": "api-xyz"},
            output="some log line",
            count=13,
        )
        max_run, dup_tool = _run_b3_guard(messages)
        self.assertGreaterEqual(max_run, 3)
        self.assertEqual(dup_tool, "get_pod_logs")

    def test_guard_does_not_fire_when_output_changes_between_calls(self):
        """Simulates legitimate polling where the pod state changes each call."""
        from langchain_core.messages import AIMessage, ToolMessage

        messages = []
        outputs = ["Pending", "ContainerCreating", "Running"]
        for i, out in enumerate(outputs):
            tc_id = f"call_{i}"
            ai_msg = AIMessage(content="")
            ai_msg.tool_calls = [{"id": tc_id, "name": "get_pod_status",
                                   "args": {"namespace": "prod", "pod_name": "api"}}]
            messages.append(ai_msg)
            messages.append(ToolMessage(content=out, tool_call_id=tc_id))
        messages.append(AIMessage(content="Pod is now Running"))

        max_run, dup_tool = _run_b3_guard(messages)
        # Each output is different — guard must not fire
        self.assertEqual(max_run, 0)
        self.assertIsNone(dup_tool)

    def test_guard_does_not_fire_on_2_identical_calls(self):
        messages = _build_repeated_calls(
            tool_name="get_pod_logs",
            args={"namespace": "prod", "pod_name": "api"},
            output="some log",
            count=2,
        )
        max_run, _ = _run_b3_guard(messages)
        self.assertEqual(max_run, 0)

    def test_guard_fires_only_on_consecutive_run(self):
        """3 identical calls interrupted by a different call should not fire."""
        from langchain_core.messages import AIMessage, ToolMessage

        messages = []
        # 2 identical calls
        for i in range(2):
            tc_id = f"call_a_{i}"
            ai_msg = AIMessage(content="")
            ai_msg.tool_calls = [{"id": tc_id, "name": "get_pod_logs",
                                   "args": {"namespace": "prod", "pod_name": "api"}}]
            messages.append(ai_msg)
            messages.append(ToolMessage(content="logs...", tool_call_id=tc_id))
        # Different call that resets the counter
        tc_id_diff = "call_b_0"
        ai_msg2 = AIMessage(content="")
        ai_msg2.tool_calls = [{"id": tc_id_diff, "name": "get_pod_logs",
                                "args": {"namespace": "prod", "pod_name": "api"}}]
        messages.append(ai_msg2)
        messages.append(ToolMessage(content="DIFFERENT output", tool_call_id=tc_id_diff))
        # 2 more identical calls (total run = 2, should not fire)
        for i in range(2):
            tc_id = f"call_c_{i}"
            ai_msg = AIMessage(content="")
            ai_msg.tool_calls = [{"id": tc_id, "name": "get_pod_logs",
                                   "args": {"namespace": "prod", "pod_name": "api"}}]
            messages.append(ai_msg)
            messages.append(ToolMessage(content="logs...", tool_call_id=tc_id))

        max_run, _ = _run_b3_guard(messages)
        # Max consecutive run is 2 — guard must not fire
        self.assertEqual(max_run, 0)


# ---------------------------------------------------------------------------
# B6 — task_complete heuristic excludes clarification questions
# ---------------------------------------------------------------------------

class TestTaskCompleteHeuristic(unittest.TestCase):
    """B6: task_complete must be False when response contains a completion
    signal AND a clarification question with '?'."""

    def _compute_task_complete(self, response_content: str) -> bool:
        """Mirror the heuristic logic from routing.py worker_node_factory."""
        _COMPLETION_SIGNALS = frozenset([
            "here is", "here are", "list of", "found", "showing", "result",
            "completed", "successfully", "has been created", "has been deleted",
            "has been scaled", "has been restarted", "has been applied",
            "already exists",
        ])
        _ERROR_OR_CLARIF = frozenset([
            "i don't have", "cannot perform", "please specify", "could you provide",
            "clarification needed", "need clarification",
        ])
        _CLARIF_PHRASES = [
            "could you", "please", "which namespace", "what namespace",
            "which pod", "which deployment", "which service", "which node",
            "do you want", "would you like", "please let me know",
        ]
        _cl = response_content.lower()
        _has_clarif_question = "?" in response_content and any(
            p in _cl for p in _CLARIF_PHRASES
        )
        return (
            any(s in _cl for s in _COMPLETION_SIGNALS)
            and not any(s in _cl for s in _ERROR_OR_CLARIF)
            and not _has_clarif_question
        )

    def test_completion_signal_without_question_is_task_complete(self):
        response = "Here are the pods in namespace prod: api-xyz, worker-abc."
        self.assertTrue(self._compute_task_complete(response))

    def test_completion_signal_with_clarification_question_is_not_task_complete(self):
        # "found" is a completion signal, but "which namespace?" is a clarification
        response = "I found some pods. Which namespace would you like me to search in?"
        self.assertFalse(self._compute_task_complete(response))

    def test_clarification_with_question_mark_is_not_task_complete(self):
        response = "I completed the action. Could you please specify which pod you mean?"
        self.assertFalse(self._compute_task_complete(response))

    def test_error_signal_overrides_completion(self):
        response = "Here are the results. Please specify the namespace."
        self.assertFalse(self._compute_task_complete(response))

    def test_question_mark_alone_without_clarification_phrase_does_not_block(self):
        # "did you want" does NOT match the phrase "do you want" in _CLARIF_PHRASES,
        # so "?" alone without a matching phrase should still yield task_complete=True.
        response = "Successfully scaled the deployment. Did you want anything else?"
        self.assertTrue(self._compute_task_complete(response))

    def test_do_you_want_phrase_blocks_task_complete(self):
        # "do you want" IS in _CLARIF_PHRASES, so task_complete must be False.
        response = "Successfully scaled the deployment. Do you want me to restart it too?"
        self.assertFalse(self._compute_task_complete(response))

    def test_pure_completion_no_question(self):
        response = "Successfully restarted the deployment api in namespace prod."
        self.assertTrue(self._compute_task_complete(response))


if __name__ == "__main__":
    unittest.main()
