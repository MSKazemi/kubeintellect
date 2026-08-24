"""Three different states all answered "usage within healthy bands; no resize warranted".

`recommend()` computed `peak/limit if limit else 0.0`. A missing limit is not a ratio of
zero — and zero is the single most reassuring value on that scale, below every threshold
the function tests. So a container with **no memory limit at all**, the highest-risk memory
configuration in Kubernetes, fell through every branch into the all-clear. So did a
container nobody had observed.

Measured 2026-08-24, before the fix — all three at `is_noop=True, confidence=0.5`, with the
identical sentence:

    no memory limit at all, 900MB peak -> 'usage within healthy bands; no resize warranted'
    no limit AND no observation        -> 'usage within healthy bands; no resize warranted'
    limit set, no observation          -> 'usage within healthy bands; no resize warranted'

`recommend()` has no production caller yet (v5 P4 groundwork), so this was latent rather
than live. The module's own docstring is why it still matters: *"Recommendations are
commodity; being believed is the product."*
"""
from app.detectors.rightsizing import _HEADROOM, _MEM_LOW, Usage, recommend

MB = 1_000_000


class TestAnUnboundedContainerIsAFinding:
    def test_no_limit_with_a_peak_is_not_a_noop(self):
        r = recommend(Usage(peak_memory_bytes=900 * MB, memory_limit_bytes=0))
        assert r.is_noop is False
        assert "set_memory_limit" in r.actions

    def test_it_says_unbounded_not_healthy(self):
        r = recommend(Usage(peak_memory_bytes=900 * MB, memory_limit_bytes=0))
        text = " ".join(r.rationale)
        assert "no memory limit is set" in text
        assert "unbounded" in text
        assert "healthy bands" not in text

    def test_it_sizes_the_new_limit_against_the_observed_peak(self):
        r = recommend(Usage(peak_memory_bytes=900 * MB, memory_limit_bytes=0))
        assert r.memory_limit_bytes == int(900 * MB * _HEADROOM)

    def test_a_bounded_healthy_container_is_still_a_noop(self):
        # Vacuity guard: a recommender that flagged everything would pass all three above.
        r = recommend(Usage(peak_memory_bytes=307 * MB, memory_limit_bytes=512 * MB))
        assert r.is_noop is True
        assert r.assessed is True
        assert "usage within healthy bands" in " ".join(r.rationale)

    def test_an_unbounded_container_is_not_reported_as_over_provisioned(self):
        # The old code's `0 < ratio` guard was the only thing stopping "rightsize down" here,
        # which is why the bug presented as silence rather than as a wrong action.
        r = recommend(Usage(peak_memory_bytes=900 * MB, memory_limit_bytes=0))
        assert "decrease_memory" not in r.actions


class TestNoObservationIsNotAnAllClear:
    def test_no_peak_is_not_assessed(self):
        r = recommend(Usage(peak_memory_bytes=0, memory_limit_bytes=512 * MB))
        assert r.assessed is False
        assert r.is_noop is True

    def test_no_peak_does_not_claim_healthy_bands(self):
        r = recommend(Usage(peak_memory_bytes=0, memory_limit_bytes=512 * MB))
        text = " ".join(r.rationale)
        assert "healthy bands" not in text
        assert "no peak-memory observation" in text

    def test_no_peak_carries_no_confidence(self):
        # 0.5 said "a considered judgement of no change". There was no judgement.
        r = recommend(Usage(peak_memory_bytes=0, memory_limit_bytes=512 * MB))
        assert r.confidence == 0.0

    def test_an_assessed_noop_still_carries_its_confidence(self):
        # Vacuity guard in the other direction: zeroing every no-op would pass the test above.
        r = recommend(Usage(peak_memory_bytes=307 * MB, memory_limit_bytes=512 * MB))
        assert r.confidence == 0.5

    def test_no_peak_and_no_limit_mentions_both(self):
        r = recommend(Usage(peak_memory_bytes=0, memory_limit_bytes=0))
        text = " ".join(r.rationale)
        assert "no peak-memory observation" in text
        assert "no memory limit set" in text
        assert r.assessed is False

    def test_the_two_unassessable_cases_are_distinguishable(self):
        # They share a cause but not a remedy: one needs metrics, the other needs metrics
        # *and* a limit. Identical text is what this whole module is about.
        a = recommend(Usage(peak_memory_bytes=0, memory_limit_bytes=512 * MB))
        b = recommend(Usage(peak_memory_bytes=0, memory_limit_bytes=0))
        assert a.rationale != b.rationale


class TestTheFourOutcomesAreDistinct:
    def test_no_two_states_share_a_rendering(self):
        states = {
            "unbounded": Usage(peak_memory_bytes=900 * MB, memory_limit_bytes=0),
            "unobserved": Usage(peak_memory_bytes=0, memory_limit_bytes=512 * MB),
            "unobserved+unbounded": Usage(peak_memory_bytes=0, memory_limit_bytes=0),
            "healthy": Usage(peak_memory_bytes=307 * MB, memory_limit_bytes=512 * MB),
            "under": Usage(peak_memory_bytes=486 * MB, memory_limit_bytes=512 * MB),
            "over": Usage(peak_memory_bytes=102 * MB, memory_limit_bytes=512 * MB),
        }
        rendered = {k: (recommend(v).assessed, tuple(recommend(v).actions),
                        tuple(recommend(v).rationale)) for k, v in states.items()}
        assert len(set(rendered.values())) == len(states), (
            f"two states render identically: {rendered}"
        )


class TestTheExistingSignalsStillWork:
    def test_an_oom_with_no_limit_says_set_not_raise(self):
        r = recommend(Usage(peak_memory_bytes=900 * MB, memory_limit_bytes=0, oom_count=2))
        assert "set_memory_limit" in r.actions
        assert "set memory limit" in " ".join(r.rationale)
        assert r.confidence == 0.9

    def test_an_oom_with_a_limit_still_says_raise(self):
        r = recommend(Usage(peak_memory_bytes=486 * MB, memory_limit_bytes=512 * MB, oom_count=1))
        assert "increase_memory" in r.actions
        assert "raise memory limit" in " ".join(r.rationale)

    def test_the_over_provisioned_boundary_did_not_move(self):
        # `0 < ratio <= _MEM_LOW` became `ratio <= _MEM_LOW`; the boundary must be unchanged.
        at = recommend(Usage(peak_memory_bytes=int(512 * MB * _MEM_LOW),
                             memory_limit_bytes=512 * MB))
        just_above = recommend(Usage(peak_memory_bytes=int(512 * MB * _MEM_LOW) + MB,
                                     memory_limit_bytes=512 * MB))
        assert "decrease_memory" in at.actions
        assert "decrease_memory" not in just_above.actions

    def test_cpu_throttling_is_unaffected_by_the_memory_rework(self):
        r = recommend(Usage(peak_memory_bytes=307 * MB, memory_limit_bytes=512 * MB,
                            cpu_throttle_pct=0.4, cpu_limit_millicores=500))
        assert "increase_cpu" in r.actions
        assert r.cpu_limit_millicores == int(500 * 1.4)
        assert r.assessed is True
