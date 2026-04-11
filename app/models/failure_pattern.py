# app/models/failure_pattern.py
"""
Pydantic model for Kubernetes failure patterns.

Patterns are seeded at deploy time and updated at runtime as the system
observes real failures.  They drive the pre-query hint injection in
workflow.py (3D-5) so the supervisor starts with targeted checks rather
than reasoning from scratch.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, Field


class FailurePattern(BaseModel):
    """A recognized Kubernetes failure mode with diagnostic and remediation guidance."""

    pattern_id: str = Field(
        ...,
        description="Unique stable identifier, e.g. 'oomkilled_memory_limit'.",
    )
    type: str = Field(
        ...,
        description=(
            "Failure category label used for display and grouping, "
            "e.g. 'OOMKilled', 'CrashLoopBackOff', 'NodeNotReady'."
        ),
    )
    signals: List[str] = Field(
        ...,
        min_length=1,
        description=(
            "Keyword signals that characterise this failure pattern. "
            "Used for keyword-overlap matching against the user query. "
            "Minimum 4 signals per pattern."
        ),
    )
    root_cause: str = Field(
        ...,
        description="One-sentence explanation of the underlying cause.",
    )
    recommended_checks: List[str] = Field(
        ...,
        min_length=1,
        description=(
            "Ordered list of diagnostic actions the agent should perform. "
            "Minimum 3 checks per pattern."
        ),
    )
    remediation_steps: List[str] = Field(
        ...,
        min_length=1,
        description="Ordered list of concrete remediation actions.",
    )
    confidence: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="Seed confidence in [0, 1]. Updated by the service as evidence accumulates.",
    )
    times_seen: int = Field(
        default=0,
        ge=0,
        description="How many times this pattern has been matched and injected.",
    )
    last_seen: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp of the most recent match (UTC).",
    )
    verified: bool = Field(
        default=False,
        description=(
            "True once a human operator has confirmed this pattern is accurate. "
            "Unverified patterns are still used but flagged in the UI."
        ),
    )
    namespace_scope: Optional[str] = Field(
        default=None,
        description=(
            "If set, this pattern is specific to the named namespace "
            "(e.g. 'kube-system', 'monitoring'). "
            "None means the pattern applies cluster-wide."
        ),
    )
