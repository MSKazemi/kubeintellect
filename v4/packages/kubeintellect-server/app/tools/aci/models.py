"""Typed contracts for the K8s-ACI v0 verbs (v5 specs/01)."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class AciContractError(RuntimeError):
    """Raised when a verb would emit a non-read-only or malformed kubectl command.

    A programming error (a verb built a mutating command) — never surfaced to the
    cluster or the model.
    """


class Health(str, Enum):
    CURRENT = "Current"
    IN_PROGRESS = "InProgress"
    FAILED = "Failed"
    TERMINATING = "Terminating"
    UNKNOWN = "Unknown"


# ── Inputs ────────────────────────────────────────────────────────────────────
class InspectInput(BaseModel):
    kind: str = Field(description="Kubernetes kind, e.g. 'deployment', 'pod'.")
    name: str = Field(description="Object name.")
    namespace: str | None = Field(default=None, description="Namespace; omit for cluster-scoped.")
    view: Literal["summary", "status", "full"] = "summary"


class SearchInput(BaseModel):
    kinds: list[str] = Field(min_length=1, description="One or more kinds to list.")
    namespace: str | None = None
    all_namespaces: bool = False
    selector: str | None = Field(default=None, description="Label selector, e.g. 'app=web'.")
    limit: int = Field(default=100, ge=1, le=100)

    @field_validator("limit")
    @classmethod
    def _clamp(cls, v: int) -> int:
        return min(v, 100)


class LogsInput(BaseModel):
    namespace: str
    pod: str | None = None
    selector: str | None = Field(default=None, description="Label selector (mutually exclusive with pod).")
    container: str | None = None
    lines: int = Field(default=100, ge=1, le=100)
    since: str | None = Field(default=None, description="e.g. '5m', '1h'.")
    previous: bool = Field(default=False, description="Read last terminated container (dead-pod evidence).")


class DiffChangeInput(BaseModel):
    against: Literal["live", "previous", "git"]
    kind: str
    name: str
    namespace: str | None = None
    manifest: str | None = Field(default=None, description="Proposed YAML for against=live.")
    revision: int | None = Field(default=None, description="Prior revision for against=previous.")


# ── Output envelope (the replay artifact) ─────────────────────────────────────
class AciResult(BaseModel):
    verb: str
    ok: bool
    rollback_class: Literal["R0"] = "R0"  # every v0 verb is read-only
    target: str
    kubectl_command: str
    health: Health | None = None
    total_lines: int = 0
    shown_lines: int = 0
    cursor: int | None = None
    body: str = ""
    empty: bool = False
    error: str | None = None

    def render(self) -> str:
        """The bounded string the model sees. Never empty (R-aci-empty-01)."""
        if self.error is not None:
            return f"[aci:{self.verb}] {self.target} — FAILED\n{self.error}"
        if self.empty:
            return f"[aci:{self.verb}] {self.target} — ran successfully, no matching results."
        header = f"[aci:{self.verb}] {self.target}"
        if self.health is not None:
            header += f" — health={self.health.value}"
        if self.cursor is not None:
            header += (
                f" — showing {self.shown_lines}/{self.total_lines} lines "
                f"(next offset {self.cursor})"
            )
        return f"{header}\n{self.body}"
