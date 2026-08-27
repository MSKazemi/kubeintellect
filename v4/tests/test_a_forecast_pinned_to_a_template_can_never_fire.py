"""A trend predicate had no liveness gate, and the gap shipped dead forecasts.

Eight NL-authored detectors were staged on the F3 soak cluster. Two of them forecast

    kube_deployment_status_replicas{deployment="your-deployment-name"}
    kube_deployment_status_replicas{deployment="your_service_name"}

— the authoring model returned its own *template* instead of filling it in, and the template was
accepted, stored, listed as `shadow`, and offered for promotion. A selector pinned to a series
name no cluster has returns nothing; `project_eta` never gets its two samples; the detector's
zero firings are indistinguishable from a cluster that never had the problem.

`predicate_liveness_errors` existed and would have had nothing to say anyway — it only ever
walked `block.watch_predicates`. `trend_predicates` were exempt at all three call sites: the
authoring validator, the engine's load path, and the promotion gate.

These tests pin the three properties that matter:
  * the two real stored predicates are refused, and a real metric is not;
  * every bound the engine actually enforces is checked against an impossible value;
  * the refusal happens at all three gates, not just the one that is easiest to test.
"""
from __future__ import annotations

import json

import pytest

from app.detectors import engine as engine_mod
from app.detectors.authoring import validate_detect_block
from app.detectors.models import TrendPredicate
from app.detectors.predicate_shape import trend_liveness_errors

# Verbatim from the `detectors` table on kind-ki-soak-c1, 2026-08-25.
LIVE_PLACEHOLDER_METRICS = (
    'kube_deployment_status_replicas{deployment="your-deployment-name"}',
    'kube_deployment_status_replicas{deployment="your_service_name"}',
)


def _trend(metric: str, **kw) -> TrendPredicate:
    return TrendPredicate(metric=metric, threshold=kw.pop("threshold", 1.0), **kw)


class TestTheTemplatesThatWereActuallyStored:
    @pytest.mark.parametrize("metric", LIVE_PLACEHOLDER_METRICS)
    def test_a_stored_template_is_refused(self, metric):
        errors = trend_liveness_errors(_trend(metric))
        assert errors, f"{metric!r} matches no series and was accepted anyway"
        assert "template" in errors[0]

    def test_a_real_selector_is_not_refused(self):
        # The same metric with a name a cluster can actually have. The check must not fire here,
        # or it would refuse working detectors to catch templates — the worse trade.
        assert trend_liveness_errors(
            _trend('kube_deployment_status_replicas{deployment="payments-api"}')
        ) == []

    @pytest.mark.parametrize("name", ["example", "foo", "test", "my-app", "demo", "sample-svc"])
    def test_ordinary_names_that_look_like_placeholders_are_left_alone(self, name):
        # Every one of these is a name someone has really shipped. Guessing at intent is how a
        # validator starts refusing valid work.
        assert trend_liveness_errors(
            _trend(f'kube_deployment_status_replicas{{deployment="{name}"}}')
        ) == []

    @pytest.mark.parametrize("value", ["<name>", "{{ app }}", "${DEPLOYMENT}", "CHANGEME", "TODO"])
    def test_the_other_templating_syntaxes_are_refused_too(self, value):
        assert trend_liveness_errors(
            _trend(f'kube_deployment_status_replicas{{deployment="{value}"}}')
        )

    def test_a_regex_matcher_is_read_as_well_as_an_equality_one(self):
        assert trend_liveness_errors(
            _trend('kube_deployment_status_replicas{deployment=~"your-deployment-name"}')
        )


class TestBoundsTheEngineActuallyEnforces:
    """Each one is read off `project_eta` and its caller, not invented."""

    def test_an_r2_floor_above_one_can_never_be_met(self):
        # r2 is a squared correlation coefficient: it is in [0, 1] by construction.
        errors = trend_liveness_errors(_trend("up", min_r2=1.5))
        assert errors and "1.0" in errors[0]

    def test_a_reachable_r2_floor_is_fine(self):
        assert trend_liveness_errors(_trend("up", min_r2=1.0)) == []

    @pytest.mark.parametrize("field", ["fire_if_eta_within_minutes", "projection_horizon_minutes"])
    def test_a_non_positive_eta_bound_excludes_every_eta(self, field):
        # The caller fires only when 0 < eta_minutes <= this. A bound of 0 or less has no
        # value of eta that satisfies both halves.
        errors = trend_liveness_errors(_trend("up", **{field: 0}))
        assert errors and field in errors[0]

    def test_an_empty_lookback_never_gets_two_samples(self):
        errors = trend_liveness_errors(_trend("up", window_minutes=0))
        assert errors and "window_minutes" in errors[0]

    def test_a_metric_that_is_only_whitespace_is_refused(self):
        errors = trend_liveness_errors(_trend("   "))
        assert errors and "no metric" in errors[0]

    def test_a_misspelled_direction_is_refused_rather_than_silently_inverted(self):
        # `project_eta` treats anything that is not exactly "falling" as rising, so a typo does
        # not fail — it quietly asks the opposite question.
        errors = trend_liveness_errors(_trend("up", direction="decreasing"))
        assert errors and "rising" in errors[0]

    @pytest.mark.parametrize("direction", ["rising", "falling"])
    def test_both_real_directions_pass(self, direction):
        assert trend_liveness_errors(_trend("up", direction=direction)) == []


class TestTheAuthoringGate:
    def test_a_block_whose_only_predicate_is_a_template_does_not_validate(self):
        block, errors = validate_detect_block(
            {"trend_predicates": [{"metric": LIVE_PLACEHOLDER_METRICS[0], "threshold": 0,
                                   "direction": "falling"}]},
            name="nl:soak-replicas-short",
        )
        assert block is None, "a forecast over an unfilled template was staged as a candidate"
        assert any("template" in e for e in errors)

    def test_the_same_block_with_a_real_deployment_validates(self):
        block, errors = validate_detect_block(
            {"trend_predicates": [
                {"metric": 'kube_deployment_status_replicas{deployment="payments-api"}',
                 "threshold": 0, "direction": "falling"}]},
            name="nl:soak-replicas-short",
        )
        assert errors == []
        assert block is not None


class _FakePool:
    def __init__(self, rows: list[dict]) -> None:
        self.rows, self.sql, self.args = rows, "", ()

    async def fetch(self, sql: str, *args):
        self.sql, self.args = sql, args
        wanted = {args[0], "global"} if "'global'" in sql else {args[0]}
        return [r for r in self.rows
                if r["cluster_id"] in wanted and r["status"] in ("active", "shadow")]

    async def fetchrow(self, sql: str, *args):
        self.sql, self.args = sql, args
        wanted = {args[0], "global"} if "'global'" in sql else {args[0]}
        for r in self.rows:
            if r["cluster_id"] in wanted and r["name"] == args[1]:
                return r
        return None


def _row(name: str, metric: str, cluster_id: str = "global", status: str = "shadow") -> dict:
    return {
        "name": name,
        "cluster_id": cluster_id,
        "status": status,
        "predicate": json.dumps({"trend_predicates": [
            {"metric": metric, "threshold": 0, "direction": "falling"}]}),
    }


@pytest.fixture
def pool(monkeypatch):
    def _install(rows):
        from app.memory import service as mem_service
        p = _FakePool(rows)
        monkeypatch.setattr(mem_service, "_pool", p, raising=False)
        return p
    return _install


class TestTheLoadGate:
    """The engine is the one point every stored row passes through, however it got there."""

    async def test_a_stored_template_row_is_not_loaded(self, pool):
        pool([_row("nl:soak-replicas-short", LIVE_PLACEHOLDER_METRICS[0])])
        active, shadow = await engine_mod.load_db_detectors("f3-shadow-soak-r2")
        assert (active, shadow) == ((), ()), (
            "a forecast pinned to an unfilled template was loaded; its zero firings would be "
            "read as precision evidence by whoever decides to promote it"
        )

    async def test_a_real_row_beside_it_still_loads(self, pool):
        pool([
            _row("nl:soak-replicas-short", LIVE_PLACEHOLDER_METRICS[0]),
            _row("nl:real", 'kube_deployment_status_replicas{deployment="payments-api"}'),
        ])
        _active, shadow = await engine_mod.load_db_detectors("f3-shadow-soak-r2")
        assert [d.playbook for d in shadow] == ["nl:real"]


class TestThePromotionGate:
    async def test_a_global_row_is_found_from_a_named_cluster(self, pool):
        """The same scoping bug as `load_db_detectors`, in the gate meant to catch it.

        `stage_candidate` stores under `global`; this read used `cluster_id = $1`, found nothing,
        and returned None — which the caller reads as "no reason to refuse".
        """
        from app.detectors import review as review_mod

        p = pool([_row("nl:soak-replicas-short", LIVE_PLACEHOLDER_METRICS[0])])
        reason = await review_mod._liveness_error("nl:soak-replicas-short", "f3-shadow-soak-r2")
        assert reason is not None, (
            f"the promotion gate could not see a globally-stored detector. SQL was: {p.sql}"
        )
        assert "template" in reason

    async def test_a_live_detector_is_not_refused_promotion(self, pool):
        from app.detectors import review as review_mod

        pool([_row("nl:real", 'kube_deployment_status_replicas{deployment="payments-api"}')])
        assert await review_mod._liveness_error("nl:real", "f3-shadow-soak-r2") is None
