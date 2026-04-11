# app/modules/query_processor.py
"""
Query Processing Module

Fast, heuristic-based scope detection for KubeIntellect.

Runs before the LangGraph workflow is invoked to reject clearly out-of-scope
queries without spinning up the multi-agent system.  Uncertain queries are
passed through — the supervisor handles them as the second line of defence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OUT_OF_SCOPE_RESPONSE = (
    "I'm sorry, but this question is outside the scope of KubeIntellect. "
    "I specialize in Kubernetes operations and cluster management. "
    "Could you please ask a Kubernetes-related question instead?"
)

# Presence of any of these tokens is sufficient to pass a query through.
_K8S_KEYWORDS: frozenset[str] = frozenset({
    # Core resources
    "pod", "pods", "deployment", "deployments", "service", "services",
    "namespace", "namespaces", "node", "nodes", "cluster", "clusters",
    "container", "containers", "image", "images",
    # Config / storage
    "configmap", "configmaps", "secret", "secrets", "volume", "volumes",
    "pvc", "pv", "storageclass", "persistentvolume", "persistentvolumeclaim",
    # Networking
    "ingress", "ingresses", "networkpolicy", "endpoint", "endpoints",
    # Workload types
    "statefulset", "daemonset", "replicaset", "cronjob", "job", "jobs",
    "horizontalpodautoscaler", "hpa", "vpa",
    # Access control
    "rbac", "role", "roles", "clusterrole", "clusterroles",
    "rolebinding", "clusterrolebinding", "serviceaccount",
    # Scheduling
    "taint", "taints", "toleration", "tolerations", "affinity",
    "nodeselector", "cordon", "drain", "evict",
    # Tooling / ecosystems
    "kubectl", "helm", "k8s", "kubernetes", "kube", "kubeconfig",
    "argocd", "flux", "certmanager", "operator",
    # Operations
    "manifest", "apply", "rollout", "scale", "replica", "replicas",
    "liveness", "readiness", "probe", "resource", "limits", "requests",
    "logs", "log", "events", "event", "metrics", "prometheus",
    "chart", "values", "release",
    # Lifecycle verbs commonly paired with K8s objects
    "deploy", "restart", "upgrade",
})

# Compiled patterns matched only when no K8s keyword is found.
# Each pattern must be specific enough to avoid false positives.
_OFF_TOPIC_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(tell me a joke|write a (poem|story|essay|song|novel))\b", re.I),
    re.compile(r"\bwhat'?s (the weather|your name|the time|the date)\b", re.I),
    re.compile(r"\b(recipe|cooking|bake|baking|restaurant|movie|film)\b", re.I),
    re.compile(r"\b(sport|football|soccer|basketball|baseball|cricket)\b", re.I),
    re.compile(r"\b(stock price|stock market|crypto|bitcoin|ethereum|investment|trading)\b", re.I),
    re.compile(r"\b(solve this math|calculate \d|what is \d+ [+\-*/] \d+)\b", re.I),
]


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ScopeResult:
    """Outcome of a scope check."""
    in_scope: bool
    # Non-empty only when in_scope is False
    rejection_message: str = field(default="")


# ---------------------------------------------------------------------------
# QueryProcessor
# ---------------------------------------------------------------------------

class QueryProcessor:
    """
    Lightweight scope gate that runs before the LangGraph workflow.

    Decision logic (in order):
    1. Any K8s keyword present → in scope (fast accept).
    2. Off-topic pattern matched → out of scope (fast reject).
    3. Otherwise → in scope (uncertain; let the supervisor decide).
    """

    def check_scope(self, query: str) -> ScopeResult:
        """
        Return a ScopeResult for *query*.

        Args:
            query: The raw user message text.

        Returns:
            ScopeResult(in_scope=True) to forward to the workflow, or
            ScopeResult(in_scope=False, rejection_message=...) to return early.
        """
        if not query or not query.strip():
            return ScopeResult(in_scope=True)

        normalized = query.lower()
        tokens = set(re.findall(r"\w+", normalized))

        # Fast accept: K8s keyword found
        if tokens & _K8S_KEYWORDS:
            logger.debug("QueryProcessor: K8s keyword matched → in scope")
            return ScopeResult(in_scope=True)

        # Fast reject: clear off-topic pattern
        for pattern in _OFF_TOPIC_PATTERNS:
            if pattern.search(normalized):
                logger.info(
                    "QueryProcessor: off-topic pattern '%s' matched → rejected. "
                    "Query prefix: %.80s",
                    pattern.pattern,
                    query,
                )
                return ScopeResult(in_scope=False, rejection_message=OUT_OF_SCOPE_RESPONSE)

        # Uncertain — pass through
        logger.debug("QueryProcessor: no decisive signal → passing through to workflow")
        return ScopeResult(in_scope=True)


# Module-level singleton used by the endpoint.
query_processor = QueryProcessor()
