"""Tests for `evaluation.follow_up.agent_self_gated`."""
from __future__ import annotations

from evaluation.follow_up import DEFAULT_FOLLOW_UP, agent_self_gated


def test_default_text() -> None:
    assert DEFAULT_FOLLOW_UP == "yes, please apply the fix"


def test_detects_would_you_like_me_to_apply() -> None:
    text = (
        "The pod is failing due to ImagePullBackOff. "
        "Suggested fix: update the image tag.\n\nWould you like me to apply this fix?"
    )
    assert agent_self_gated(text, tool_calls=[]) is True


def test_detects_shall_i_apply() -> None:
    assert agent_self_gated("Shall I apply this change?", tool_calls=[]) is True


def test_detects_should_i_run() -> None:
    assert agent_self_gated("Should I run the rollout undo?", tool_calls=[]) is True


def test_detects_do_you_want_me_to_patch() -> None:
    assert agent_self_gated("Do you want me to patch the deployment?", tool_calls=[]) is True


def test_detects_short_question_at_end() -> None:
    assert agent_self_gated("Pod is broken. Apply the fix?", tool_calls=[]) is True


def test_no_question_no_gate() -> None:
    text = "Pod is healthy. No issues found."
    assert agent_self_gated(text, tool_calls=["run_kubectl"]) is False


def test_question_in_middle_not_at_end_does_not_trigger() -> None:
    text = "I asked myself, would I apply it? Then I did. Pod is healthy."
    assert agent_self_gated(text, tool_calls=["run_kubectl"]) is False


def test_empty_final_text() -> None:
    assert agent_self_gated("", tool_calls=[]) is False


def test_long_final_sentence_with_question_mark_does_not_trigger() -> None:
    long = ("This is a long-running incident commentary that ends with a question mark "
            "but is far too long to be a confirmation prompt and should not be treated "
            "as a gate by the heuristic fallback path?")
    assert agent_self_gated(long, tool_calls=[]) is False


def test_write_tool_fired_without_question_no_gate() -> None:
    """Agent invoked run_kubectl AND ended without a question — not self-gated."""
    assert agent_self_gated("Patched the deployment.", tool_calls=["run_kubectl"]) is False


def test_write_tool_fired_but_then_asked_followup_does_gate() -> None:
    """Edge case: agent ran kubectl read-only AND then asked. Conservative gate."""
    text = "I described the pod. Would you like me to apply the fix to the image tag?"
    assert agent_self_gated(text, tool_calls=["run_kubectl"]) is True


def test_historical_02_imagepull_response() -> None:
    """Spot-check against the actual 02-imagepull response captured in
    `runs/postreflexion_20260429_233220/02-imagepull_response.txt`."""
    text = (
        "The pod `bad-image-app-76d9cff595-8tq4p` is attempting to pull the image "
        "`nginx:99.99.99-does-not-exist`, which does not exist in the Docker registry.\n\n"
        "### Suggested Fix:\n"
        "Update the deployment to use a valid image tag for `nginx`.\n\n"
        "Would you like me to apply this fix?"
    )
    assert agent_self_gated(text, tool_calls=[]) is True
