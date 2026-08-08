"""OpsMemBench deterministic core (v5 specs/03).

The live Kind fault-injection driver and snapshot capture need a cluster; this module is the
pure, deterministic spine underneath them:

- ``HarnessSpec`` — the mandatory disclosure record every reported number ships with (the
  eval-rigor "steal" from arXiv:2605.23950: token/harness explains ~80% of variance (C1), so a
  number without its harness is uninterpretable).
- ``snapshot_manifest_hash`` — content-addressed integrity for the record/replay twin, reusing
  the flight recorder's SHA-256 so a tampered layer forces a replay mismatch.
- ``GradeArtifact`` + ``decompose`` — wraps a raw score with its HarnessSpec and enforces the
  separability gate: a differentiation claim (e.g. "memory lifts M1") is stamped ``unproven``
  unless the effect exceeds ``z·√harness_variance``.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Optional

from app.db.flight_recorder import compute_hash

_Z_95 = 1.6448536269514722


@dataclass(frozen=True)
class HarnessSpec:
    """Full disclosure of the harness a number was produced under (required)."""
    model: str
    max_gather_rounds: int
    tool_surface: str            # e.g. "aci-v0" | "kubectl-flat"
    memory_flags: tuple[str, ...]
    replay_fidelity: str         # "full" | "environment" | "live"
    seed: int

    def is_complete(self) -> bool:
        return bool(self.model and self.tool_surface and self.replay_fidelity)


@dataclass(frozen=True)
class ScoreCard:
    """A single graded run's metrics (M1–M5 abbreviated here)."""
    m1_localized: float          # correct root-cause localization rate
    m2_resolved: float           # postcondition-verified resolution rate
    tokens: int


def snapshot_manifest_hash(layers: dict[str, str]) -> str:
    """Content-addressed hash of a snapshot's layer digests (env/model/memory).

    Deterministic: sorts keys, reuses the recorder's compute_hash so the same tamper-evidence
    guarantees apply. Any changed layer byte changes the manifest hash → replay mismatch.
    """
    canonical = json.dumps(layers, sort_keys=True, separators=(",", ":"))
    return compute_hash("", "opsmembench", 0, "snapshot_manifest", {"canonical": canonical})


@dataclass
class GradeArtifact:
    scorecard: ScoreCard
    harness: HarnessSpec
    manifest_hash: str
    notes: list[str] = field(default_factory=list)

    def validate(self) -> None:
        """A GradeArtifact with an incomplete HarnessSpec is not shippable (raises)."""
        if not self.harness.is_complete():
            raise ValueError("GradeArtifact requires a complete HarnessSpec (disclosure standard).")

    def to_dict(self) -> dict:
        return {
            "scorecard": asdict(self.scorecard),
            "harness": asdict(self.harness),
            "manifest_hash": self.manifest_hash,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class SeparabilityResult:
    effect: float
    harness_std: float
    separable: bool
    verdict: str                 # "proven" | "unproven"
    detail: str


def decompose(
    effect: float,
    harness_variance: float,
    z: float = _Z_95,
) -> SeparabilityResult:
    """Separability gate (arXiv:2605.23950): a claimed effect is ``proven`` only if it exceeds
    ``z·√harness_variance``; otherwise it cannot be told apart from harness noise → ``unproven``.
    """
    harness_std = math.sqrt(max(harness_variance, 0.0))
    threshold = z * harness_std
    separable = abs(effect) > threshold
    return SeparabilityResult(
        effect=effect,
        harness_std=harness_std,
        separable=separable,
        verdict="proven" if separable else "unproven",
        detail=f"|effect|={abs(effect):.4f} {'>' if separable else '<='} z·σ_harness={threshold:.4f}",
    )


def build_grade(
    scorecard: ScoreCard,
    harness: HarnessSpec,
    snapshot_layers: dict[str, str],
    notes: Optional[list[str]] = None,
) -> GradeArtifact:
    art = GradeArtifact(
        scorecard=scorecard,
        harness=harness,
        manifest_hash=snapshot_manifest_hash(snapshot_layers),
        notes=notes or [],
    )
    art.validate()
    return art
