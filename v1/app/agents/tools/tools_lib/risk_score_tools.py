"""
Deployment risk scoring tools.

Scores every deployment in the cluster (or a given namespace) against a set of
operational best-practice rules and returns the top riskiest deployments.

Scoring rules:
  +3  Missing resource limits (cpu_limit or memory_limit absent on any container)
  +2  Missing liveness probe on any container
  +2  Single replica (replicas == 1)
  +1  :latest image tag on any container
"""

from typing import Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.agents.tools.tools_lib._base import (
    get_apps_v1_api,
    _handle_k8s_exceptions,
)
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")


# ===============================================================================
#                               INPUT SCHEMA
# ===============================================================================

class DeploymentRiskScoreInput(BaseModel):
    namespace: Optional[str] = Field(
        default=None,
        description=(
            "Kubernetes namespace to scan. "
            "If omitted, all namespaces are scanned."
        ),
    )
    top_n: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Number of highest-risk deployments to return. Defaults to 5.",
    )


# ===============================================================================
#                               SCORING LOGIC
# ===============================================================================

_PENALTY_MISSING_LIMITS = 3
_PENALTY_MISSING_LIVENESS = 2
_PENALTY_SINGLE_REPLICA = 2
_PENALTY_LATEST_TAG = 1


def _score_deployment(deployment) -> dict:
    """Return a risk entry dict for a single deployment object."""
    name = deployment.metadata.name
    namespace = deployment.metadata.namespace
    spec = deployment.spec
    replicas = spec.replicas if spec.replicas is not None else 1
    containers = spec.template.spec.containers or []

    score = 0
    reasons = []

    # Rule: single replica
    if replicas == 1:
        score += _PENALTY_SINGLE_REPLICA
        reasons.append(f"single replica (+{_PENALTY_SINGLE_REPLICA})")

    for c in containers:
        # Rule: missing resource limits
        has_cpu_limit = bool(
            c.resources
            and c.resources.limits
            and c.resources.limits.get("cpu")
        )
        has_mem_limit = bool(
            c.resources
            and c.resources.limits
            and c.resources.limits.get("memory")
        )
        if not has_cpu_limit or not has_mem_limit:
            score += _PENALTY_MISSING_LIMITS
            missing = []
            if not has_cpu_limit:
                missing.append("cpu_limit")
            if not has_mem_limit:
                missing.append("memory_limit")
            reasons.append(
                f"container '{c.name}' missing {', '.join(missing)} (+{_PENALTY_MISSING_LIMITS})"
            )
            break  # one penalty per deployment regardless of container count

    # Rule: missing liveness probe — checked independently of other rules
    liveness_missing = any(c.liveness_probe is None for c in containers)
    if liveness_missing:
        score += _PENALTY_MISSING_LIVENESS
        reasons.append(f"missing liveness probe (+{_PENALTY_MISSING_LIVENESS})")

    # Rule: :latest image tag
    for c in containers:
        image = c.image or ""
        tag = image.split(":")[-1] if ":" in image else "latest"
        if tag == "latest":
            score += _PENALTY_LATEST_TAG
            reasons.append(
                f"container '{c.name}' uses :latest tag (+{_PENALTY_LATEST_TAG})"
            )
            break  # one penalty per deployment

    return {
        "name": name,
        "namespace": namespace,
        "replicas": replicas,
        "score": score,
        "reasons": reasons,
    }


# ===============================================================================
#                               TOOL FUNCTION
# ===============================================================================

@_handle_k8s_exceptions
def score_deployment_risk(
    namespace: Optional[str] = None,
    top_n: int = 5,
) -> str:
    """Score every deployment in the cluster and return the top riskiest."""
    apps_v1 = get_apps_v1_api()

    if namespace:
        deployments = apps_v1.list_namespaced_deployment(namespace).items
    else:
        deployments = apps_v1.list_deployment_for_all_namespaces().items

    if not deployments:
        scope = f"namespace '{namespace}'" if namespace else "all namespaces"
        return f"No deployments found in {scope}."

    scored = [_score_deployment(d) for d in deployments]
    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:top_n]

    scope = f"namespace '{namespace}'" if namespace else "all namespaces"
    total = len(scored)
    lines = [
        f"Deployment Risk Scores — top {len(top)} of {total} in {scope}",
        "(+3 missing resource limits, +2 missing liveness probe, +2 single replica, +1 :latest tag)",
        "",
    ]
    for rank, entry in enumerate(top, 1):
        lines.append(
            f"{rank}. {entry['namespace']}/{entry['name']}  "
            f"score={entry['score']}  replicas={entry['replicas']}"
        )
        for reason in entry["reasons"]:
            lines.append(f"   - {reason}")
        if not entry["reasons"]:
            lines.append("   - (no penalties — low risk)")

    # Summary line: how many zero-risk deployments
    zero_risk = sum(1 for e in scored if e["score"] == 0)
    if zero_risk:
        lines.append(f"\n{zero_risk}/{total} deployment(s) have a risk score of 0.")

    return "\n".join(lines)


# ===============================================================================
#                               TOOL INSTANCE
# ===============================================================================

score_deployment_risk_tool = StructuredTool.from_function(
    func=score_deployment_risk,
    name="score_deployment_risk",
    description=(
        "Compute a deployment risk score for every deployment in the cluster "
        "(or a specific namespace). Penalises: missing resource limits (+3), "
        "missing liveness probe (+2), single replica (+2), :latest image tag (+1). "
        "Returns the top N riskiest deployments with scores and reasons."
    ),
    args_schema=DeploymentRiskScoreInput,
)

risk_score_tools = [score_deployment_risk_tool]
