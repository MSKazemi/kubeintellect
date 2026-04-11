"""
Unit tests for app.modules.query_processor.QueryProcessor.

Zero external dependencies — pure Python logic, no LLM calls.
"""
import pytest
from app.modules.query_processor import QueryProcessor, OUT_OF_SCOPE_RESPONSE


@pytest.fixture()
def qp():
    return QueryProcessor()


# ── Fast-accept: K8s keyword present ──────────────────────────────────────

@pytest.mark.parametrize("query", [
    "show me all pods",
    "list deployments in namespace default",
    "what is the status of my cluster",
    "how do I scale a replicaset",
    "kubectl get nodes",
    "check the logs for pod xyz",
    "SHOW ALL PODS",           # uppercase should still match
    "Check Logs for pod xyz",  # mixed case
    "apply this manifest",
    "describe the ingress",
])
def test_k8s_keyword_in_scope(qp, query):
    result = qp.check_scope(query)
    assert result.in_scope is True
    assert result.rejection_message == ""


# ── Fast-reject: clear off-topic patterns ─────────────────────────────────

@pytest.mark.parametrize("query", [
    "tell me a joke",
    "write a poem about spring",
    "write a story about dragons",
    "what's the weather today",
    "what's your name",
    "give me a recipe for pasta",
    "who won the football game",
    "what is the bitcoin price",
    "stock market analysis",
    "solve this math problem: 2+2",
])
def test_off_topic_rejected(qp, query):
    result = qp.check_scope(query)
    assert result.in_scope is False
    assert result.rejection_message == OUT_OF_SCOPE_RESPONSE


def test_rejection_message_is_non_empty_string(qp):
    result = qp.check_scope("tell me a joke")
    assert isinstance(result.rejection_message, str)
    assert len(result.rejection_message) > 0


# ── Pass-through: uncertain / ambiguous queries ────────────────────────────

@pytest.mark.parametrize("query", [
    "how are you",
    "what can you do",
    "help me",
    "hello",
    "what is wrong with my environment",  # "environment" has no K8s keyword, no off-topic match
])
def test_uncertain_query_passes_through(qp, query):
    result = qp.check_scope(query)
    assert result.in_scope is True


# ── Edge cases ─────────────────────────────────────────────────────────────

def test_empty_string_passes_through(qp):
    assert qp.check_scope("").in_scope is True


def test_whitespace_only_passes_through(qp):
    assert qp.check_scope("   \t\n").in_scope is True


def test_result_has_empty_rejection_message_when_in_scope(qp):
    result = qp.check_scope("list pods")
    assert result.rejection_message == ""
