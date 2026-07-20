"""Machine-checkable postconditions (v5 P3) — deployment-readiness health oracle."""
from __future__ import annotations

from app.tools.aci.postcondition import deployment_ready, parse_ready_column

_GET = (
    "NAME   READY   UP-TO-DATE   AVAILABLE   AGE\n"
    "web    3/3     3            3           40h\n"
    "api    1/2     2            1           2h\n"
)


class TestParse:
    def test_reads_ready_desired(self):
        assert parse_ready_column(_GET, "web") == (3, 3)
        assert parse_ready_column(_GET, "api") == (1, 2)

    def test_missing_returns_none(self):
        assert parse_ready_column(_GET, "nope") is None

    def test_garbage_returns_none(self):
        assert parse_ready_column("no table here", "web") is None


class TestDeploymentReady:
    def test_fully_ready_met(self):
        r = deployment_ready("web", "demo", _runner=lambda c: _GET)
        assert r.met is True and r.ready == 3 and r.desired == 3

    def test_partial_not_met(self):
        r = deployment_ready("api", "demo", _runner=lambda c: _GET)
        assert r.met is False and r.ready == 1 and r.desired == 2

    def test_absent_not_met(self):
        r = deployment_ready("ghost", "demo", _runner=lambda c: _GET)
        assert r.met is False and "not found" in r.detail

    def test_read_error_safe(self):
        def boom(c):
            raise RuntimeError("no cluster")
        r = deployment_ready("web", "demo", _runner=boom)
        assert r.met is False and "read error" in r.detail

    def test_zero_desired_not_met(self):
        r = deployment_ready("scaled-down", "demo",
                             _runner=lambda c: "NAME          READY   AGE\nscaled-down   0/0     1h\n")
        assert r.met is False   # desired == 0 ⇒ not a satisfied postcondition
