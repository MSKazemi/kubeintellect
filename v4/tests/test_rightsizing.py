"""Evidence-grounded rightsizing (v5 P4, A-CH-08) — resize recommendations from signals."""
from __future__ import annotations

from app.detectors.rightsizing import Recommendation, Usage, recommend

_MB = 1024 * 1024


class TestMemory:
    def test_oom_recommends_increase(self):
        r = recommend(Usage(peak_memory_bytes=200 * _MB, memory_limit_bytes=256 * _MB, oom_count=3))
        assert "increase_memory" in r.actions
        assert r.memory_limit_bytes == int(200 * _MB * 1.25)
        assert r.confidence >= 0.9 and any("OOMKill" in x for x in r.rationale)

    def test_high_ratio_pre_emptive_increase(self):
        r = recommend(Usage(peak_memory_bytes=240 * _MB, memory_limit_bytes=256 * _MB))  # 94%
        assert "increase_memory" in r.actions and any("under-provisioned" in x for x in r.rationale)

    def test_low_ratio_rightsize_down(self):
        r = recommend(Usage(peak_memory_bytes=50 * _MB, memory_limit_bytes=256 * _MB))   # ~20%
        assert "decrease_memory" in r.actions and any("over-provisioned" in x for x in r.rationale)

    def test_healthy_band_noop(self):
        r = recommend(Usage(peak_memory_bytes=150 * _MB, memory_limit_bytes=256 * _MB))  # ~59%
        assert r.is_noop and any("no resize" in x for x in r.rationale)


class TestCpu:
    def test_throttle_recommends_cpu_increase(self):
        r = recommend(Usage(peak_memory_bytes=100 * _MB, memory_limit_bytes=256 * _MB,
                            cpu_throttle_pct=0.4, cpu_limit_millicores=500))
        assert "increase_cpu" in r.actions
        assert r.cpu_limit_millicores == int(500 * 1.4)

    def test_low_throttle_no_cpu_change(self):
        r = recommend(Usage(peak_memory_bytes=100 * _MB, memory_limit_bytes=256 * _MB,
                            cpu_throttle_pct=0.05, cpu_limit_millicores=500))
        assert "increase_cpu" not in r.actions


class TestCombined:
    def test_oom_and_throttle_both_recommended(self):
        r = recommend(Usage(peak_memory_bytes=250 * _MB, memory_limit_bytes=256 * _MB, oom_count=1,
                            cpu_throttle_pct=0.5, cpu_limit_millicores=1000))
        assert set(r.actions) == {"increase_memory", "increase_cpu"}
        assert isinstance(r, Recommendation) and not r.is_noop
