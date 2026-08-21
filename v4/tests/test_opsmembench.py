"""Unit tests for the OpsMemBench deterministic core (v5 specs/03)."""

from __future__ import annotations

import pytest
from app.eval.opsmembench import (
    GradeArtifact,
    HarnessSpec,
    ScoreCard,
    build_grade,
    decompose,
    snapshot_manifest_hash,
)


def _harness(**kw):
    base = {"model": "m", "max_gather_rounds": 8, "tool_surface": "aci-v0",
                "memory_flags": ("MEMORY_HYBRID_RETRIEVAL",), "replay_fidelity": "full", "seed": 1}
    base.update(kw)
    return HarnessSpec(**base)


# ── snapshot manifest: content-addressed + deterministic ──────────────────────
def test_manifest_hash_is_deterministic_and_order_independent():
    a = snapshot_manifest_hash({"env": "e1", "model": "m1", "memory": "mem1"})
    b = snapshot_manifest_hash({"memory": "mem1", "model": "m1", "env": "e1"})
    assert a == b


def test_manifest_hash_changes_when_any_layer_changes():
    base = snapshot_manifest_hash({"env": "e1", "model": "m1"})
    tampered = snapshot_manifest_hash({"env": "e1-tampered", "model": "m1"})
    assert base != tampered


# ── HarnessSpec disclosure is mandatory ───────────────────────────────────────
def test_grade_requires_complete_harness():
    incomplete = _harness(tool_surface="")
    art = GradeArtifact(ScoreCard(0.9, 0.8, 1000), incomplete, "hash")
    with pytest.raises(ValueError):
        art.validate()


def test_build_grade_attaches_manifest_and_validates():
    art = build_grade(ScoreCard(0.9, 0.8, 1000), _harness(), {"env": "e", "model": "m", "memory": "x"})
    assert art.manifest_hash
    assert art.to_dict()["harness"]["tool_surface"] == "aci-v0"


# ── separability gate (arXiv:2605.23950) ──────────────────────────────────────
def test_effect_below_harness_noise_is_unproven():
    # effect 0.02, harness variance 0.01 → σ=0.1, z·σ≈0.164 → not separable
    r = decompose(effect=0.02, harness_variance=0.01)
    assert r.separable is False
    assert r.verdict == "unproven"


def test_effect_above_harness_noise_is_proven():
    # effect 0.30, harness variance 0.0004 → σ=0.02, z·σ≈0.033 → separable
    r = decompose(effect=0.30, harness_variance=0.0004)
    assert r.separable is True
    assert r.verdict == "proven"


def test_zero_harness_variance_makes_any_nonzero_effect_proven():
    assert decompose(effect=0.001, harness_variance=0.0).separable is True
    assert decompose(effect=0.0, harness_variance=0.0).separable is False


def test_detail_string_reports_the_comparison():
    r = decompose(effect=0.30, harness_variance=0.0004)
    assert "effect" in r.detail and "σ_harness" in r.detail
