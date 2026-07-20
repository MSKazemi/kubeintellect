"""Staged propagation (v5 P3 blast-radius) — never instant-global; windowed stages."""
from __future__ import annotations

from app.autonomy.staged_propagation import is_instant_global, next_stage

_TARGETS = ["c1", "c2", "c3"]


class TestNextStage:
    def test_first_stage_releases_stage_size(self):
        d = next_stage(_TARGETS, [], stage_size=1)
        assert d.batch == ["c1"] and not d.waiting and not d.done

    def test_waits_within_window(self):
        d = next_stage(_TARGETS, ["c1"], stage_size=1, window_seconds=300,
                       last_stage_epoch=1000.0, now_epoch=1100.0)   # 100s < 300s
        assert d.waiting is True and d.batch == [] and "until next stage" in d.reason

    def test_releases_after_window(self):
        d = next_stage(_TARGETS, ["c1"], stage_size=1, window_seconds=300,
                       last_stage_epoch=1000.0, now_epoch=1400.0)   # 400s ≥ 300s
        assert d.batch == ["c2"] and not d.waiting

    def test_preserves_order_across_stages(self):
        assert next_stage(_TARGETS, ["c1", "c2"], stage_size=1).batch == ["c3"]

    def test_done_when_all_applied(self):
        d = next_stage(_TARGETS, ["c1", "c2", "c3"])
        assert d.done is True and d.batch == []

    def test_stage_size_bounds_batch(self):
        d = next_stage(["a", "b", "c", "d"], [], stage_size=2)
        assert d.batch == ["a", "b"]      # never all 4 at once

    def test_never_exceeds_one_per_stage_with_size_1(self):
        # the roadmap gate: a 3-target change never applies >1 per stage
        applied: list[str] = []
        batches = []
        for _ in range(3):
            d = next_stage(_TARGETS, applied, stage_size=1)
            assert len(d.batch) <= 1
            batches.append(d.batch)
            applied += d.batch
        assert batches == [["c1"], ["c2"], ["c3"]]


class TestInstantGlobalGuard:
    def test_flags_instant_global(self):
        assert is_instant_global(_TARGETS, stage_size=3) is True
        assert is_instant_global(_TARGETS, stage_size=99) is True

    def test_single_target_is_not_global(self):
        assert is_instant_global(["only"], stage_size=1) is False

    def test_bounded_size_is_safe(self):
        assert is_instant_global(_TARGETS, stage_size=1) is False
